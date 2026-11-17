import json

import pytest

from whipit.audio.metadata import fetch_release_details
from whipit.models import TrackInfo
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
