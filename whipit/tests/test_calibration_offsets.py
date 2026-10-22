import threading
from pathlib import Path
import pytest
from whipit.config import (
    get_stored_drive_offset,
    get_whipper_conf,
    get_whipper_drive_offset,
    store_drive_offset,
    store_whipper_offset,
)
from whipit.ripping.calibration import (
    CalibrationAction,
    CalibrationChoice,
    CalibrationResult,
    CalibrationStatus,
    _run_whipper_streaming,
    calibrate_drive,
)
from whipit.ripping.engine import RippingEngine
from whipit.ripping.whipper import WhipperResult, WhipperRunner
from whipit.ui.base import DiscPromptAction, DiscPromptResult, NullInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


def test_calibration_manual_offset_skips_drive_analyze(monkeypatch):
    analyzed = []
    monkeypatch.setattr("whipit.ripping.calibration.store_drive_offset", lambda dev, off: None)
    monkeypatch.setattr("whipit.ripping.calibration._run_whipper_streaming", lambda cmd, *a, **kw: analyzed.append(cmd))

    class MockAdapter(NullInteractionAdapter):
        def prompt_calibration_start(self, dev):
            return CalibrationChoice(CalibrationAction.MANUAL_OFFSET, manual_offset=12)

    res = calibrate_drive("/dev/sr0", ui=MockAdapter())
    assert res.status == CalibrationStatus.SUCCESS
    assert res.offset == 12
    assert len(analyzed) == 0


def test_calibration_cancellation_aborts_immediately(monkeypatch):
    cancel_evt = threading.Event()
    cancel_evt.set()

    executed = []
    monkeypatch.setattr("whipit.ripping.calibration._run_whipper_streaming", lambda cmd, *a, **kw: executed.append(cmd))

    class MockAdapter(NullInteractionAdapter):
        def prompt_calibration_start(self, dev):
            return CalibrationChoice(CalibrationAction.AUTO_DETECT)

    res = calibrate_drive("/dev/sr0", ui=MockAdapter(), cancel_event=cancel_evt)
    assert res.status == CalibrationStatus.ABORTED
    assert res.offset is None
    assert len(executed) == 0


def test_calibration_streaming_silent_hang_cancellation(monkeypatch):
    import time

    class BlockingStdout:
        def readline(self):
            time.sleep(10.0)
            return ""
        def close(self):
            pass

    class HangingProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = BlockingStdout()
            self.returncode = None
            self.pid = 88888
        def poll(self):
            return self.returncode
        def terminate(self):
            self.returncode = -15
        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", HangingProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)

    cancel_evt = threading.Event()
    cancel_evt.set()

    rc = _run_whipper_streaming(["whipper", "offset", "find"], ui=NullInteractionAdapter(), cancel_event=cancel_evt)
    assert rc == -1


def test_engine_unconfigured_offset_aborts_without_infinite_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)

    call_count = 0
    def mock_calibrate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return CalibrationResult(CalibrationStatus.ABORTED)

    monkeypatch.setattr("whipit.ripping.engine.calibrate_drive", mock_calibrate)

    class MockWhipperRunner:
        def __init__(self, *args, **kwargs):
            self.target_device = "/dev/sr0"
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *args, **kwargs):
            return WhipperResult(
                returncode=1,
                stdout="",
                stderr="offset unconfigured",
                log_file=tmp_path / "whipper.log",
                offset_unconfigured=True,
                is_cdr=False,
                network_error=False,
                metadata_missing=False,
                no_cd=False,
                cancelled=False,
            )

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockWhipperRunner)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0")
    result = engine.run_extraction()

    assert result.success is False
    assert result.cancelled is True
    assert call_count == 1


def test_manual_offset_zero_preserved(sample_manifest, temp_env, monkeypatch):
    import whipit.workflow
    monkeypatch.setattr(whipit.workflow, "get_stored_drive_offset", lambda dev: 6)
    coord = RipWorkflowCoordinator(device="/dev/sr0", manual_offset=0)
    assert coord.manifest.device.read_offset == 0


def test_get_stored_drive_offset_reads_whipper_conf_and_syncs(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "HL-DT-ST_BD-RE_WH16NS40")
    whipper_conf = get_whipper_conf()
    whipper_conf.parent.mkdir(parents=True, exist_ok=True)
    whipper_conf.write_text(
        "[drive:HL-DT-ST_BD-RE_WH16NS40]\nvendor = HL-DT-ST\nmodel = BD-RE WH16NS40\nread_offset = 6\n",
        encoding="utf-8",
    )

    offset = get_stored_drive_offset("/dev/sr0")
    assert offset == 6

    offsets_file = temp_env["config"] / "whipit" / "offsets"
    assert offsets_file.exists()
    assert "6" in offsets_file.read_text()


