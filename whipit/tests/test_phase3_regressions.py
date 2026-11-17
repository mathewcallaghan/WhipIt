"""Regression tests for Phase 3 inhibit, whipper subprocess, engine bounds, identify, and config locking."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from whipit.config import get_config_dir, get_whipper_conf, get_whipper_drive_offset, store_whipit_offset
from whipit.inhibit import MediaDialogInhibitor, suppress_media_dialogs, unmount_cdda_device
from whipit.ripping.engine import RippingEngine
from whipit.ripping.identify import inspect_disc
from whipit.ripping.whipper import WhipperResult, WhipperRunner
from whipit.ui.base import ExtractionErrorAction, ExtractionErrorChoice, NullInteractionAdapter


def test_inhibit_stop_releases_pm_and_screensaver_cookies_via_qtdbus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))

    uninhibits: list[tuple[str, int]] = []

    class FakeBus:
        def isConnected(self) -> bool:
            return True

    class FakeInterface:
        def __init__(self, service: str, obj_path: str, interface: str, bus: FakeBus) -> None:
            self.service = service

        def isValid(self) -> bool:
            return True

        def call(self, method: str, *args: object) -> None:
            if method in ("UnInhibit", "Uninhibit"):
                uninhibits.append((self.service, int(args[0])))

    fake_qtdbus = types.ModuleType("PyQt6.QtDBus")
    fake_qtdbus.QDBusConnection = types.SimpleNamespace(sessionBus=lambda: FakeBus())  # type: ignore[attr-defined]
    fake_qtdbus.QDBusInterface = FakeInterface  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyQt6.QtDBus", fake_qtdbus)

    inh = MediaDialogInhibitor()
    inh.pm_cookie = 42
    inh.ss_cookie = 99
    assert inh.stop() is True
    assert ("org.freedesktop.PowerManagement.Inhibit", 42) in uninhibits
    assert ("org.freedesktop.ScreenSaver", 99) in uninhibits


def test_suppress_media_dialogs_clears_shared_inhibitor_if_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import whipit.inhibit as inh_mod

    class FailingInhibitor:
        def __init__(self, dev: str | None = None) -> None:
            pass

        def start(self, ui=None) -> None:
            raise RuntimeError("Simulated persistence failure")

    monkeypatch.setattr(inh_mod, "MediaDialogInhibitor", FailingInhibitor)
    with pytest.raises(RuntimeError, match="Simulated persistence failure"):
        with suppress_media_dialogs("/dev/sr0"):
            pass
    assert inh_mod._shared_inhibitor is None
    assert inh_mod._inhibit_ref_count == 0


def test_unmount_cdda_device_only_ejects_cdda_virtual_mounts_and_tolerates_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmds: list[list[str]] = []

    def mock_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        cmds.append(cmd)
        if cmd[0] == "pkill":
            raise subprocess.TimeoutExpired(cmd, 4.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)
    unmount_cdda_device("/dev/sr0")

    assert ["gio", "mount", "-u", "/dev/sr0"] in cmds
    assert ["gio", "mount", "-e", "/dev/sr0"] not in cmds
    assert ["gio", "mount", "-e", "cdda://sr0/"] in cmds


def test_whipper_runner_drains_trailing_lines_and_closes_log_fd_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir()
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (tmp_bin / "cd-paranoia").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_bin / "cd-paranoia").chmod(0o755)
    whipper_bin = tmp_bin / "whipper"
    whipper_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('AccurateRip summary: All tracks accurately ripped\\n')\n"
        "sys.stdout.write('Health status: No errors occurred\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    whipper_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    events: list[object] = []
    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0", event_callback=events.append)
    res = runner.run_rip(stage_dir)
    assert res.returncode == 0
    assert "AccurateRip summary: All tracks accurately ripped" in res.stdout
    assert "Health status: No errors occurred" in res.log_file.read_text(encoding="utf-8")

    # Verify log_fd does not leak if full_log_path fails
    bad_full_log = tmp_path / "blocked" / "full.log"
    (tmp_path / "blocked").write_text("not a dir", encoding="utf-8")
    runner2 = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0", full_log_path=bad_full_log)
    with pytest.raises(OSError):
        runner2.run_rip(stage_dir)


def test_engine_caps_repeated_toc_read_error_without_infinite_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    attempts = 0

    class FailingTocRunner:
        def __init__(self, *a: object, **kw: object) -> None:
            self.target_device = "/dev/sr0"
            self.speed = None

        def cleanup_wrapper(self) -> None:
            pass

        def run_rip(self, *a: object, **kw: object) -> WhipperResult:
            nonlocal attempts
            attempts += 1
            return WhipperResult(1, "", "", tmp_path / "1.log", False, False, False, False, False, toc_read_error=True)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", FailingTocRunner)

    prompts = 0

    class AbortAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail):
            nonlocal prompts
            prompts += 1
            return ExtractionErrorChoice(ExtractionErrorAction.ABORT)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=AbortAdapter())
    res = engine.run_extraction()
    assert res.success is False
    assert attempts == 3
    assert prompts == 1


def test_inspect_disc_skips_musicbrainz_queries_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_evt = threading.Event()
    mb_calls: list[str] = []

    class CancellingStdout:
        def __init__(self) -> None:
            self._lines = ["MusicBrainz disc id 9V8z1c\n"]

        def readline(self) -> str:
            if self._lines:
                return self._lines.pop(0)
            cancel_evt.set()
            return ""

        def close(self) -> None:
            pass

    class MockProc:
        def __init__(self, *a: object, **kw: object) -> None:
            self.stdout = CancellingStdout()
            self.returncode = 0
            self.pid = 4321

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.identify._terminate_process_group", lambda proc: None)
    monkeypatch.setattr("subprocess.Popen", MockProc)
    monkeypatch.setattr("whipit.net.fetch_bounded", lambda url, *a, **kw: mb_calls.append(url))

    res = inspect_disc("/dev/sr0", cancel_event=cancel_evt)
    assert res.success is False
    assert mb_calls == []


def test_store_whipit_offset_concurrent_and_bare_drive_section_rejected(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "whipit.config.get_drive_hardware_id",
        lambda dev: "DRIVE_0" if dev == "/dev/sr0" else "DRIVE_1",
    )

    errors: list[Exception] = []

    def writer(dev: str, val: int) -> None:
        try:
            for _ in range(10):
                store_whipit_offset(dev, val)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer, args=("/dev/sr0", 6))
    t2 = threading.Thread(target=writer, args=("/dev/sr1", 102))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert errors == []
    offsets_text = (get_config_dir() / "offsets").read_text(encoding="utf-8")
    assert "DRIVE_0=6" in offsets_text
    assert "DRIVE_1=102" in offsets_text

    # Bare [drive] section with vendor/model mismatch must not match "TEST_OPTICAL_DRIVE"
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "TEST_OPTICAL_DRIVE")
    monkeypatch.setattr("whipit.config.find_optical_drives", lambda: ["/dev/sr0", "/dev/sr1"])
    conf = get_whipper_conf()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[drive]\nread_offset = 999\n", encoding="utf-8")
    assert get_whipper_drive_offset("/dev/sr0") is None
