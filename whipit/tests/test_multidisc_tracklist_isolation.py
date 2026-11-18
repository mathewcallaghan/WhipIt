from __future__ import annotations

import json
from pathlib import Path

import pytest

from whipit.audio.metadata import fetch_release_details
from whipit.models import JobManifest, TrackInfo
from whipit.ripping.identify import inspect_disc
from whipit.ui.gui.drive_panel import DrivePanel

_TWO_DISC_PAYLOAD = {
    "title": "The Wall",
    "artist-credit": [{"name": "Pink Floyd", "joinphrase": ""}],
    "media": [
        {
            "position": 1,
            "track-count": 13,
            "discs": [{"id": "DISCID_DISC1", "sectors": 200000}],
            "tracks": [
                {"number": "1", "title": "In the Flesh?", "length": 196000},
                {"number": "2", "title": "The Thin Ice", "length": 147000},
            ],
        },
        {
            "position": 2,
            "track-count": 13,
            "discs": [{"id": "DISCID_DISC2", "sectors": 190000}],
            "tracks": [
                {"number": "1", "title": "Hey You", "length": 280000},
                {"number": "2", "title": "Is There Anybody Out There?", "length": 164000},
            ],
        },
    ],
}


def test_fetch_release_details_selects_disc2_with_identical_track_counts(monkeypatch):
    """Verifies that Disc 2 is resolved correctly even when Disc 1 and Disc 2 have identical track counts."""
    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_bounded",
        lambda *a, **kw: json.dumps(_TWO_DISC_PAYLOAD).encode("utf-8"),
    )
    monkeypatch.setattr("whipit.audio.metadata._mb_throttle", lambda *a: None)

    # 1. Fetching Disc 1
    d1 = fetch_release_details(
        "11111111-2222-3333-4444-555555555555",
        expected_tracks=13,
        disc_number=1,
    )
    assert d1.disc_number == 1
    assert d1.total_discs == 2
    assert len(d1.tracks) == 2
    assert d1.tracks[0].title == "In the Flesh?"

    # 2. Fetching Disc 2 with disc_number=2
    d2 = fetch_release_details(
        "11111111-2222-3333-4444-555555555555",
        expected_tracks=13,
        disc_number=2,
    )
    assert d2.disc_number == 2
    assert d2.total_discs == 2
    assert len(d2.tracks) == 2
    assert d2.tracks[0].title == "Hey You"


def test_fetch_release_details_selects_disc_by_disc_id(monkeypatch):
    """Verifies that providing disc_id resolves the exact corresponding medium."""
    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_bounded",
        lambda *a, **kw: json.dumps(_TWO_DISC_PAYLOAD).encode("utf-8"),
    )
    monkeypatch.setattr("whipit.audio.metadata._mb_throttle", lambda *a: None)

    d2 = fetch_release_details(
        "11111111-2222-3333-4444-555555555555",
        disc_id="DISCID_DISC2",
    )
    assert d2.disc_number == 2
    assert d2.tracks[0].title == "Hey You"


def test_inspect_disc_parses_disc_number_from_whipper_output(monkeypatch):
    """Verifies that whipper stdout containing '(Disc 2 of 2)' extracts disc_number=2."""
    whipper_stdout = """
CDDB disc id: c009560d
MusicBrainz disc id DISCID_DISC2
Disc duration: 00:39:50.466, 13 audio tracks

Matching releases:

Artist  : Pink Floyd
Title   : The Wall (Disc 2 of 2)
Release : https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555
"""
    class MockInspectProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdout = io.StringIO(whipper_stdout.strip())
            self.returncode = 0
            self.pid = 9999
        def poll(self): return 0
        def wait(self, timeout=None): return 0

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("subprocess.Popen", MockInspectProc)

    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_release_details",
        lambda mbid, disc_number=None, **kw: type(
            "Details",
            (),
            {
                "tracks": [TrackInfo(number=1, title="Hey You" if disc_number == 2 else "In the Flesh?")],
                "discogs_url": None,
                "disc_number": disc_number or 2,
                "total_discs": 2,
                "artist": "Pink Floyd",
                "title": "The Wall",
            },
        )(),
    )

    res = inspect_disc("/dev/sr1")
    assert res.success is True
    assert res.disc_number == 2
    assert res.total_discs == 2
    assert len(res.tracks) == 1
    assert res.tracks[0].title == "Hey You"


