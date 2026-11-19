import os
import sys
import time
from pathlib import Path

from whipit.config import (
    get_whipper_conf,
    get_whipper_defeats_cache,
    is_usb_drive,
    store_whipper_defeats_cache,
)
from whipit.ripping.whipper import DriveStallEvent, WhipperRunner


def test_is_usb_drive_detects_usb_sysfs(monkeypatch):
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self: Path("/sys/devices/pci0000:00/0000:00:14.0/usb1/1-2/1-2:1.0/host0/target0:0:0/0:0:0:0/block/sr0"),
    )
    assert is_usb_drive("/dev/sr0") is True

    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self: Path("/sys/devices/pci0000:00/0000:00:17.0/ata1/host1/target1:0:0/1:0:0:0/block/sr0"),
    )
    assert is_usb_drive("/dev/sr0") is False


def test_store_and_get_whipper_defeats_cache(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "HL-DT-ST_BD-RE_WH16NS40")
    conf = get_whipper_conf()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[drive:HL-DT-ST_BD-RE_WH16NS40]\nread_offset = 6\ndefeats_cache = True\n", encoding="utf-8")

    assert get_whipper_defeats_cache("/dev/sr0") is True

    store_whipper_defeats_cache("/dev/sr0", False)
    assert get_whipper_defeats_cache("/dev/sr0") is False

    text = conf.read_text(encoding="utf-8")
    assert "defeats_cache = False" in text
    assert "read_offset = 6" in text


def test_get_whipper_defeats_cache_multi_drive_matching(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.find_optical_drives", lambda: ["/dev/sr0", "/dev/sr1"])
    conf = get_whipper_conf()
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(
        "[drive:FIRST_DRIVE]\nvendor = FIRST\nmodel = DRIVE\ndefeats_cache = True\n\n"
        "[drive:SECOND_DRIVE]\nvendor = SECOND\nmodel = DRIVE\ndefeats_cache = False\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "SECOND_DRIVE" if dev == "/dev/sr1" else "FIRST_DRIVE")
    assert get_whipper_defeats_cache("/dev/sr1") is False
    assert get_whipper_defeats_cache("/dev/sr0") is True

    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "THIRD_UNCONFIGURED_DRIVE")
    assert get_whipper_defeats_cache("/dev/sr2") is None


def test_watchdog_detects_stall_and_terminates(tmp_path, monkeypatch):
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    fake_whipper = tmp_bin / "whipper"
    script = (
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "print('INITIALIZING', flush=True)\n"
        "time.sleep(10)\n"
    )
    fake_whipper.write_text(script, encoding="utf-8")
    fake_whipper.chmod(0o755)

    dummy_paranoia = tmp_bin / "cd-paranoia"
    dummy_paranoia.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy_paranoia.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    events = []
    runner = WhipperRunner(
        tmp_path / "cache",
        target_device="/dev/sr97",
        stall_warn_seconds=0.2,
        stall_timeout_seconds=0.6,
        event_callback=events.append,
    )

    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    res = runner.run_rip(stage_dir)
    elapsed = time.time() - t0

    assert res.stalled is True
    assert elapsed < 3.0
    assert any(isinstance(e, DriveStallEvent) for e in events)


def test_whipper_runner_default_stall_timeouts(tmp_path):
    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")
    assert runner.stall_warn_seconds == 30.0
    assert runner.stall_timeout_seconds == 60.0


def test_whipper_runner_records_stalled_track_on_timeout(tmp_path, monkeypatch):
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    fake_whipper = tmp_bin / "whipper"
    fake_whipper.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "print('INFO:whipper.command.cd:ripping track 3 of 10: 03. Song.flac', flush=True)\n"
        "print('Reading track 3 of 10:  12 %', flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake_whipper.chmod(0o755)
    dummy_paranoia = tmp_bin / "cd-paranoia"
    dummy_paranoia.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy_paranoia.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    runner = WhipperRunner(
        tmp_path / "cache",
        target_device="/dev/sr97",
        stall_warn_seconds=0.15,
        stall_timeout_seconds=0.45,
        event_callback=None,
    )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    res = runner.run_rip(stage_dir)
    assert res.stalled is True
    assert res.stalled_track == 3
    assert "track 3" in (res.error_reason or "")


def test_whipper_runner_skip_tracks_fast_fails_blacklisted_track_only(tmp_path, monkeypatch):
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)

    paranoia_calls = tmp_path / "paranoia_calls.log"
    fake_paranoia = tmp_bin / "cd-paranoia"
    fake_paranoia.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(paranoia_calls)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_paranoia.chmod(0o755)

    fake_whipper = tmp_bin / "whipper"
    fake_whipper.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "assert subprocess.run(['cd-paranoia', '-V'], check=False).returncode == 0\n"
        "print('INFO:whipper.command.cd:ripping track 1 of 2: 01. Track.flac', flush=True)\n"
        "rc1 = subprocess.run(['cd-paranoia', '-d', '/dev/sr0', '[0]-[100]'], check=False).returncode\n"
        "assert rc1 != 0, 'blacklisted track 1 should fail fast in wrapper'\n"
        "print('INFO:whipper.command.cd:ripping track 2 of 2: 02. Track.flac', flush=True)\n"
        "rc2 = subprocess.run(['cd-paranoia', '-d', '/dev/sr0', '[101]-[200]'], check=False).returncode\n"
        "assert rc2 == 0, 'non-blacklisted track 2 should reach real cd-paranoia'\n",
        encoding="utf-8",
    )
    fake_whipper.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    res = runner.run_rip(stage_dir, keep_going=True, skip_tracks={1})
    assert res.returncode == 0
    logged_calls = paranoia_calls.read_text(encoding="utf-8").splitlines()
    assert "-V" in logged_calls
    assert "-d /dev/sr0 [101]-[200]" in logged_calls
    assert "-d /dev/sr0 [0]-[100]" not in logged_calls


