import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
import pytest
from whipit.audio.opus import _encode_single_track, stop_process, transcode_flacs_to_opus


def test_stop_process_terminates_and_reaps():
    proc = subprocess.Popen(
        ["sleep", "10"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.poll() is None
    stop_process(proc, grace_seconds=1.0)
    assert proc.poll() is not None


def test_stop_process_handles_already_exited_process():
    proc = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    assert proc.poll() is not None
    stop_process(proc, grace_seconds=1.0)
    assert proc.poll() == 0


def test_stop_process_escalates_to_sigkill_on_hung_process():
    proc = subprocess.Popen(
        [
            "python3",
            "-u",
            "-c",
            "import signal, sys, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "sys.stdout.write('READY\\n'); sys.stdout.flush(); time.sleep(10)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    # Ensure child has installed the SIG_IGN handler before attempting stop
    ready = proc.stdout.readline()
    assert "READY" in ready

    stop_process(proc, grace_seconds=0.3)
    assert proc.poll() is not None
    assert proc.returncode == -signal.SIGKILL


def test_opus_encode_does_not_deadlock_on_large_output(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    fake_opusenc = mock_bin / "opusenc"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('A' * (128 * 1024))\n"
        "sys.stdout.flush()\n"
        "dest = sys.argv[-1]\n"
        "with open(dest, 'wb') as f:\n"
        "    f.write(b'OPUS_DATA')\n"
    )
    fake_opusenc.write_text(script)
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    t0 = time.time()
    ok, msg = _encode_single_track(flac, tmp_path, tmp_path / "opus", cover_art=None)
    elapsed = time.time() - t0

    assert ok is True
    assert elapsed < 25.0  # Generous hang-detection threshold; separates deadlock check from strict benchmarks
    assert (tmp_path / "opus" / "track.opus").exists()


def test_opus_encode_captures_diagnostic_and_cleans_temp_file_on_error(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    fake_opusenc = mock_bin / "opusenc"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('CRITICAL: Corrupted FLAC input header', flush=True)\n"
        "dest = sys.argv[-1]\n"
        "with open(dest, 'wb') as f:\n"
        "    f.write(b'INCOMPLETE')\n"
        "sys.exit(1)\n"
    )
    fake_opusenc.write_text(script)
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    ok, msg = _encode_single_track(flac, tmp_path, tmp_path / "opus", cover_art=None)

    assert ok is False
    assert "Corrupted FLAC input header" in msg
    assert len(list((tmp_path / "opus").glob(".tmp.*"))) == 0


def test_opus_encode_cancellation_cleans_temp_file(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    fake_opusenc = mock_bin / "opusenc"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "dest = sys.argv[-1]\n"
        "with open(dest, 'wb') as f:\n"
        "    f.write(b'PARTIAL')\n"
        "time.sleep(10)\n"
    )
    fake_opusenc.write_text(script)
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    cancel_evt = threading.Event()
    results = []

    def run():
        res = _encode_single_track(flac, tmp_path, tmp_path / "opus", cover_art=None, cancel_event=cancel_evt)
        results.append(res)

    t = threading.Thread(target=run)
    t.start()

    time.sleep(0.2)
    cancel_evt.set()
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert len(results) == 1
    assert results[0][0] is False
    assert "Cancelled" in results[0][1]
    assert len(list((tmp_path / "opus").glob(".tmp.*"))) == 0


def test_opus_encode_unconditional_stream_and_process_cleanup(tmp_path, monkeypatch):
    flac = tmp_path / "track.flac"
    flac.touch()

    closed_streams = []

    class MockStream:
        def __init__(self, name):
            self.name = name
        def close(self):
            closed_streams.append(self.name)

    cancel_evt = threading.Event()

    class MockProc:
        def __init__(self, *args, **kwargs):
            self.returncode = None
            self.stdout = MockStream("stdout")
            self.stderr = MockStream("stderr")
            self.stdin = MockStream("stdin")
            self.pid = 12345
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
    assert "stdout" in closed_streams
    assert "stderr" in closed_streams
    assert "stdin" in closed_streams
    assert len(active_procs) == 0


def test_transcode_flacs_to_opus_worker_bounds(tmp_path):
    flac = tmp_path / "track.flac"
    flac.touch()

    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", None, max_workers=0)

    with pytest.raises(ValueError, match="max_workers must be at least 1"):
        transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", None, max_workers=-5)


def test_opus_encode_enforces_log_size_bound(tmp_path, monkeypatch):
    flac = tmp_path / "track_noisy.flac"
    flac.touch()

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir(parents=True, exist_ok=True)
    fake_opusenc = mock_bin / "opusenc"
    script = (
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('X' * 65536 + '\\n')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.001)\n"
    )
    fake_opusenc.write_text(script, encoding="utf-8")
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    t0 = time.time()
    ok, msg = _encode_single_track(flac, tmp_path, tmp_path / "opus", cover_art=None)
    elapsed = time.time() - t0

    assert ok is False
    assert elapsed < 25.0  # Must terminate promptly without hanging
    assert "exceeded maximum allowed size" in msg
    assert len(list((tmp_path / "opus").glob(".tmp.*"))) == 0


def test_transcode_flacs_to_opus_bounded_in_flight_tasks(tmp_path, monkeypatch):
    flacs = [tmp_path / f"track_{i:02d}.flac" for i in range(12)]
    for f in flacs:
        f.touch()

    max_active_observed = 0
    current_active = 0
    lock = threading.Lock()

    def mock_encode(flac_file, *args, **kwargs):
        nonlocal max_active_observed, current_active
        with lock:
            current_active += 1
            if current_active > max_active_observed:
                max_active_observed = current_active
        time.sleep(0.02)
        with lock:
            current_active -= 1
        return True, "Encoded"

    monkeypatch.setattr("whipit.audio.opus._encode_single_track", mock_encode)

    ok = transcode_flacs_to_opus(flacs, tmp_path, tmp_path / "opus", None, max_workers=2)
    assert ok is True
    assert max_active_observed <= 2


def test_transcode_flacs_to_opus_logs_future_exception_details(tmp_path, monkeypatch, caplog):
    flac = tmp_path / "track_fail.flac"
    flac.touch()

    def crashing_encode(*args, **kwargs):
        raise OSError("Simulated disk I/O failure during transcode")

    monkeypatch.setattr("whipit.audio.opus._encode_single_track", crashing_encode)

    with caplog.at_level(logging.ERROR):
        ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", None)

    assert ok is False
    assert any("Unexpected Opus worker failure for" in r.message and "track_fail.flac" in r.message for r in caplog.records)
    assert any("Opus encoding failed for" in r.message for r in caplog.records)
