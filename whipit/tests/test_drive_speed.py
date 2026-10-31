from pathlib import Path
import pytest
from whipit.config import format_time_span, get_drive_max_speed, parse_duration_seconds
from whipit.models import DeviceInfo, TrackValidationInfo
from whipit.ripping.engine import ExtractionResult
from whipit.ripping.whipper import DiscTOCEvent, TrackProgressEvent, WhipperStreamParser
from whipit.validation.flac import get_flac_duration_seconds


def test_get_drive_max_speed_parses_proc_cdrom_info(tmp_path: Path):
    fake_info = tmp_path / "cdrom_info"
    fake_info.write_text(
        "CD-ROM information, Id: cdrom.c 3.20 2003/12/17\n\n"
        "drive name:\t\tsr0\tsr1\n"
        "drive speed:\t\t48\t24\n"
        "drive # of slots:\t1\t1\n"
    )

    assert get_drive_max_speed("/dev/sr0", proc_info_path=fake_info) == 48
    assert get_drive_max_speed("/dev/sr1", proc_info_path=fake_info) == 24
    assert get_drive_max_speed("/dev/sr2", proc_info_path=fake_info) is None


def test_get_drive_max_speed_handles_missing_file(tmp_path: Path):
    missing_info = tmp_path / "nonexistent_info"
    assert get_drive_max_speed("/dev/sr0", proc_info_path=missing_info) is None


def test_parse_duration_seconds():
    assert parse_duration_seconds("04:15") == 255.0
    assert parse_duration_seconds("01:02:03") == 3723.0
    assert parse_duration_seconds("00:45:53.826") == pytest.approx(2753.826, rel=1e-3)
    assert parse_duration_seconds("120") == 120.0
    assert parse_duration_seconds(None) is None
    assert parse_duration_seconds("invalid") is None


def test_format_time_span():
    assert format_time_span(35) == "35s"
    assert format_time_span(255) == "4m 15s"
    assert format_time_span(3723) == "1h 02m 03s"


def test_get_flac_duration_seconds_parses_metaflac(tmp_path: Path, monkeypatch):
    test_flac = tmp_path / "test.flac"
    test_flac.touch()

    class MockRun:
        stdout = "11907000\n44100\n"
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MockRun())
    duration = get_flac_duration_seconds(test_flac)
    assert duration == pytest.approx(270.0, rel=1e-3)


def test_extraction_result_and_device_info_models():
    res = ExtractionResult(
        success=True,
        partial=False,
        device_path="/dev/sr0",
        effective_offset=6,
        speed=8,
        elapsed_seconds=120.5,
        average_speed_x=12.4,
    )
    assert res.elapsed_seconds == 120.5
    assert res.average_speed_x == 12.4

    dev = DeviceInfo(path="/dev/sr0", hardware_id="TEST", max_speed=48)
    assert dev.max_speed == 48

    tracks = TrackValidationInfo(total_audio_seconds=2753.0, rip_elapsed_seconds=280.0, average_speed_x=9.83)
    assert tracks.average_speed_x == 9.83


def test_whipper_stream_parser_parses_disc_duration():
    parser = WhipperStreamParser()
    ev = parser.parse_line("Disc duration: 00:45:53.826, 10 audio tracks\n")
    assert isinstance(ev, DiscTOCEvent)
    assert ev.duration == "00:45:53.826"


def test_track_progress_event_speed_field():
    ev = TrackProgressEvent(current_track=1, total_tracks=10, percent=50.0, phase="Reading", speed_x=8.5)
    assert ev.speed_x == 8.5


def test_whipper_stream_parser_parses_live_speed():
    parser = WhipperStreamParser()
    ev = parser.parse_line("Reading track 3 of 12: 45 % @ 8.4x\n")
    assert isinstance(ev, TrackProgressEvent)
    assert ev.current_track == 3
    assert ev.total_tracks == 12
    assert ev.percent == 45.0
    assert ev.speed_x == 8.4


def test_drive_panel_renders_speed_in_progress_format():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    ev = TrackProgressEvent(current_track=2, total_tracks=10, percent=60.0, phase="Reading", speed_x=9.2)
    panel._on_whipper_event(ev)
    assert "Reading Track 2 of 10... (60% @ 9.2x)" in panel.rip_progress_bar.format()