def test_watchdog_stall_warning_includes_track_number(tmp_path, monkeypatch):
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    fake_whipper = tmp_bin / "whipper"
    fake_whipper.write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "print('INFO:whipper.command.cd:ripping track 2 of 8: 02. Track.flac', flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake_whipper.chmod(0o755)
    dummy_paranoia = tmp_bin / "cd-paranoia"
    dummy_paranoia.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dummy_paranoia.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    events = []
    runner = WhipperRunner(
        tmp_path / "cache",
        target_device="/dev/sr97",
        stall_warn_seconds=0.15,
        stall_timeout_seconds=0.45,
        event_callback=events.append,
    )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    res = runner.run_rip(stage_dir)
    assert res.stalled is True
    stall_events = [e for e in events if isinstance(e, DriveStallEvent)]
    assert len(stall_events) >= 1
    assert stall_events[0].track_number == 2


def test_engine_and_real_runner_stall_on_track1_keep_going_completes_track2(tmp_path, monkeypatch):
    from whipit.ripping.engine import RippingEngine
    from whipit.ui.base import ExtractionErrorAction, ExtractionErrorChoice, NullInteractionAdapter

    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir(parents=True, exist_ok=True)
    paranoia_log = tmp_path / "paranoia_invocations.log"

    fake_paranoia = tmp_bin / "cd-paranoia"
    fake_paranoia.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"with open({str(paranoia_log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_paranoia.chmod(0o755)

    fake_whipper = tmp_bin / "whipper"
    fake_whipper.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys, time\n"
        "if '--keep-going' not in sys.argv:\n"
        "    print('INFO:whipper.command.cd:ripping track 1 of 2: 01. Stalled.flac', flush=True)\n"
        "    time.sleep(10)\n"
        "print('INFO:whipper.command.cd:ripping track 1 of 2: 01. Stalled.flac', flush=True)\n"
        "rc1 = subprocess.run(['cd-paranoia', '-d', '/dev/sr0', '[0]-[100]'], check=False).returncode\n"
        "assert rc1 != 0\n"
        "print('CRITICAL:whipper.command.cd:giving up on track 1 after 1 times', flush=True)\n"
        "print('INFO:whipper.command.cd:ripping track 2 of 2: 02. Clean.flac', flush=True)\n"
        "rc2 = subprocess.run(['cd-paranoia', '-d', '/dev/sr0', '[101]-[200]'], check=False).returncode\n"
        "assert rc2 == 0\n"
        "pathlib.Path('02. Clean.flac').write_bytes(b'FLAC_T2')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_whipper.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.is_usb_drive", lambda dev: False)
    monkeypatch.setattr("whipit.ripping.engine.disable_usb_autosuspend", lambda dev: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    orig_runner = WhipperRunner
    monkeypatch.setattr(
        "whipit.ripping.engine.WhipperRunner",
        lambda *a, **kw: orig_runner(*a, **{**kw, "stall_warn_seconds": 0.1, "stall_timeout_seconds": 0.35}),
    )

    prompts = []

    class KeepGoingOnceAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            prompts.append(kw)
            assert len(prompts) == 1
            return ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING)

    stage_dir = tmp_path / "stage"
    engine = RippingEngine(
        cache_dir=tmp_path / "cache",
        stage_dir=stage_dir,
        device_path="/dev/sr0",
        auto_eject=False,
        ui=KeepGoingOnceAdapter(),
    )
    res = engine.run_extraction()

    assert res.success is True
    assert len(prompts) == 1
    assert prompts[0]["is_hardware_stall"] is True
    assert (stage_dir / "02. Clean.flac").exists()
    invocations = paranoia_log.read_text(encoding="utf-8").splitlines()
    assert not any("[0]-[100]" in line for line in invocations)
    assert "-S 4 -d /dev/sr0 [101]-[200]" in invocations
