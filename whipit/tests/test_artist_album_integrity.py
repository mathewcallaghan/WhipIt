"""Tests asserting artist and album data integrity across inspection, selection, and tagging."""

import pytest
from whipit.audio.metadata import fetch_release_details
from whipit.models import AlbumMetadata, ReleaseOption, TrackInfo, WorkflowResult
from whipit.ripping.identify import inspect_disc
from whipit.ui.base import NullInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


def test_inspect_disc_parses_artist_when_title_precedes_artist(monkeypatch):
    """Verifies that whipper blocks where Title precedes Artist or lack leading spaces still parse artist correctly."""
    sample_stdout = """
CDDB disc id: 800ac10a
MusicBrainz lookup URL https://musicbrainz.org/cdtoc/attach?toc=1+10+206687
Disc duration: 00:45:53.826, 10 audio tracks

Matching releases:

Title   : OK Computer
Artist  : Radiohead
Release : https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc
"""
    class MockProc:
        def __init__(self, *a, **kw):
            import io
            self.stdout = io.StringIO(sample_stdout.strip())
            self.returncode = 0
            self.pid = 1111
        def poll(self): return 0
        def wait(self, timeout=None): return 0

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("subprocess.Popen", MockProc)
    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_release_details",
        lambda *a, **kw: type("Details", (), {"tracks": [], "discogs_url": None, "disc_number": None, "total_discs": None, "artist": None, "title": None})(),
    )

    res = inspect_disc("/dev/sr0")
    assert res.success is True
    assert res.artist == "Radiohead"
    assert res.title == "OK Computer"
    assert len(res.matching_releases) == 1
    assert res.matching_releases[0].artist == "Radiohead"
    assert res.matching_releases[0].title == "OK Computer"


def test_workflow_updates_artist_and_title_to_chosen_pressing(temp_env, monkeypatch):
    """Verifies that selecting an alternate pressing correctly updates manifest artist and album instead of using release 0."""
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MultiInspect:
        matching_releases = [
            ReleaseOption(mbid="mbid-wrong-initial", artist="Wrong Artist", title="Wrong Album"),
            ReleaseOption(mbid="mbid-chosen", artist="Correct Artist", title="Correct Album"),
        ]
        artist = "Wrong Artist"
        title = "Wrong Album"
        mb_lookup_url = "https://musicbrainz.org/attach"
        discogs_url = None

    monkeypatch.setattr("whipit.ripping.identify.inspect_disc", lambda dev, **kw: MultiInspect())

    class SelectSecondAdapter(NullInteractionAdapter):
        def is_interactive(self): return True
        def prompt_select_release(self, releases):
            return "mbid-chosen"

    coord.ui = SelectSecondAdapter()

    class MockEngine:
        def run_extraction(self, **kw):
            from whipit.ripping.engine import ExtractionResult
            return ExtractionResult(success=True, partial=False, device_path="/dev/sr0", effective_offset=6, speed=None)

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    (coord.whipper_stage / "01.flac").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else None)
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert coord.manifest.metadata.artist == "Correct Artist"
    assert coord.manifest.metadata.album == "Correct Album"
    assert "Correct Artist - Correct Album" in str(coord.manifest.paths.final_flac_dir)


def test_workflow_heals_artist_when_album_accidentally_copied_to_artist(temp_env, monkeypatch):
    """Verifies that if curr_meta had album name copied to artist, workflow heals artist from FLAC tags."""
    initial_faulty_meta = AlbumMetadata(
        artist="Sgt. Pepper's Lonely Hearts Club Band",
        album="Sgt. Pepper's Lonely Hearts Club Band",
    )
    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        initial_metadata=initial_faulty_meta,
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockEngine:
        def run_extraction(self, **kw):
            from whipit.ripping.engine import ExtractionResult
            return ExtractionResult(success=True, partial=False, device_path="/dev/sr0", effective_offset=6, speed=None)

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    (coord.whipper_stage / "01.flac").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))

    def mock_read_tag(f, tag):
        if tag == "TRACKNUMBER": return "1"
        if tag in ("ALBUMARTIST", "ARTIST"): return "The Beatles"
        if tag == "ALBUM": return "Sgt. Pepper's Lonely Hearts Club Band"
        return None

    monkeypatch.setattr("whipit.workflow.read_flac_tag", mock_read_tag)
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert coord.manifest.metadata.artist == "The Beatles"
    assert coord.manifest.metadata.album == "Sgt. Pepper's Lonely Hearts Club Band"