def test_drive_panels_render_independent_tracklists_for_multi_disc_set():
    """Verifies that DrivePanel for sr0 (Disc 1) and DrivePanel for sr1 (Disc 2) have distinct track listings."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ripping.identify import DiscInspectionResult

    _ = QApplication.instance() or QApplication([])

    panel0 = DrivePanel(device="/dev/sr0")
    panel1 = DrivePanel(device="/dev/sr1")

    res_disc1 = DiscInspectionResult(
        success=True,
        artist="Pink Floyd",
        title="The Wall",
        disc_number=1,
        total_discs=2,
        mbid="release-wall-uuid",
        tracks=[
            TrackInfo(number=1, title="In the Flesh?", duration="03:16"),
            TrackInfo(number=2, title="The Thin Ice", duration="02:27"),
        ],
    )
    panel0._on_identify_finished(res_disc1)

    res_disc2 = DiscInspectionResult(
        success=True,
        artist="Pink Floyd",
        title="The Wall",
        disc_number=2,
        total_discs=2,
        mbid="release-wall-uuid",
        tracks=[
            TrackInfo(number=1, title="Hey You", duration="04:40"),
            TrackInfo(number=2, title="Is There Anybody Out There?", duration="02:44"),
        ],
    )
    panel1._on_identify_finished(res_disc2)

    # Invariant: Each drive panel displays its own disc track listing
    assert "Disc 1" in panel0.album_title_lbl.text()
    assert panel0.track_table.item(0, 1).text() == "In the Flesh?"

    assert "Disc 2" in panel1.album_title_lbl.text()
    assert panel1.track_table.item(0, 1).text() == "Hey You"
    assert panel0.track_table.item(0, 1).text() != panel1.track_table.item(0, 1).text()


def test_fetch_release_details_retries_503_and_recovers_tracks(monkeypatch):
    """Verifies transient 503 rate-limit errors from MusicBrainz are retried and recover tracks."""
    import urllib.error

    attempts = 0

    def mock_fetch(url, *a, **kw):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(url, 503, "Service Temporarily Unavailable", {}, None)
        return json.dumps(_TWO_DISC_PAYLOAD).encode("utf-8")

    monkeypatch.setattr("whipit.audio.metadata.fetch_bounded", mock_fetch)
    monkeypatch.setattr("whipit.audio.metadata._mb_throttle", lambda *a: None)

    details = fetch_release_details(
        "e506a9f8-a9b7-4501-ab30-9c6e53c97a7f",
        disc_number=1,
        disc_id="DISCID_DISC1",
    )
    assert attempts == 3
    assert details.disc_number == 1
    assert len(details.tracks) == 2
    assert details.tracks[0].title == "In the Flesh?"


def test_drive_panel_identify_triggers_details_lookup_when_tracks_missing(monkeypatch):
    """Verifies that if inspect_disc returns an MBID without tracks, DrivePanel triggers a background lookup."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ripping.identify import DiscInspectionResult

    _ = QApplication.instance() or QApplication([])

    panel = DrivePanel(device="/dev/sr2")
    lookups = []
    monkeypatch.setattr(panel, "_trigger_release_details_lookup", lambda mbid, **kw: lookups.append((mbid, kw)))

    mbid = "e506a9f8-a9b7-4501-ab30-9c6e53c97a7f"
    res = DiscInspectionResult(
        success=True,
        artist="Various Artists",
        title="So Fresh: Songs for Christmas 2009",
        disc_number=1,
        total_discs=2,
        tracks_count=22,
        disc_id="ec8X7O7Tx5dkJE4Q7dOjJA2_XBc-",
        mbid=mbid,
        mb_url=f"https://musicbrainz.org/release/{mbid}",
        tracks=[],
    )
    panel._on_identify_finished(res, silent=True)

    assert len(lookups) == 1
    assert lookups[0][0] == mbid
    assert lookups[0][1]["disc_number"] == 1
    assert lookups[0][1]["disc_id"] == "ec8X7O7Tx5dkJE4Q7dOjJA2_XBc-"

    panel._on_details_resolved(
        mbid,
        "https://www.discogs.com/release/123",
        [TrackInfo(number=1, title="Track One", duration="03:20")],
        disc_number=1,
        total_discs=2,
    )
    assert panel.track_table.rowCount() == 1
    assert panel.track_table.item(0, 1).text() == "Track One"


