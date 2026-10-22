import errno
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from whipit.audio.opus import DEFAULT_MAX_WORKERS, MAX_WORKERS, _encode_single_track, transcode_flacs_to_opus
from whipit.inhibit import MediaDialogInhibitor
from whipit.models import InvariantViolationError, WorkflowResult, WorkflowState
from whipit.workflow import RipWorkflowCoordinator


# 1. Preflight failure: Mock shutil.disk_usage() to raise OSError; assert the workflow aborts before extraction.
def test_preflight_fails_closed_and_aborts_before_extraction(temp_env, monkeypatch):
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda _: (_ for _ in ()).throw(OSError("simulated filesystem error")),
    )
    coordinator = RipWorkflowCoordinator(device="/dev/sr0")
    ok, error = coordinator.check_disk_space_preflight()
    assert ok is False
    assert "simulated filesystem error" in str(error)

    res = coordinator.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coordinator.manifest.state == WorkflowState.ABORTED
    assert coordinator.job_lock_fd is None


# 2. Pipe saturation: Run a fake encoder that writes substantial output to stderr; assert the worker completes without deadlock.
def test_pipe_saturation_stderr_drained_without_deadlock(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir(parents=True, exist_ok=True)
    fake_opusenc = mock_bin / "opusenc"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "# Write 128 KiB to stderr (exceeding standard 64 KiB OS pipe buffer)\n"
        "sys.stderr.write('E' * (128 * 1024))\n"
        "sys.stderr.flush()\n"
        "# Touch destination output passed as last arg\n"
        "dest = sys.argv[-1]\n"
        "with open(dest, 'wb') as f:\n"
        "    f.write(b'OPUS_DATA')\n"
    )
    fake_opusenc.write_text(script, encoding="utf-8")
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    t0 = time.time()
    ok, msg = _encode_single_track(flac, tmp_path, tmp_path / "opus", cover_art=None)
    elapsed = time.time() - t0

    assert ok is True
    assert elapsed < 25.0  # Generous hang-detection threshold; separates deadlock check from strict benchmarks
    assert (tmp_path / "opus" / "track.opus").exists()


# 3. Cancellation cleanup: Cancel a running encoder and verify the process exits, its streams are closed, and temporary outputs are removed.
def test_cancellation_cleanup_terminates_process_closes_streams_and_removes_temp(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    closed_streams = []

    class MockStream:
        def __init__(self, name):
            self.name = name
        def close(self):
            closed_streams.append(self.name)

    cancel_evt = threading.Event()
    process_spawned = threading.Event()

    class MockProc:
        def __init__(self, *args, **kwargs):
            self.returncode = None
            self.stdout = MockStream("stdout")
            self.stderr = MockStream("stderr")
            self.stdin = MockStream("stdin")
            self.pid = 98765
            process_spawned.set()
            cancel_evt.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr("subprocess.Popen", MockProc)

    active_procs = {}
    lock = threading.Lock()

    ok, msg = _encode_single_track(
        flac,
        tmp_path,
        tmp_path / "opus",
        cover_art=None,
        cancel_event=cancel_evt,
        active_procs=active_procs,
        procs_lock=lock,
    )

    assert ok is False
    assert "Cancelled" in msg
    assert process_spawned.is_set()
    assert "stdout" in closed_streams
    assert "stderr" in closed_streams
    assert "stdin" in closed_streams
    assert len(active_procs) == 0
    assert len(list((tmp_path / "opus").glob(".tmp.*"))) == 0


# 4. Settings persistence failure: Force the state-file write to fail and verify that desktop settings are not modified.
def test_settings_persistence_failure_aborts_without_modifying_settings(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    applied_modifications = []

    def mock_run(cmd, *a, **kw):
        if "-s" in cmd or "set" in cmd:
            applied_modifications.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n")

    monkeypatch.setattr("subprocess.run", mock_run)

    inhibitor = MediaDialogInhibitor()
    monkeypatch.setattr(inhibitor, "_persist_state", lambda: False)

    with pytest.raises((RuntimeError, InvariantViolationError), match="Cannot safely suppress desktop automount"):
        inhibitor.start()

    assert len(applied_modifications) == 0
    assert not state_file.exists()


# 5. Worker exception reporting: Raise an unexpected exception inside a transcode future and verify that the error is logged with track context.
def test_worker_exception_logged_with_track_context(tmp_path, monkeypatch, caplog):
    flac = tmp_path / "failing_track_01.flac"
    flac.touch()

    def faulty_encode(*args, **kwargs):
        raise OSError("Simulated I/O fault inside transcode worker")

    monkeypatch.setattr("whipit.audio.opus._encode_single_track", faulty_encode)

    with caplog.at_level(logging.ERROR):
        ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", cover_art=None)

    assert ok is False
    assert any(
        "Unexpected Opus worker failure for" in r.message and "failing_track_01.flac" in r.message
        for r in caplog.records
    )
    assert any("Opus encoding failed for" in r.message for r in caplog.records)


# 6. High-core systems: Simulate a large os.cpu_count() and assert that the default encoder concurrency remains capped.
def test_high_core_systems_default_concurrency_remains_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 128)

    flacs = [tmp_path / f"track_{i:02d}.flac" for i in range(20)]
    for f in flacs:
        f.touch()

    captured_max_workers = []
    orig_init = ThreadPoolExecutor.__init__

    def spy_executor_init(self, *args, **kwargs):
        mw = kwargs.get("max_workers") or (args[0] if args else None)
        captured_max_workers.append(mw)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "__init__", spy_executor_init)
    monkeypatch.setattr("whipit.audio.opus._encode_single_track", lambda *a, **kw: (True, "Encoded"))

    ok = transcode_flacs_to_opus(flacs, tmp_path, tmp_path / "opus", cover_art=None)
    assert ok is True
    assert len(captured_max_workers) >= 1
    assert captured_max_workers[0] == DEFAULT_MAX_WORKERS
    assert captured_max_workers[0] <= MAX_WORKERS
