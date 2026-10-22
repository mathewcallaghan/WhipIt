import os
import signal
import threading
import time
from pathlib import Path
import pytest
from whipit.ripping.whipper import WhipperRunner


def _setup_mock_tools(tmp_path: Path, monkeypatch, script_content: str) -> Path:
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    dummy_paranoia = tmp_bin / "cd-paranoia"
    dummy_paranoia.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy_paranoia.chmod(0o755)

    whipper_bin = tmp_bin / "whipper"
    whipper_bin.write_text(script_content, encoding="utf-8")
    whipper_bin.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)
    return stage_dir


def test_whipper_cancellation_while_child_running(tmp_path, monkeypatch):
    script = (
        "#!/usr/bin/env python3\n"
        "import time, sys\n"
        "print('STARTED', flush=True)\n"
        "time.sleep(10)\n"
    )
    stage_dir = _setup_mock_tools(tmp_path, monkeypatch, script)

    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")
    cancel_evt = threading.Event()
    results = []

    def run():
        res = runner.run_rip(stage_dir, cancel_event=cancel_evt)
        results.append(res)

    t = threading.Thread(target=run)
    t.start()

    for _ in range(50):
        if runner.active_proc is not None:
            break
        time.sleep(0.05)
    assert runner.active_proc is not None

    cancel_evt.set()
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert len(results) == 1
    assert results[0].cancelled is True
    assert runner.active_proc is None


def test_whipper_cancellation_during_output(tmp_path, monkeypatch):
    script = (
        "#!/usr/bin/env python3\n"
        "import time, sys\n"
        "for i in range(100):\n"
        "    print(f'Ripping track {i+1} of 100', flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    stage_dir = _setup_mock_tools(tmp_path, monkeypatch, script)

    received_lines = []
    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0", output_callback=received_lines.append)
    results = []

    def run():
        res = runner.run_rip(stage_dir)
        results.append(res)

    t = threading.Thread(target=run)
    t.start()

    for _ in range(50):
        if len(received_lines) >= 2:
            break
        time.sleep(0.05)
    assert len(received_lines) >= 2

    runner.cancel()
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert len(results) == 1
    assert results[0].cancelled is True
    assert runner.active_proc is None


def test_whipper_repeated_cancellation(tmp_path, monkeypatch):
    script = (
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(10)\n"
    )
    stage_dir = _setup_mock_tools(tmp_path, monkeypatch, script)

    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")

    runner.cancel()
    runner.cancel()

    results = []
    def run():
        res = runner.run_rip(stage_dir)
        results.append(res)

    t = threading.Thread(target=run)
    t.start()

    for _ in range(50):
        if runner.active_proc is not None:
            break
        time.sleep(0.05)
    assert runner.active_proc is not None

    runner.cancel()
    runner.cancel()
    runner.cancel()

    t.join(timeout=5.0)
    assert not t.is_alive()
    assert len(results) == 1
    assert results[0].cancelled is True

    runner.cancel()
    runner.cancel()
    assert runner.active_proc is None


def test_whipper_child_ignores_sigterm_killed_by_sigkill(tmp_path, monkeypatch):
    script = (
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('IGNORING_SIGTERM', flush=True)\n"
        "time.sleep(10)\n"
    )
    stage_dir = _setup_mock_tools(tmp_path, monkeypatch, script)

    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")
    results = []

    def run():
        res = runner.run_rip(stage_dir)
        results.append(res)

    t = threading.Thread(target=run)
    t.start()

    for _ in range(50):
        if runner.active_proc is not None:
            break
        time.sleep(0.05)
    assert runner.active_proc is not None

    runner.cancel()
    t.join(timeout=6.0)
    assert not t.is_alive()

    assert len(results) == 1
    assert results[0].cancelled is True
    assert results[0].returncode == -signal.SIGKILL


def test_whipper_bounded_diagnostic_tail_and_early_flag_detection(tmp_path, monkeypatch):
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('CD-R detected: true', flush=True)\n"
        "for i in range(3000):\n"
        "    print(f'noise line {i}', flush=True)\n"
    )
    stage_dir = _setup_mock_tools(tmp_path, monkeypatch, script)

    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")
    res = runner.run_rip(stage_dir)

    assert res.is_cdr is True
    # Verify stdout in memory was bounded to at most 2000 lines
    stdout_lines = res.stdout.splitlines()
    assert len(stdout_lines) <= 2000
    # Verify log file on disk has full output (>3000 lines)
    log_lines = res.log_file.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) >= 3000
