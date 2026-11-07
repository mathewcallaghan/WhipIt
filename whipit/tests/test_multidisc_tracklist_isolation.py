import json
import pytest
from whipit.audio.metadata import fetch_release_details
from whipit.models import ReleaseOption, TrackInfo
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
