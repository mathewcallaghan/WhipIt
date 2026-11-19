"""Automated tests validating read-error recovery, drive switching, and reload prompt lifecycles."""

import subprocess

import pytest

from whipit.config import (
    CDS_AUDIO,
    CDS_DATA_1,
    CDS_DISC_OK,
    CDS_DRIVE_NOT_READY,
    CDS_NO_DISC,
    CDS_NO_INFO,
    CDS_TRAY_OPEN,
    drive_has_disc,
    get_disc_status,
)
from whipit.models import AlbumMetadata, TrackInfo, WorkflowResult
from whipit.ripping.engine import ExtractionResult, RippingEngine
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.ripping.whipper import WhipperResult
from whipit.ui.base import (
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
    NullInteractionAdapter,
)
from whipit.workflow import RipWorkflowCoordinator


def test_retry_slow_4x_closes_tray_and_proceeds(tmp_path, monkeypatch):
    """Verifies that selecting RETRY_SLOW_4X does not drop session state and attempts extraction at 4x."""
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)

    closed_trays = []
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed_trays.append(dev))

    attempts = 0

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return WhipperResult(1, "", "read error", tmp_path / "1.log", False, False, False, False, False)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class PromptAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail):
            return ExtractionErrorChoice(ExtractionErrorAction.RETRY_SLOW_4X)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=PromptAdapter())
    res = engine.run_extraction()

    assert res.success is True
    assert attempts == 2
    assert engine.speed == 4
    assert len(closed_trays) >= 1


def test_switch_drive_ejects_both_prompts_reload_and_continues(tmp_path, monkeypatch):
    """Verifies that SWITCH_DRIVE ejects both drives, waits for disc insertion, and resumes on the target drive."""
    ejected = []
    closed = []
    reloaded = []

    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 102 if dev == "/dev/sr1" else 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    attempts = []

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            attempts.append(self.target_device)
            if len(attempts) == 1:
                return WhipperResult(1, "", "hardware error", tmp_path / "1.log", False, False, False, False, False)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class SwitchAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            reloaded.append(dev)
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=SwitchAdapter())
    res = engine.run_extraction()

    assert res.success is True
    assert attempts == ["/dev/sr0", "/dev/sr1"]
    assert ejected == ["/dev/sr0", "/dev/sr1", "/dev/sr1"]
    assert reloaded == ["/dev/sr1"]
    assert "/dev/sr1" in closed
    assert res.device_path == "/dev/sr1"
    assert res.effective_offset == 102


def test_cdrdao_read_toc_error_heals_and_retries(tmp_path, monkeypatch):
    """Verifies that Whipper's known cdrdao FileNotFoundError spin-up race is caught and retried automatically."""
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    attempts = 0

    class MockRunner:
        def __init__(self, *a, **kw):
            self.target_device = "/dev/sr0"
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return WhipperResult(
                    1, "", "", tmp_path / "1.log",
                    False, False, False, False, False,
                    toc_read_error=True
                )
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0")
    res = engine.run_extraction()

    assert res.success is True
    assert attempts == 2


def test_switch_drive_user_cancellation_aborts_without_orphan_locks(tmp_path, monkeypatch):
    """Verifies that cancelling at the disc reload dialog cleanly stops extraction."""
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)

    class MockRunner:
        def __init__(self, *a, **kw):
            self.target_device = "/dev/sr0"
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            return WhipperResult(1, "", "err", tmp_path / "1.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class CancelSwitchAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            return False

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=CancelSwitchAdapter())
    res = engine.run_extraction()

    assert res.success is False
    assert res.cancelled is True


def test_workflow_skips_redundant_inspect_disc_when_metadata_already_known(temp_env, monkeypatch):
    """Assures the pipeline does not execute a 60-120s inspect_disc redundant TOC scan if metadata is already populated."""
    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        initial_metadata=AlbumMetadata(artist="Radiohead", album="OK Computer", musicbrainz_id="mock-mbid"),
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    inspect_calls = []
    monkeypatch.setattr("whipit.ripping.identify.inspect_disc", lambda dev, **kw: inspect_calls.append(dev))

    class MockEngine:
        def run_extraction(self, **kw):
            return ExtractionResult(
                success=True,
                partial=False,
                device_path="/dev/sr0",
                effective_offset=6,
                speed=None,
            )

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    (coord.whipper_stage / "01.flac").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "OK Computer")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert len(inspect_calls) == 0


