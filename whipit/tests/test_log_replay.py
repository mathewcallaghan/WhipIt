"""Golden log-replay integration tests asserting UI invariants against captured Whipper sessions."""

from pathlib import Path
import pytest
from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout, TrackInfo, WorkflowState
from whipit.ripping.identify import DiscInspectionResult
from whipit.ripping.whipper import ReleasePickedEvent, TrackProgressEvent, WhipperStreamParser


_LOGS_DIR = Path(__file__).parent / "fixtures" / "logs"


@pytest.fixture(autouse=True)
def disable_background_drive_polling(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr("whipit.ui.gui.main_window.prompt_unconfigured_offset_dialog", lambda *a, **kw: ("zero", 0))


@pytest.fixture
def win():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.drive_poll_timer.stop()
    w.show()
    yield w
    w.close()
    w.deleteLater()


def test_replay_coldplay_multi_release_catalog_locks_only_picked(win, monkeypatch):
    log_text = (_LOGS_DIR / "coldplay_viva_la_vida_multi.log").read_text(encoding="utf-8")

    fetched = []
    monkeypatch.setattr(win, "_fetch_cover_art", lambda mbid: fetched.append(mbid))

    for line in log_text.splitlines():
        win._on_whipper_output(line + "\n")

    # Invariant 1: Exactly 1 release was locked onto (the requested Australian edition)
    assert win.session.mbid == "902ba59e-fcad-428e-9f20-3d982fe6d323"
    assert fetched == ["902ba59e-fcad-428e-9f20-3d982fe6d323"]

    # Invariant 2: Candidate catalog rows never trigger spurious art queries
    assert "10c38900-3f35-4a09-a9cc-5c44ee2da0ed" not in fetched
    assert "553f9750-daaa-3be3-a476-374ff6a376e5" not in fetched

    # Invariant 3: Title rendered cleanly without multi-disc suffix
    assert win.session.display_title == "Coldplay — Viva la Vida or Death and All His Friends"

    # Invariant 4: Recognized release suppresses "Add this Disc" link
    win._update_links_display()
    rendered = win.external_links_lbl.text()
    assert "MusicBrainz" in rendered
    assert "Add this Disc to MusicBrainz" not in rendered
    assert "Copy Submission Note" not in rendered


def test_replay_foo_fighters_single_disc_never_shows_disc_1(win):
    log_text = (_LOGS_DIR / "foo_fighters_pretender_single.log").read_text(encoding="utf-8")

    for line in log_text.splitlines():
        win._on_whipper_output(line + "\n")

    # Invariant: Single disc release never displays '(Disc 1)'
    assert win.session.mbid == "08d2710e-a2c1-40f3-a113-f577728b1f67"
    assert "(Disc 1)" not in win.session.display_title
    assert "(Disc 1)" not in win.album_title_lbl.text()
    assert "Foo Fighters — The Pretender" in win.session.display_title


def test_replay_uncataloged_disc_shows_submit_link_and_copy_note(win):
    log_text = (_LOGS_DIR / "uncataloged_disc_unknown.log").read_text(encoding="utf-8")

    for line in log_text.splitlines():
        win._on_whipper_output(line + "\n")

    # Invariant: Uncataloged disc MUST show submission link and copy note button
    assert win.session.mbid is None
    assert win.session.mb_lookup_url is not None
    assert "cdtoc/attach" in win.session.mb_lookup_url

    win._update_links_display()
    rendered = win.external_links_lbl.text()
    assert "Add this Disc to MusicBrainz" in rendered
    assert "Copy Submission Note" in rendered
    assert "↗ MusicBrainz" not in rendered


def test_identify_to_rip_continuity_preserves_session_state(win, monkeypatch):
    # 1. Simulate "Identify Disc" finding the Australian Foo Fighters single
    insp_result = DiscInspectionResult(
        success=True,
        artist="Foo Fighters",
        title="The Pretender",
        duration="14:12",
        tracks_count=3,
        mbid="08d2710e-a2c1-40f3-a113-f577728b1f67",
        mb_url="https://musicbrainz.org/release/08d2710e-a2c1-40f3-a113-f577728b1f67",
        discogs_url="https://www.discogs.com/release/2066268",
        tracks=[
            TrackInfo(number=1, title="The Pretender", duration="04:31"),
            TrackInfo(number=2, title="Come Alive (demo)", duration="05:31"),
            TrackInfo(number=3, title="If Ever", duration="04:13"),
        ],
    )
    win._on_identify_finished(insp_result)

    # Assert precondition: Identified state populated in session
    assert win.session.mbid == "08d2710e-a2c1-40f3-a113-f577728b1f67"
    assert win.session.mb_url is not None
    assert win.session.discogs_url is not None
    assert len(win.session.tracks) == 3
    assert win.track_table.rowCount() == 3

    # 2. Intercept RipWorker creation to verify arguments passed to worker
    spawned_workers = []
    class DummyWorker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.workflow_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()
        def start(self):
            spawned_workers.append(self.kwargs)
        def isRunning(self):
            return False
        def cancel(self):
            pass

    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", DummyWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    # 3. User clicks "Start Rip"
    win.start_rip()

    # Invariant: Starting rip MUST NOT wipe URLs or reset mbid
    assert win.session.mbid == "08d2710e-a2c1-40f3-a113-f577728b1f67"
    assert win.session.mb_url == "https://musicbrainz.org/release/08d2710e-a2c1-40f3-a113-f577728b1f67"
    assert not win.external_links_lbl.isHidden()
    assert len(spawned_workers) == 1
    assert spawned_workers[0]["release_id"] == "08d2710e-a2c1-40f3-a113-f577728b1f67"