def test_drive_panel_ignores_ebusy_poll_while_identifying():
    """Verifies transient CDS_NO_INFO polls during IdentifyWorker execution do not reset disc state."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.config import CDS_AUDIO, CDS_DISC_OK, CDS_NO_INFO

    _ = QApplication.instance() or QApplication([])

    panel = DrivePanel(device="/dev/sr2")
    panel.session.is_disc_loaded = True
    panel._disc_was_ejected = False
    panel._disc_identified = True

    class FakeIdentifyRunning:
        def isRunning(self):
            return True

    panel.identify_worker = FakeIdentifyRunning()

    panel.handle_drive_status(CDS_NO_INFO, CDS_NO_INFO)
    assert panel._disc_was_ejected is False
    assert panel._disc_identified is True

    panel.handle_drive_status(CDS_DISC_OK, CDS_NO_INFO)
    assert panel._disc_was_ejected is False
    assert panel._disc_identified is True

    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert panel._disc_identified is True


def test_multidisc_publication_paths_and_single_disc_override(temp_env: dict[str, Path]) -> None:
    from whipit.config import get_cache_dir
    from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout
    from whipit.services.publication_service import PublicationService

    flac_root = temp_env["root"] / "FLAC_ISOLATION"
    flac_root.mkdir()
    job_id = "test-multidisc-paths"
    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=flac_root,
            generate_opus=False,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=flac_root / f".whipit-stage.{job_id}",
        ),
    )

    # 1. Multi-disc set: Disc 1 of 2 -> publishes into Disc 1 subfolder
    meta_d1 = AlbumMetadata(artist="Pink Floyd", album="The Wall", disc_number=1, total_discs=2)
    PublicationService.resolve_paths(manifest, meta_d1, flac_root=flac_root, opus_root=None, generate_opus=False)
    assert manifest.paths.final_flac_dir is not None
    assert manifest.paths.final_flac_dir.name == "Disc 1"
    assert manifest.paths.final_flac_dir.parent.name == "Pink Floyd - The Wall"

    # 2. Disc 2 of 2 -> publishes into Disc 2 subfolder
    meta_d2 = AlbumMetadata(artist="Pink Floyd", album="The Wall", disc_number=2, total_discs=2)
    PublicationService.resolve_paths(manifest, meta_d2, flac_root=flac_root, opus_root=None, generate_opus=False)
    assert manifest.paths.final_flac_dir is not None
    assert manifest.paths.final_flac_dir.name == "Disc 2"
    assert manifest.paths.final_flac_dir.parent.name == "Pink Floyd - The Wall"

    # 3. Single-disc override (disc_number=None, total_discs=None) -> publishes directly to album root
    meta_single = AlbumMetadata(artist="Pink Floyd", album="The Wall", disc_number=None, total_discs=None)
    PublicationService.resolve_paths(manifest, meta_single, flac_root=flac_root, opus_root=None, generate_opus=False)
    assert manifest.paths.final_flac_dir is not None
    assert manifest.paths.final_flac_dir.name == "Pink Floyd - The Wall"


def test_stage_candidate_writes_discnumber_and_disctotal_vorbis_tags(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whipit.models import AlbumMetadata
    from whipit.services.stage_metadata_service import StageMetadataService
    from whipit.ui.base import NullInteractionAdapter

    content_dir = sample_manifest.paths.whipper_stage / "disc"
    content_dir.mkdir(parents=True, exist_ok=True)
    t1 = content_dir / "01.flac"
    t1.write_bytes(b"FLAC1")

    written_tags: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.flac_audio_sha256", lambda p: "mock-hash")
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Raw Title")
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: (written_tags.append((f.name, dict(tags))), True)[1],
    )

    meta = AlbumMetadata(
        artist="DJ Shadow",
        album="The Private Repress",
        disc_number=1,
        total_discs=2,
        custom_track_titles={1: "Intro"},
        custom_track_artists={1: "DJ Shadow feat. Cut Chemist"},
    )

    res = StageMetadataService().stage_candidate(
        content_dir=content_dir,
        candidate_stage=sample_manifest.paths.candidate_stage,
        candidate_stage_opus=None,
        generate_opus=False,
        expected_tracks=1,
        flac_files=[t1],
        is_partial=False,
        metadata=meta,
        raw_tag_artist="DJ Shadow",
        raw_tag_album="The Private Repress",
        user_art=None,
        ui=NullInteractionAdapter(),
    )

    assert res.success is True
    assert len(written_tags) == 1
    tags = written_tags[0][1]
    assert tags.get("DISCNUMBER") == "1"
    assert tags.get("DISCTOTAL") == "2"
    assert tags.get("ARTIST") == "DJ Shadow feat. Cut Chemist"
    assert tags.get("ALBUMARTIST") == "DJ Shadow"
    assert tags.get("TITLE") == "Intro"


def test_workflow_edited_2cd_clears_conflicting_mbids_for_unified_album(
    temp_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whipit.models import WorkflowResult
    from whipit.ripping.engine import ExtractionResult
    from whipit.ui.gui.session import ActiveSession
    from whipit.workflow import RipWorkflowCoordinator

    flac_out = temp_env["root"] / "FLAC_2CD"
    opus_out = temp_env["root"] / "OPUS_2CD"
    flac_out.mkdir()
    opus_out.mkdir()

    class MockEngine:
        def run_extraction(self, **kw):
            return ExtractionResult(
                success=True,
                partial=False,
                device_path="/dev/sr0",
                effective_offset=6,
                speed=None,
            )

    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr(
        "whipit.workflow.parse_whipper_report",
        lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1),
    )
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.flac_audio_sha256", lambda p: "mock-sha256")
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)

    disc_configs = [
        (
            1,
            "11111111-1111-1111-1111-111111111111",
            {
                "TRACKNUMBER": "1",
                "TITLE": "Opening Track",
                "ARTIST": "The Artist",
                "ALBUMARTIST": "The Artist",
                "ALBUM": "Wrong MB Edition (Disc 1)",
                "DATE": "1999-03-01",
                "DISCNUMBER": "1",
                "TOTALDISCS": "1",
                "MUSICBRAINZ_ALBUMID": "11111111-1111-1111-1111-111111111111",
                "MUSICBRAINZ_RELEASEGROUPID": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "MUSICBRAINZ_ALBUMARTISTID": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "MUSICBRAINZ_DISCID": "DISC1_ID",
                "MUSICBRAINZ_TRACKID": "TRACK1_UUID",
                "LABELID": "CAT-001",
                "ALBUM_GROUPING_KEY": "wrong-key-1",
                "ALBUMSORT": "Wrong MB Edition (Disc 1)",
                "ALBUMARTISTSORT": "Artist, The",
            },
        ),
        (
            2,
            "22222222-2222-2222-2222-222222222222",
            {
                "TRACKNUMBER": "1",
                "TITLE": "Disc Two Opener",
                "ARTIST": "The Artist",
                "ALBUMARTIST": "The Artist",
                "ALBUM": "Wrong MB Edition (Disc 2)",
                "DATE": "2001-07-15",
                "DISCNUMBER": "1",
                "TOTALDISCS": "1",
                "MUSICBRAINZ_ALBUMID": "22222222-2222-2222-2222-222222222222",
                "MUSICBRAINZ_RELEASEGROUPID": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "MUSICBRAINZ_ALBUMARTISTID": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "MUSICBRAINZ_DISCID": "DISC2_ID",
                "MUSICBRAINZ_TRACKID": "TRACK2_UUID",
                "LABELID": "CAT-002",
                "ALBUM_GROUPING_KEY": "wrong-key-2",
                "ALBUMSORT": "Wrong MB Edition (Disc 2)",
                "ALBUMARTISTSORT": "Artist, The",
            },
        ),
    ]

    final_tags_by_disc: dict[int, dict[str, str]] = {}
    final_dirs_by_disc: dict[int, tuple] = {}

    for disc_num, mbid, initial_tags in disc_configs:
        disc_tags = dict(initial_tags)

        monkeypatch.setattr(
            "whipit.workflow.read_flac_tag",
            lambda f, tag, store=disc_tags: store.get(tag.upper()),
        )
        monkeypatch.setattr(
            "whipit.workflow.write_flac_tags",
            lambda f, tags, store=disc_tags: (
                store.update({k.upper(): str(v) for k, v in tags.items()}),
                True,
            )[1],
        )
        monkeypatch.setattr(
            "whipit.workflow.remove_flac_tags",
            lambda f, keys, store=disc_tags: (
                [store.pop(k.upper(), None) for k in keys],
                True,
            )[1],
        )

        session = ActiveSession()
        session.artist = "The Artist"
        session.title = "Unified 2CD: Complete"
        session.year = "2005"
        session.disc_number = disc_num
        session.total_discs = 2
        session.mbid = mbid
        session.mb_release_overridden = True
        session.tracks = [TrackInfo(number=1, title=initial_tags["TITLE"])]

        coord = RipWorkflowCoordinator(
            device="/dev/sr0",
            flac_root=flac_out,
            opus_root=opus_out,
            generate_opus=True,
            release_id=mbid,
            initial_metadata=session.to_album_metadata(),
        )
        coord.whipper_stage.mkdir(parents=True, exist_ok=True)
        (coord.whipper_stage / "01.flac").write_bytes(b"FLAC_AUDIO")
        (coord.whipper_stage / "test.cue").write_text(
            'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n',
            encoding="utf-8",
        )
        (coord.whipper_stage / "test.log").write_text(
            "Health status: No errors occurred\nAccurateRip summary: All tracks accurately ripped\n",
            encoding="utf-8",
        )

        res = coord.run_workflow()
        assert res == WorkflowResult.SUCCESS
        final_tags_by_disc[disc_num] = disc_tags
        final_dirs_by_disc[disc_num] = (
            coord.manifest.paths.final_flac_dir,
            coord.manifest.paths.final_opus_dir,
        )

    assert final_dirs_by_disc[1][0] == flac_out / "The Artist - Unified 2CD - Complete" / "Disc 1"
    assert final_dirs_by_disc[2][0] == flac_out / "The Artist - Unified 2CD - Complete" / "Disc 2"
    assert final_dirs_by_disc[1][1] == opus_out / "The Artist - Unified 2CD - Complete" / "Disc 1"
    assert final_dirs_by_disc[2][1] == opus_out / "The Artist - Unified 2CD - Complete" / "Disc 2"

    d1, d2 = final_tags_by_disc[1], final_tags_by_disc[2]
    assert d1["ALBUM"] == d2["ALBUM"] == "Unified 2CD: Complete"
    assert d1["ALBUMARTIST"] == d2["ALBUMARTIST"] == "The Artist"
    assert d1["DATE"] == d2["DATE"] == "2005"
    assert d1["DISCNUMBER"] == "1"
    assert d2["DISCNUMBER"] == "2"
    assert d1["DISCTOTAL"] == d2["DISCTOTAL"] == "2"
    assert d1["TOTALDISCS"] == d2["TOTALDISCS"] == "2"

    for stale_tag in (
        "MUSICBRAINZ_ALBUMID",
        "MUSICBRAINZ_RELEASEGROUPID",
        "MUSICBRAINZ_ALBUMARTISTID",
        "LABELID",
        "ALBUM_GROUPING_KEY",
        "ALBUMSORT",
        "ALBUMARTISTSORT",
    ):
        assert stale_tag not in d1
        assert stale_tag not in d2

    assert d1["MUSICBRAINZ_DISCID"] == "DISC1_ID"
    assert d2["MUSICBRAINZ_DISCID"] == "DISC2_ID"


def test_workflow_track_title_edit_preserves_musicbrainz_albumid(
    temp_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from whipit.models import WorkflowResult
    from whipit.ripping.engine import ExtractionResult
    from whipit.ui.gui.session import ActiveSession
    from whipit.workflow import RipWorkflowCoordinator

    flac_out = temp_env["root"] / "FLAC_PRESERVE_MBID"
    flac_out.mkdir()
    mbid = "33333333-3333-3333-3333-333333333333"

    class MockEngine:
        def run_extraction(self, **kw):
            return ExtractionResult(
                success=True,
                partial=False,
                device_path="/dev/sr0",
                effective_offset=6,
                speed=None,
            )

    disc_tags = {
        "TRACKNUMBER": "1",
        "TITLE": "Typo Title",
        "ARTIST": "Artist",
        "ALBUMARTIST": "Artist",
        "ALBUM": "Exact Match Album",
        "MUSICBRAINZ_ALBUMID": mbid,
    }

    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr(
        "whipit.workflow.parse_whipper_report",
        lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1),
    )
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.flac_audio_sha256", lambda p: "mock-sha256")
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr(
        "whipit.workflow.read_flac_tag",
        lambda f, tag: disc_tags.get(tag.upper()),
    )
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: (disc_tags.update({k.upper(): str(v) for k, v in tags.items()}), True)[1],
    )
    monkeypatch.setattr(
        "whipit.workflow.remove_flac_tags",
        lambda f, keys: ([disc_tags.pop(k.upper(), None) for k in keys], True)[1],
    )

    session = ActiveSession()
    session.artist = "Artist"
    session.title = "Exact Match Album"
    session.mbid = mbid
    session.mb_release_overridden = False
    session.tracks = [TrackInfo(number=1, title="Corrected Track Title")]

    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        flac_root=flac_out,
        generate_opus=False,
        release_id=mbid,
        initial_metadata=session.to_album_metadata(),
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    (coord.whipper_stage / "01.flac").write_bytes(b"FLAC_AUDIO")
    (coord.whipper_stage / "test.cue").write_text(
        'FILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n',
        encoding="utf-8",
    )
    (coord.whipper_stage / "test.log").write_text(
        "Health status: No errors occurred\nAccurateRip summary: All tracks accurately ripped\n",
        encoding="utf-8",
    )

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert disc_tags["TITLE"] == "Corrected Track Title"
    assert disc_tags["MUSICBRAINZ_ALBUMID"] == mbid