def test_drive_panel_handle_status_ignores_polls_during_active_rip():
    """Verifies that transient ioctl poll responses during ripping cannot mark disc as empty or reset badges."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    class FakeRunningWorker:
        def isRunning(self):
            return True

    panel.worker = FakeRunningWorker()
    panel.session.is_disc_loaded = True
    panel._disc_was_ejected = False

    panel.handle_drive_status(CDS_NO_INFO, 0)
    assert panel.session.is_disc_loaded is True
    assert panel._disc_was_ejected is False

    panel.handle_drive_status(CDS_TRAY_OPEN, 0)
    assert panel.session.is_disc_loaded is True
    assert panel._disc_was_ejected is False


def test_drive_has_disc_wait_ready_with_cds_no_info_returns_true_immediately(monkeypatch):
    """Contract test: drives returning CDS_NO_INFO on CDROM_DISC_STATUS must return True with wait_ready=True."""
    monkeypatch.setattr("whipit.config.get_disc_status", lambda dev: (CDS_DISC_OK, CDS_NO_INFO))
    assert drive_has_disc("/dev/sr0", wait_ready=True, timeout=8.0) is True
    assert drive_has_disc("/dev/sr0", wait_ready=False) is True


def test_drive_has_disc_recovers_after_drive_spinning_up(monkeypatch):
    """Contract test: drive transitioning from CDS_DRIVE_NOT_READY -> CDS_DISC_OK succeeds when wait_ready=True."""
    calls = 0
    def mock_status(dev):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return (CDS_DRIVE_NOT_READY, CDS_NO_INFO)
        return (CDS_DISC_OK, CDS_AUDIO)

    monkeypatch.setattr("whipit.config.get_disc_status", mock_status)
    assert drive_has_disc("/dev/sr0", wait_ready=True, timeout=5.0) is True
    assert calls >= 3


def test_drive_has_disc_rejects_empty_tray_and_data_discs(monkeypatch):
    """Contract test: empty trays and data discs are reliably rejected."""
    monkeypatch.setattr("whipit.config.get_disc_status", lambda dev: (CDS_TRAY_OPEN, CDS_NO_INFO))
    assert drive_has_disc("/dev/sr0", wait_ready=False) is False

    monkeypatch.setattr("whipit.config.get_disc_status", lambda dev: (CDS_DISC_OK, CDS_DATA_1))
    assert drive_has_disc("/dev/sr0", wait_ready=False, audio_only=True) is False


def test_get_disc_status_handles_enomedium_as_cds_no_disc(monkeypatch):
    """Contract test: os.open raising ENOMEDIUM (123) is translated to CDS_NO_DISC."""
    def mock_open(*a, **kw):
        raise OSError(123, "No medium found")

    monkeypatch.setattr("os.open", mock_open)
    drive_st, disc_st = get_disc_status("/dev/sr0")
    assert drive_st == CDS_NO_DISC


def test_disc_insertion_triggers_auto_load_and_enables_start_rip(monkeypatch):
    """Verifies that inserting a disc transitions state and automatically starts disc identification."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    panel.handle_drive_status(CDS_NO_DISC, CDS_NO_INFO)
    assert panel._disc_was_ejected is True
    assert panel.session.is_disc_loaded is False
    assert panel.start_btn.isEnabled() is False

    identified = []
    monkeypatch.setattr(panel, "identify_disc", lambda silent=True: identified.append(silent))

    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert len(identified) == 1
    assert panel.session.is_disc_loaded is True
    assert panel._disc_was_ejected is False

    res = DiscInspectionResult(
        success=True,
        artist="The Beatles",
        title="Abbey Road",
        tracks=[TrackInfo(number=1, title="Come Together", duration="04:20")],
    )
    panel._on_identify_finished(res)
    assert panel.start_btn.isEnabled() is True