def test_store_drive_offset_writes_both_stores(temp_env):
    store_drive_offset("/dev/sr0", 667)

    offsets_file = temp_env["config"] / "whipit" / "offsets"
    assert offsets_file.exists()
    assert "667" in offsets_file.read_text()

    whipper_conf = get_whipper_conf()
    assert whipper_conf.exists()
    assert "read_offset = 667" in whipper_conf.read_text()


def test_calibration_auto_detect_populates_offset_from_whipper_conf(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "generic")

    def mock_whipper_offset_find(cmd, ui, cancel_event, dev_path=None):
        whipper_conf = get_whipper_conf()
        whipper_conf.parent.mkdir(parents=True, exist_ok=True)
        whipper_conf.write_text("[drive:generic]\nread_offset = 12\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("whipit.ripping.calibration._run_whipper_streaming", mock_whipper_offset_find)

    class AutoDetectAdapter(NullInteractionAdapter):
        def prompt_calibration_start(self, dev):
            return CalibrationChoice(CalibrationAction.AUTO_DETECT)

    res = calibrate_drive("/dev/sr0", ui=AutoDetectAdapter())
    assert res.status == CalibrationStatus.SUCCESS
    assert res.offset == 12
    assert get_stored_drive_offset("/dev/sr0") == 12


def test_get_whipper_drive_offset_rejects_different_drive(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "ASUS_BW-16D1HT")
    monkeypatch.setattr("whipit.config.find_optical_drives", lambda: ["/dev/sr0", "/dev/sr1"])
    whipper_conf = get_whipper_conf()
    whipper_conf.parent.mkdir(parents=True, exist_ok=True)
    whipper_conf.write_text(
        "[drive:HL-DT-ST_BD-RE_WH16NS40]\nvendor = HL-DT-ST\nmodel = BD-RE WH16NS40\nread_offset = 6\n",
        encoding="utf-8",
    )

    offset = get_whipper_drive_offset("/dev/sr0")
    assert offset is None


def test_store_whipper_offset_does_not_overwrite_other_drive_section(temp_env, monkeypatch):
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "ASUS_BW-16D1HT")
    whipper_conf = get_whipper_conf()
    whipper_conf.parent.mkdir(parents=True, exist_ok=True)
    whipper_conf.write_text(
        "[drive:HL-DT-ST_BD-RE_WH16NS40]\nvendor = HL-DT-ST\nmodel = BD-RE WH16NS40\nread_offset = 6\n",
        encoding="utf-8",
    )

    store_whipper_offset("/dev/sr1", 667)
    content = whipper_conf.read_text(encoding="utf-8")
    assert "[drive:HL-DT-ST_BD-RE_WH16NS40]" in content
    assert "read_offset = 6" in content
    assert "[drive:ASUS_BW-16D1HT]" in content
    assert "read_offset = 667" in content


def test_engine_prompt_insert_disc_retry_does_not_eject(tmp_path, monkeypatch):
    ejected = []
    closed = []
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.get_drives_with_hardware_ids", lambda: [("/dev/sr0", "TEST_DRIVE")])
    monkeypatch.setattr("whipit.ripping.engine.get_drive_hardware_id", lambda dev: "TEST_DRIVE")

    class MockAdapter(NullInteractionAdapter):
        def prompt_insert_disc(self, dev, hw, **kw):
            return DiscPromptResult(DiscPromptAction.RETRY)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=MockAdapter())
    runner = WhipperRunner(tmp_path, "/dev/sr0")

    should_cont, _ = engine._handle_insert_disc_prompt(runner, effective_offset=6)
    assert should_cont is True
    assert len(ejected) == 0
    assert len(closed) == 1


def test_engine_prompt_insert_disc_eject_ejects_tray(tmp_path, monkeypatch):
    ejected = []
    closed = []
    reloaded = []
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.get_drives_with_hardware_ids", lambda: [("/dev/sr0", "TEST_DRIVE")])
    monkeypatch.setattr("whipit.ripping.engine.get_drive_hardware_id", lambda dev: "TEST_DRIVE")

    class MockAdapter(NullInteractionAdapter):
        def prompt_insert_disc(self, dev, hw, **kw):
            return DiscPromptResult(DiscPromptAction.EJECT)
        def wait_for_user_reload(self, dev, msg):
            reloaded.append(dev)
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=MockAdapter())
    runner = WhipperRunner(tmp_path, "/dev/sr0")

    should_cont, _ = engine._handle_insert_disc_prompt(runner, effective_offset=6)
    assert should_cont is True
    assert len(ejected) == 1
    assert len(reloaded) == 1
    assert len(closed) == 1
