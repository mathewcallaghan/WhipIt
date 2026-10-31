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