def test_eject_drive_clears_stale_metadata_and_resets_state(monkeypatch):
    """Verifies that clicking Eject clears old disc metadata, hides track table, and resets state."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel, DriveState

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    panel.session.artist = "Previous Artist"
    panel.session.title = "Previous Album"
    panel.session.tracks = [TrackInfo(number=1, title="Old Track", duration="03:00")]
    panel.session.is_disc_loaded = True
    panel.transition_to(DriveState.READY)

    panel.eject_drive()
    assert panel.drive_state == DriveState.EMPTY
    assert panel.session.is_disc_loaded is False
    assert panel.session.artist == "Unknown Artist"
    assert panel.track_table.isVisible() is False
    assert panel.start_btn.isEnabled() is False
    assert panel.identify_btn.isEnabled() is True


def test_inspect_disc_terminates_early_when_table_reading_starts(monkeypatch):
    sample_output = (
        "CDDB disc id: c009560d\n"
        "MusicBrainz disc id 5cVTKwEtQPTiqT4A3ktiYJ14WzQ-\n"
        "MusicBrainz lookup URL https://musicbrainz.org/attach\n"
        "Disc duration: 00:39:50.466, 13 audio tracks\n"
        "Matching releases:\n"
        "Artist  : The Beatles\n"
        "Title   : Sgt. Pepper’s Lonely Hearts Club Band\n"
        "Release : https://musicbrainz.org/release/ce681fec-ca96-3acc-ae66-af17f09f05a7\n\n"
        "Reading table   0 %\n"
        "Track 1 finished, found 79 Q sub-channels with CRC errors\n"
    )
    killed = []

    class MockStreamingProc:
        def __init__(self, *a, **kw):
            import io
            self.stdout = io.StringIO(sample_output)
            self.returncode = 0
            self.pid = 4321
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.identify._terminate_process_group", lambda proc: killed.append(proc.pid))
    monkeypatch.setattr("subprocess.Popen", MockStreamingProc)
    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_release_details",
        lambda *a, **kw: type("Details", (), {"tracks": [], "discogs_url": None, "disc_number": None, "total_discs": None, "artist": None, "title": None})(),
    )

    res = inspect_disc("/dev/sr0")
    assert res.success is True
    assert res.artist == "The Beatles"
    assert len(killed) >= 1


def test_eject_drive_recovers_when_returncode_nonzero_without_busy_string(monkeypatch):
    from whipit.config import eject_drive
    call_count = 0
    unmounted = []
    locks_released = []

    def mock_subprocess_run(cmd, *a, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="eject: unable to eject, last error: Inappropriate ioctl")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("whipit.inhibit.unmount_cdda_device", lambda dev: unmounted.append(dev))
    monkeypatch.setattr("whipit.config.release_drive_locks", lambda dev: locks_released.append(dev))
    monkeypatch.setattr("subprocess.run", mock_subprocess_run)

    eject_drive("/dev/sr0")
    assert call_count == 2
    assert "/dev/sr0" in unmounted
    assert "/dev/sr0" in locks_released


def test_inspect_disc_terminates_early_without_matching_releases(monkeypatch):
    """Verifies uncataloged discs without 'Matching releases:' terminate immediately upon table reading."""
    sample_output = (
        "CDDB disc id: f7113912\n"
        "MusicBrainz disc id A59uocvYUbLE81SCouKIZ2TDuNg-\n"
        "MusicBrainz lookup URL https://musicbrainz.org/cdtoc/attach?toc=1+10+206687\n"
        "Disc duration: 00:45:53.826, 10 audio tracks\n"
        "release not found: NotFoundException\n\n"
        "Reading table   0 %\n"
        "Track 1 finished\n"
    )
    killed = []

    class MockStreamingProc:
        def __init__(self, *a, **kw):
            import io
            self.stdout = io.StringIO(sample_output)
            self.returncode = 0
            self.pid = 9876
        def poll(self): return 0
        def wait(self, timeout=None): return 0

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.identify._terminate_process_group", lambda proc: killed.append(proc.pid))
    monkeypatch.setattr("subprocess.Popen", MockStreamingProc)

    res = inspect_disc("/dev/sr0")
    assert len(killed) >= 1
    assert res.duration == "45:53"
    assert res.tracks_count == 10


def test_eject_drive_escalates_to_udisksctl_when_eject_commands_fail(monkeypatch):
    """Ensures eject_drive falls back to udisksctl when standard and SCSI eject tools fail on USB bridges."""
    from whipit.config import eject_drive
    ran_cmds = []

    def mock_run(cmd, *a, **kw):
        ran_cmds.append(cmd)
        # Fail standard eject and SCSI eject, succeed on udisksctl
        if cmd[0] == "eject":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Inappropriate ioctl for device")
        if cmd[0] == "udisksctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="Ejected /dev/sr0.", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("whipit.inhibit.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.config.release_drive_locks", lambda dev: None)
    monkeypatch.setattr("shutil.which", lambda tool: "/usr/bin/udisksctl" if tool == "udisksctl" else None)
    monkeypatch.setattr("subprocess.run", mock_run)

    eject_drive("/dev/sr0")
    assert any(cmd == ["eject", "-s", "/dev/sr0"] for cmd in ran_cmds)
    assert any(cmd == ["udisksctl", "eject", "-b", "/dev/sr0"] for cmd in ran_cmds)


def test_switch_drive_resets_per_drive_state_and_notifies_ui(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 102 if dev == "/dev/sr1" else 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    (tmp_path / "stale_from_sr0.flac").write_bytes(b"STALE")
    seen_keep_going = []
    seen_cdr = []
    switched_events = []

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10
        def cleanup_wrapper(self):
            pass
        def run_rip(self, stage_dir, keep_going=False, cdr=False, **kw):
            seen_keep_going.append(keep_going)
            seen_cdr.append(cdr)
            if self.target_device == "/dev/sr0":
                return WhipperResult(1, "", "", tmp_path / "1.log", False, False, False, False, False)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class SwitchWithCdrAdapter(NullInteractionAdapter):
        def on_active_device_changed(self, old_device, new_device):
            switched_events.append((old_device, new_device))
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=SwitchWithCdrAdapter())
    res = engine.run_extraction(keep_going=True)

    assert res.success is True
    assert res.device_path == "/dev/sr1"
    assert switched_events == [("/dev/sr0", "/dev/sr1")]
    assert seen_keep_going == [True, False]
    assert seen_cdr == [False, False]
    assert not (tmp_path / "stale_from_sr0.flac").exists()


def test_false_cdr_declined_falls_through_to_extraction_error_switch(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            if self.target_device == "/dev/sr0":
                return WhipperResult(1, "", "", tmp_path / "1.log", False, True, False, False, False)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class DeclineCdrThenSwitchAdapter(NullInteractionAdapter):
        def prompt_cdr_detected(self, dev):
            return False
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=DeclineCdrThenSwitchAdapter())
    res = engine.run_extraction()
    assert res.success is True
    assert res.device_path == "/dev/sr1"


def test_re_is_cdr_does_not_match_bare_flag():
    from whipit.ripping.whipper import _RE_IS_CDR

    assert _RE_IS_CDR.search("CD-R detected: true")
    assert _RE_IS_CDR.search("CRITICAL:whipper.command.cd:inserted disc seems to be a CD-R, --cdr not passed.")
    assert not _RE_IS_CDR.search("CD-R detected: No")
    assert not _RE_IS_CDR.search("usage: whipper cd rip [--cdr]")


def test_whipper_runner_unlatches_traceback_on_retry_and_captures_giveup_reason(tmp_path, monkeypatch):
    import os
    import sys

    from whipit.ripping.whipper import WhipperRunner

    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir()
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    (tmp_bin / "cd-paranoia").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tmp_bin / "cd-paranoia").chmod(0o755)
    whipper_bin = tmp_bin / "whipper"
    whipper_bin.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('Traceback (most recent call last):', flush=True)\n"
        "print('RuntimeError: cdparanoia couldn\\'t read any frames', flush=True)\n"
        "print('INFO:whipper.command.cd:ripping track 1 of 2 (try 2): 01. Track.flac', flush=True)\n"
        "print('CRITICAL:whipper.command.cd:giving up on track 1 after 10 times', flush=True)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    whipper_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("whipit.ripping.whipper.release_drive_locks", lambda dev: None)

    streamed = []
    runner = WhipperRunner(tmp_path / "cache", target_device="/dev/sr0", output_callback=streamed.append)
    res = runner.run_rip(stage_dir)

    assert res.returncode == 1
    assert any("ripping track 1 of 2 (try 2)" in ln for ln in streamed)
    assert res.error_reason is not None
    assert "giving up on track 1 after 10 times" in res.error_reason


def test_workflow_releases_locks_on_switched_device_when_aborted(temp_env, monkeypatch):
    from whipit.models import WorkflowResult
    from whipit.workflow import RipWorkflowCoordinator

    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    released = []
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.workflow.release_drive_locks", lambda dev: released.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            return WhipperResult(1, "", "err", temp_env["cache"] / "1.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class SwitchThenCancelAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            return False

    coord.ui = SwitchThenCancelAdapter()
    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.device_path == "/dev/sr1"
    assert "/dev/sr1" in released


def test_switch_drive_restores_initial_speed_after_stall_downgrade(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    seen_speeds = []

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *a, **kw):
            seen_speeds.append((self.target_device, self.speed))
            if self.target_device == "/dev/sr0":
                return WhipperResult(1, "", "stalled", tmp_path / "1.log", False, False, False, False, False, stalled=True)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    class SwitchAfterStallAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")
        def wait_for_user_reload(self, dev, msg):
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", speed=8, ui=SwitchAfterStallAdapter())
    res = engine.run_extraction()
    assert res.success is True
    assert seen_speeds == [("/dev/sr0", 8), ("/dev/sr1", 8)]


def test_switch_drive_rejects_same_device_and_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)

    ejected = []
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, *a, **kw):
            return WhipperResult(1, "", "err", tmp_path / "1.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    warnings = []

    class SameDriveSwitchAdapter(NullInteractionAdapter):
        def on_log(self, level, message):
            if level == LogLevel.WARN:
                warnings.append(message)

        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr0")

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=SameDriveSwitchAdapter())
    res = engine.run_extraction()

    assert res.success is False
    assert res.cancelled is False
    assert res.device_path == "/dev/sr0"
    assert ejected == []
    assert any("Cannot switch to the same optical drive" in w for w in warnings)


def test_switch_drive_logs_warning_when_wait_for_disc_ready_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: False)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    class MockRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, *a, **kw):
            if self.target_device == "/dev/sr0":
                return WhipperResult(1, "", "err", tmp_path / "1.log", False, False, False, False, False)
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockRunner)

    warnings = []

    class SwitchReadyTimeoutAdapter(NullInteractionAdapter):
        def on_log(self, level, message):
            if level == LogLevel.WARN:
                warnings.append(message)

        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1")

        def wait_for_user_reload(self, dev, msg):
            return True

    engine = RippingEngine(
        cache_dir=tmp_path,
        stage_dir=tmp_path,
        device_path="/dev/sr0",
        ui=SwitchReadyTimeoutAdapter(),
    )
    res = engine.run_extraction()

    assert res.success is True
    assert res.device_path == "/dev/sr1"
    assert any("Optical drive /dev/sr1 is not ready yet" in w for w in warnings)


def test_engine_forwards_is_hardware_stall_to_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    class StallingRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, *a, **kw):
            return WhipperResult(
                1,
                "",
                "",
                tmp_path / "1.log",
                False,
                False,
                False,
                False,
                False,
                stalled=True,
                error_reason="Optical drive stopped responding (read stall).",
            )

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", StallingRunner)

    captured = []

    class StallInspectingAdapter(NullInteractionAdapter):
        def prompt_extraction_error(
            self,
            device,
            available_drives,
            error_reason=None,
            has_partial_tracks=True,
            is_hardware_stall=False,
        ):
            captured.append((device, error_reason, has_partial_tracks, is_hardware_stall))
            return ExtractionErrorChoice(ExtractionErrorAction.ABORT)

    engine = RippingEngine(
        cache_dir=tmp_path,
        stage_dir=tmp_path,
        device_path="/dev/sr0",
        ui=StallInspectingAdapter(),
    )
    res = engine.run_extraction()

    assert res.success is False
    assert captured == [
        ("/dev/sr0", "Optical drive stopped responding (read stall).", False, True),
    ]


def test_hardware_stall_dialog_and_panel_dispatch(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QRadioButton

    from whipit.ui.gui.dialogs import prompt_extraction_error_dialog
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])

    seen = []

    def spy_exec_multi(self):
        titles = [self.windowTitle()]
        labels = [lbl.text() for lbl in self.findChildren(QLabel)]
        checked = [rb.text() for rb in self.findChildren(QRadioButton) if rb.isChecked()]
        combos = [cb.isEnabled() for cb in self.findChildren(QComboBox)]
        seen.append((titles, labels, checked, combos))
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", spy_exec_multi)

    choice = prompt_extraction_error_dialog(
        None,
        "/dev/sr0",
        [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")],
        error_reason="Optical drive stopped responding (read stall).",
        has_partial_tracks=False,
        is_hardware_stall=True,
    )
    assert choice.action == ExtractionErrorAction.SWITCH_DRIVE
    assert choice.selected_device == "/dev/sr1"
    assert seen[0][0] == ["Drive Hardware Controller Lockup"]
    assert any("Drive Hardware Controller Lockup" in t for t in seen[0][1])
    assert seen[0][2] == ["Switch to Drive /dev/sr1 (Recommended):"]
    assert seen[0][3] == [True]

    # Single-drive stall selects Eject instead of disabled Switch
    seen.clear()
    choice_single = prompt_extraction_error_dialog(
        None,
        "/dev/sr0",
        [("/dev/sr0", "DRIVE_A")],
        error_reason="Optical drive stopped responding (read stall).",
        has_partial_tracks=False,
        is_hardware_stall=True,
    )
    assert choice_single.action == ExtractionErrorAction.EJECT_AND_RETRY
    assert seen[0][2] == ["Eject disc to inspect/clean, then retry"]

    # DrivePanel._dispatch_modal_prompt forwards 5-tuple is_hardware_stall flag
    panel = DrivePanel(device="/dev/sr0")
    dispatched = []
    monkeypatch.setattr(
        "whipit.ui.gui.drive_panel.prompt_extraction_error_dialog",
        lambda *a, **kw: (dispatched.append(kw), ExtractionErrorChoice(ExtractionErrorAction.ABORT))[1],
    )
    panel._dispatch_modal_prompt(
        "prompt_extraction_error",
        ("/dev/sr0", [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")], "Stall", False, True),
    )
    assert dispatched == [{"error_reason": "Stall", "has_partial_tracks": False, "is_hardware_stall": True}]


def test_keep_going_after_stall_blacklists_stalled_track_without_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    album_dir = tmp_path / "Artist - Album"
    album_dir.mkdir()
    zero_flac = album_dir / "01. Stalled.flac"
    zero_flac.write_bytes(b"")
    leftover_wav = album_dir / "01. Stalled.wav"
    leftover_wav.write_bytes(b"WAV")

    calls = []

    class StallThenRecoverRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, stage_dir, keep_going=False, skip_tracks=None, **kw):
            calls.append((keep_going, set(skip_tracks or ())))
            if len(calls) == 1:
                return WhipperResult(
                    1,
                    "",
                    "",
                    tmp_path / "1.log",
                    False,
                    False,
                    False,
                    False,
                    False,
                    stalled=True,
                    stalled_track=1,
                )
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", StallThenRecoverRunner)

    class KeepGoingAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=KeepGoingAdapter())
    res = engine.run_extraction()

    assert res.success is True
    assert calls == [(False, set()), (True, {1})]
    assert not zero_flac.exists()
    assert not leftover_wav.exists()


def test_retry_4x_and_switch_drive_do_not_leak_stalled_track_blacklist(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.wait_for_disc_ready", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("time.sleep", lambda s: None)

    seen_calls = []

    class MultiStepRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, stage_dir, keep_going=False, skip_tracks=None, **kw):
            seen_calls.append((self.target_device, keep_going, set(skip_tracks or ())))
            if len(seen_calls) == 1:
                return WhipperResult(
                    1, "", "", tmp_path / "1.log", False, False, False, False, False,
                    stalled=True, stalled_track=1,
                )
            if len(seen_calls) == 2:
                return WhipperResult(
                    1, "", "", tmp_path / "2.log", False, False, False, False, False,
                    stalled=True, stalled_track=1,
                )
            if len(seen_calls) == 3:
                return WhipperResult(
                    1, "", "", tmp_path / "3.log", False, False, False, False, False,
                    stalled=True, stalled_track=2,
                )
            return WhipperResult(0, "", "", tmp_path / "4.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MultiStepRunner)

    actions = [
        ExtractionErrorChoice(ExtractionErrorAction.RETRY_SLOW_4X),
        ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING),
        ExtractionErrorChoice(ExtractionErrorAction.SWITCH_DRIVE, selected_device="/dev/sr1"),
    ]

    class ScriptedAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return actions.pop(0)

        def wait_for_user_reload(self, dev, msg):
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=ScriptedAdapter())
    res = engine.run_extraction()

    assert res.success is True
    assert seen_calls == [
        ("/dev/sr0", False, set()),
        ("/dev/sr0", False, set()),
        ("/dev/sr0", True, {1}),
        ("/dev/sr1", False, set()),
    ]


def test_stall_caps_speed_to_4x_and_preserves_lower_speed_on_keep_going(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("whipit.ripping.engine.is_usb_drive", lambda dev: False)
    monkeypatch.setattr("time.sleep", lambda s: None)

    defeats_calls = []
    monkeypatch.setattr(
        "whipit.ripping.engine.store_whipper_defeats_cache",
        lambda dev, val: defeats_calls.append((dev, val)),
    )

    seen_speeds = []

    class SpeedCapturingRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, *a, **kw):
            seen_speeds.append(self.speed)
            if len(seen_speeds) % 2 == 1:
                return WhipperResult(
                    1, "", "", tmp_path / "1.log", False, False, False, False, False,
                    stalled=True, stalled_track=1,
                )
            return WhipperResult(0, "", "", tmp_path / "2.log", False, False, False, False, False)

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", SpeedCapturingRunner)

    class KeepGoingAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING)

    # Full speed (None) on SATA drive caps to 4x and persists defeats_cache=False
    engine_full = RippingEngine(
        cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", speed=None, ui=KeepGoingAdapter()
    )
    res_full = engine_full.run_extraction()
    assert res_full.success is True
    assert seen_speeds[:2] == [None, 4]
    assert ("/dev/sr0", False) in defeats_calls

    # Speed already < 4x (2x) is preserved instead of bumped up to 4x
    seen_speeds.clear()
    engine_2x = RippingEngine(
        cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", speed=2, ui=KeepGoingAdapter()
    )
    res_2x = engine_2x.run_extraction()
    assert res_2x.success is True
    assert seen_speeds == [2, 2]


def test_parse_cdrdao_toc_and_slice_raw_tracks_to_flac(tmp_path, monkeypatch):
    from pathlib import Path

    from whipit.ripping.raw import CD_FRAME_BYTES, RawSectorExtractor, parse_cdrdao_toc

    toc_file = tmp_path / "disc.toc"
    toc_file.write_text(
        "CD_ROM_XA\n"
        "// Track 1 Data\n"
        "TRACK MODE2_RAW\n"
        'DATAFILE "disc.bin" 00:01:00\n'
        "// Track 2 Audio\n"
        "TRACK AUDIO\n"
        "NO COPY\n"
        "SILENCE 00:02:00\n"
        'FILE "disc.bin" #4704 0 00:00:02\n'
        "// Track 3 Audio\n"
        "TRACK AUDIO\n"
        'FILE "disc.bin" #9408 00:00:03\n',
        encoding="utf-8",
    )

    slices = parse_cdrdao_toc(toc_file)
    assert len(slices) == 2
    assert slices[0].track_number == 2
    assert slices[0].segments == [(4704, 2 * CD_FRAME_BYTES)]
    assert slices[1].track_number == 3
    assert slices[1].segments == [(9408, 3 * CD_FRAME_BYTES)]

    # End-to-end raw extraction slicing with mocked cdrdao + flac + metaflac
    bin_path = tmp_path / "stage" / "raw_recovery.bin"
    toc_stage = tmp_path / "stage" / "raw_recovery.toc"
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()

    raw_payload = b"\x01\x02\x03\x04" * ((CD_FRAME_BYTES * 5) // 4)

    class FakeProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 0
            self.pid = 7777
            self.stdout = None
            self.stdin = None
            if cmd[0] == "cdrdao":
                import io
                bin_path.write_bytes(raw_payload)
                toc_stage.write_text(
                    "CD_DA\n"
                    "TRACK AUDIO\n"
                    'FILE "raw_recovery.bin" 0 00:00:02\n'
                    "TRACK AUDIO\n"
                    'FILE "raw_recovery.bin" 00:00:02 00:00:03\n',
                    encoding="utf-8",
                )
                self.stdout = io.StringIO("Reading raw tracks...\n")
            elif cmd[0] == "flac":
                import io
                self.stdin = io.BytesIO()
                out_idx = cmd.index("-o") + 1
                Path(cmd[out_idx]).write_bytes(b"FLAC_RAW_SLICE")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def communicate(self, timeout=None):
            return (b"", b"")

    written_tags = []
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("subprocess.Popen", FakeProc)
    monkeypatch.setattr(
        "whipit.ripping.raw.write_flac_tags",
        lambda path, tags: (written_tags.append((path.name, dict(tags))), True)[1],
    )

    extractor = RawSectorExtractor("/dev/sr0")
    res = extractor.run_raw_extraction(stage_dir)

    assert res.success is True
    assert [p.name for p in res.flac_files] == ["01.flac", "02.flac"]
    assert res.cue_path is not None and res.cue_path.exists()
    assert res.log_path is not None and res.log_path.exists()
    assert "Unverified (Raw sector recovery)" in res.log_path.read_text(encoding="utf-8")
    assert not bin_path.exists()
    assert not toc_stage.exists()
    assert written_tags == [
        ("01.flac", {"TRACKNUMBER": "1", "WARNING": "Extracted via raw sector recovery pass"}),
        ("02.flac", {"TRACKNUMBER": "2", "WARNING": "Extracted via raw sector recovery pass"}),
    ]


def test_engine_raw_sector_fallback_recovers_after_stall(tmp_path, monkeypatch):
    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.reset_optical_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ripping.engine.store_whipper_defeats_cache", lambda *a, **kw: None)
    monkeypatch.setattr("whipit.ripping.engine.repair_whipper_conf", lambda dev: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    class StallingRunner:
        def __init__(self, cache_dir, target_device, speed=None, **kw):
            self.target_device = target_device
            self.speed = speed
            self.max_retries = 10

        def cleanup_wrapper(self):
            pass

        def run_rip(self, stage_dir, *a, **kw):
            (stage_dir / "whipper_exec.log").write_text("KEEP_LOG", encoding="utf-8")
            (stage_dir / "stale.flac").write_bytes(b"STALE")
            return WhipperResult(
                1, "", "", tmp_path / "1.log", False, False, False, False, False,
                stalled=True, stalled_track=1,
            )

    from whipit.ripping.raw import RawExtractionResult

    raw_calls = []

    class MockRawExtractor:
        def __init__(self, device_path, speed=None, output_callback=None, **kw):
            raw_calls.append((device_path, speed))

        def run_raw_extraction(self, stage_dir, cancel_event=None):
            f1 = stage_dir / "01.flac"
            f1.write_bytes(b"RAW_FLAC")
            return RawExtractionResult(success=True, flac_files=[f1])

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", StallingRunner)
    monkeypatch.setattr("whipit.ripping.engine.RawSectorExtractor", MockRawExtractor)

    class RawFallbackAdapter(NullInteractionAdapter):
        def prompt_extraction_error(self, dev, avail, **kw):
            return ExtractionErrorChoice(ExtractionErrorAction.RAW_SECTOR_FALLBACK)

    engine = RippingEngine(
        cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=RawFallbackAdapter()
    )
    res = engine.run_extraction()

    assert res.success is True
    assert res.partial is False
    assert res.raw_fallback is True
    assert raw_calls == [("/dev/sr0", 4)]
    assert (tmp_path / "whipper_exec.log").exists()
    assert not (tmp_path / "stale.flac").exists()


def test_raw_sector_dialog_radio_enabled_and_selected(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QDialog, QRadioButton

    from whipit.ui.gui.dialogs import prompt_extraction_error_dialog

    _ = QApplication.instance() or QApplication([])

    monkeypatch.setattr("whipit.ui.gui.dialogs.shutil.which", lambda cmd: "/usr/bin/cdrdao" if cmd == "cdrdao" else None)

    def select_raw_exec(self):
        for rb in self.findChildren(QRadioButton):
            if "Emergency raw sector fallback" in rb.text():
                assert rb.isEnabled() is True
                rb.setChecked(True)
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(QDialog, "exec", select_raw_exec)
    choice = prompt_extraction_error_dialog(
        None,
        "/dev/sr0",
        [("/dev/sr0", "DRIVE_A")],
        has_partial_tracks=False,
        is_hardware_stall=True,
    )
    assert choice.action == ExtractionErrorAction.RAW_SECTOR_FALLBACK


def test_raw_extractor_toc_edge_cases_stall_and_cancellation(tmp_path, monkeypatch):
    import os
    import sys
    import threading

    from whipit.ripping.raw import CD_FRAME_BYTES, RawSectorExtractor, parse_cdrdao_toc

    assert parse_cdrdao_toc(tmp_path / "missing.toc") == []

    multi_seg_toc = tmp_path / "multi.toc"
    multi_seg_toc.write_text(
        "CD_DA\n"
        "TRACK AUDIO\n"
        'FILE "disc.bin" 0 00:00:01 // lead-in slice\n'
        'FILE "disc.bin" 00:00:01 00:00:02\n'
        "TRACK AUDIO\n"
        'FILE "disc.bin" 00:00:03\n',
        encoding="utf-8",
    )
    slices = parse_cdrdao_toc(multi_seg_toc)
    assert len(slices) == 2
    assert slices[0].segments == [(0, CD_FRAME_BYTES), (CD_FRAME_BYTES, 2 * CD_FRAME_BYTES)]
    assert slices[1].segments == [(3 * CD_FRAME_BYTES, None)]

    # Verify stall timeout and cancellation both clean up temp .bin/.toc artifacts
    tmp_bin = tmp_path / "bin"
    tmp_bin.mkdir()
    fake_cdrdao = tmp_bin / "cdrdao"
    fake_cdrdao.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, time\n"
        "pathlib.Path('raw_recovery.bin').write_bytes(b'PARTIAL')\n"
        "pathlib.Path('raw_recovery.toc').write_text('CD_DA\\n', encoding='utf-8')\n"
        "print('Reading...', flush=True)\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    fake_cdrdao.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_bin}:{os.environ.get('PATH', '')}")

    stage_dir = tmp_path / "stage_raw"
    extractor = RawSectorExtractor("/dev/sr0", stall_timeout_seconds=0.35)
    res_stall = extractor.run_raw_extraction(stage_dir)
    assert res_stall.success is False
    assert res_stall.stalled is True
    assert not (stage_dir / "raw_recovery.bin").exists()
    assert not (stage_dir / "raw_recovery.toc").exists()

    cancel_evt = threading.Event()
    cancel_evt.set()
    res_cancel = extractor.run_raw_extraction(stage_dir, cancel_event=cancel_evt)
    assert res_cancel.success is False
    assert res_cancel.cancelled is True


def test_workflow_raw_fallback_preserves_warning_tag_and_publishes(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        initial_metadata=AlbumMetadata(artist="Test Artist", album="Raw Recovery Album"),
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class RawMockEngine:
        def run_extraction(self, **kw):
            (coord.whipper_stage / "01.flac").write_bytes(b"RAW_FLAC_PAYLOAD")
            (coord.whipper_stage / "raw_recovery.cue").write_text(
                'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            (coord.whipper_stage / "raw_recovery.log").write_text(
                "Health status: Extraction completed via raw sector recovery (unverified)\n"
                "AccurateRip summary: Unverified (Raw sector recovery)\n"
                "Filename: 01.flac\n"
                "Status: Not verified\n",
                encoding="utf-8",
            )
            return ExtractionResult(
                success=True,
                partial=False,
                device_path="/dev/sr0",
                effective_offset=6,
                speed=4,
                raw_fallback=True,
            )

    tag_store = {
        "TRACKNUMBER": "1",
        "WARNING": "Extracted via raw sector recovery pass",
    }
    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: RawMockEngine())
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.flac_audio_sha256", lambda p: "raw-sha256")
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: tag_store.get(tag.upper()))
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: (tag_store.update({k.upper(): str(v) for k, v in tags.items()}), True)[1],
    )
    monkeypatch.setattr(
        "whipit.workflow.remove_flac_tags",
        lambda f, keys: ([tag_store.pop(k.upper(), None) for k in keys], True)[1],
    )

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert coord.manifest.verification.is_accurate is False
    assert "Unverified (Raw sector recovery)" in coord.manifest.verification.accuraterip_summary
    assert tag_store.get("WARNING") == "Extracted via raw sector recovery pass"
    assert tag_store.get("ARTIST") == "Test Artist"
    assert tag_store.get("ALBUM") == "Raw Recovery Album"