def test_drive_panel_computes_speed_from_duration_and_delta():
    pytest.importorskip("PyQt6")
    import time
    from PyQt6.QtWidgets import QApplication
    from whipit.models import TrackInfo
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    panel.session.tracks = [TrackInfo(number=1, title="Track 1", duration="05:00")]  # 300 seconds

    # Sample 1: 1 second ago at 0%
    now = time.monotonic()
    panel._speed_sample_track = 1
    panel._speed_sample_time = now - 1.0
    panel._speed_sample_percent = 0.0

    # Progress update arriving after time delta: 5% in 1 second on 300s track -> (0.05 * 300) / 1.0 = 15.0x
    ev = TrackProgressEvent(current_track=1, total_tracks=1, percent=5.0, phase="Reading")
    panel._on_whipper_event(ev)

    # Assert speed formatting was generated
    assert "@" in panel.rip_progress_bar.format()


def test_drive_panel_renders_drive_stall_warning():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ripping.whipper import DriveStallEvent
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    stall_ev = DriveStallEvent(seconds_stalled=45.0, wchan="scsi_wait")
    panel._on_whipper_event(stall_ev)

    assert "Stalled (45s)" in panel.rip_progress_bar.format()
    assert "Drive read stalled (45s)" in panel.phase_detail_lbl.text()


def test_track_progress_event_highlights_track_table_row():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.models import TrackInfo
    from whipit.ripping.whipper import TrackProgressEvent
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    panel.session.tracks = [
        TrackInfo(number=1, title="Track 1", duration="03:00"),
        TrackInfo(number=2, title="Track 2", duration="04:00"),
        TrackInfo(number=3, title="Track 3", duration="05:00"),
    ]
    panel._populate_track_table(panel.session.tracks)

    # Initially nothing is selected
    assert len(panel.track_table.selectedItems()) == 0

    # Reading track 2 highlights row 1 (0-indexed)
    panel._on_whipper_event(TrackProgressEvent(current_track=2, total_tracks=3, percent=30.0, phase="Reading"))
    assert {item.row() for item in panel.track_table.selectedItems()} == {1}

    # Reading track 3 moves highlight to row 2
    panel._on_whipper_event(TrackProgressEvent(current_track=3, total_tracks=3, percent=10.0, phase="Reading"))
    assert {item.row() for item in panel.track_table.selectedItems()} == {2}

    # Finished rip clears selection
    panel._on_workflow_finished(True, "Complete")
    assert len(panel.track_table.selectedItems()) == 0


def test_whipper_stream_parser_parses_pre_extraction():
    parser = WhipperStreamParser()
    ev = parser.parse_line("Reading table   0 %\n")
    from whipit.ripping.whipper import PreExtractionEvent
    assert isinstance(ev, PreExtractionEvent)


def test_drive_panel_displays_pre_extraction_status_between_toc_and_track_1():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ripping.whipper import DiscTOCEvent, TrackProgressEvent
    from whipit.ui.gui.drive_panel import DrivePanel, DriveState

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    panel.transition_to(DriveState.RIPPING)

    # 1. TOC parsed during ripping before Track 1 starts
    toc_ev = DiscTOCEvent(duration="00:45:53.826")
    panel._on_whipper_event(toc_ev)

    expected_msg = "Reading track pregaps & indexing audio sectors..."
    assert panel.rip_progress_bar.format() == expected_msg
    assert panel.phase_detail_lbl.text() == expected_msg

    # 2. Reading table line received
    panel._on_whipper_output("Reading table   0 %\n")
    assert panel.rip_progress_bar.format() == expected_msg
    assert panel.phase_detail_lbl.text() == expected_msg

    # 3. Track 1 starts -> switches to track progress
    track_ev = TrackProgressEvent(current_track=1, total_tracks=10, percent=0.0, phase="Reading")
    panel._on_whipper_event(track_ev)

    assert panel.rip_progress_bar.format() != expected_msg
    assert "Reading Track 1 of 10" in panel.rip_progress_bar.format()
    assert "Reading Track 1 / 10" in panel.phase_detail_lbl.text()
