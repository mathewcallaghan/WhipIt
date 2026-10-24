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
