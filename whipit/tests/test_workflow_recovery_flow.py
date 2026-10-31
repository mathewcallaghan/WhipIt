"""Automated tests validating read-error recovery, drive switching, and reload prompt lifecycles."""

import errno
from pathlib import Path
import subprocess
import threading
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
from whipit.models import AlbumMetadata, ReleaseOption, TrackInfo, WorkflowResult, WorkflowState
from whipit.ripping.engine import ExtractionResult, RippingEngine
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.ripping.whipper import WhipperResult, WhipperRunner
from whipit.ui.base import (
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
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
