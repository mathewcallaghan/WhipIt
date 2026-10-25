import json
import urllib.error
from pathlib import Path
import pytest
from whipit.audio.metadata import fetch_cover_art_from_caa
from whipit.config import get_cache_dir
from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout, ReleaseOption, WorkflowState


@pytest.fixture(autouse=True)
def disable_background_drive_polling(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)


@pytest.fixture
def win():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.drive_poll_timer.stop()
    yield w
    w.close()
    w.deleteLater()


_SAMPLE_WHIPPER_SESSION_LOG = """
INFO:whipper.command.cd:using configured read offset 6
INFO:whipper.command.cd:checking device /dev/sr0
CDDB disc id: 800ac10a
MusicBrainz disc id nIDrTG_DHGA9S.6.kzjb9zGOZXI-
MusicBrainz lookup URL https://musicbrainz.org/cdtoc/attach?toc=1+10+206687
Disc duration: 00:45:53.826, 10 audio tracks

Matching releases:

Artist  : Coldplay
Title   : Viva la Vida or Death and All His Friends
URL     : https://musicbrainz.org/release/10c38900-3f35-4a09-a9cc-5c44ee2da0ed
Release : 10c38900-3f35-4a09-a9cc-5c44ee2da0ed

Artist  : Coldplay
Title   : Viva la Vida or Death and All His Friends
URL     : https://musicbrainz.org/release/553f9750-daaa-3be3-a476-374ff6a376e5
Release : 553f9750-daaa-3be3-a476-374ff6a376e5

Artist  : Coldplay
Title   : Viva la Vida: Prospekt’s March Edition
URL     : https://musicbrainz.org/release/c9bd301c-66b6-3364-87f6-6e68e0069818
Release : c9bd301c-66b6-3364-87f6-6e68e0069818

Artist  : Coldplay
Title   : Viva la Vida or Death and All His Friends
URL     : https://musicbrainz.org/release/902ba59e-fcad-428e-9f20-3d982fe6d323
Release : 902ba59e-fcad-428e-9f20-3d982fe6d323

INFO:whipper.common.program:picked requested release id 902ba59e-fcad-428e-9f20-3d982fe6d323
Artist: Coldplay
Title : Viva la Vida or Death and All His Friends
Reading table 100 %
INFO:whipper.command.cd:ripping track 1 of 10: 01. Coldplay - Life in Technicolor.flac
"""


def test_log_replay_whipper_session_only_picks_active_release(win, monkeypatch):
    win._current_mbid = None

    fetched = []
    monkeypatch.setattr(win, "_fetch_cover_art", lambda mbid: fetched.append(mbid))

    for line in _SAMPLE_WHIPPER_SESSION_LOG.strip().splitlines():
        win._on_whipper_output(line + "\n")

    assert win._current_mbid == "902ba59e-fcad-428e-9f20-3d982fe6d323"
    assert fetched == ["902ba59e-fcad-428e-9f20-3d982fe6d323"]
    assert "Coldplay — Viva la Vida" in win.album_title_lbl.text()


def test_ui_invariant_user_art_never_cleared_by_failed_probe(win, tmp_path):
    target_mbid = "902ba59e-fcad-428e-9f20-3d982fe6d323"
    win._current_mbid = target_mbid

    active_art = tmp_path / "custom_user_art.jpg"
    from PyQt6.QtGui import QImage
    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.save(str(active_art), "JPEG")

    win._current_art_path = active_art
    win._art_is_user_selected = True

    win._on_cover_art_failed(target_mbid)
    assert win._current_art_path == active_art
    assert win._art_is_user_selected is True


def test_cover_art_callbacks_guard_against_mismatched_mbid(win, tmp_path):
    target_mbid = "10c38900-3f35-4a09-a9cc-5c44ee2da0ed"
    win._current_mbid = target_mbid

    active_art = tmp_path / "active.jpg"
    from PyQt6.QtGui import QImage
    img = QImage(32, 32, QImage.Format.Format_RGB32)
    img.save(str(active_art), "JPEG")

    win._on_cover_art_found(target_mbid, str(active_art))
    assert win._current_art_path == active_art

    stray_mbid = "84ae9ae8-8e3b-44c7-a20d-41f2ba5c0dc7"
    win._on_cover_art_failed(stray_mbid)
    assert win._current_art_path == active_art

    stray_art = tmp_path / "stray.jpg"
    img.save(str(stray_art), "JPEG")
    win._on_cover_art_found(stray_mbid, str(stray_art))
    assert win._current_art_path == active_art

    win._on_cover_art_failed(target_mbid)
    assert win._current_art_path is None


def test_prompt_select_release_synchronizes_state_and_fetches_art(win, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QInputDialog

    releases = [
        ReleaseOption(mbid="mbid-us", title="US Pressing", artist="Coldplay"),
        ReleaseOption(mbid="mbid-au", title="Australian Pressing", artist="Coldplay"),
    ]

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("Australian Pressing", True))
    fetched = []
    monkeypatch.setattr(win, "_fetch_cover_art", lambda mbid: fetched.append(mbid))

    selected = win._dispatch_modal_prompt("prompt_select_release", releases)

    assert selected == "mbid-au"
    assert win._current_mbid == "mbid-au"
    assert win._current_mb_url == "https://musicbrainz.org/release/mbid-au"
    assert "Coldplay" in win.album_title_lbl.text()
    assert fetched == ["mbid-au"]


def test_release_picked_event_initiates_track_lookup_when_table_empty(win, monkeypatch):
    target_mbid = "902ba59e-fcad-428e-9f20-3d982fe6d323"
    win.track_table.setRowCount(0)
    win.session.tracks.clear()

    lookups = []
    monkeypatch.setattr(win, "_start_release_details_lookup", lambda mbid, **kw: lookups.append(mbid))
    monkeypatch.setattr(win, "_fetch_cover_art", lambda mbid: None)

    from whipit.ripping.whipper import ReleasePickedEvent
    win._on_whipper_event(ReleasePickedEvent(mbid=target_mbid, is_requested=True))

    assert win.session.mbid == target_mbid
    assert lookups == [target_mbid]


def test_state_changed_triggers_track_lookup_even_if_mbid_already_set(win, temp_env, monkeypatch):
    output = temp_env["output"]
    cache = get_cache_dir()
    job_id = "test-track-lookup-sync"
    target_mbid = "10c38900-3f35-4a09-a9cc-5c44ee2da0ed"

    win.session.mbid = target_mbid
    win.track_table.setRowCount(0)
    win.session.tracks.clear()

    lookups = []
    monkeypatch.setattr(win, "_start_release_details_lookup", lambda mbid, **kw: lookups.append(mbid))
    monkeypatch.setattr(win, "_fetch_cover_art", lambda mbid: None)

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        metadata=AlbumMetadata(
            artist="Coldplay",
            album="Viva la Vida",
            musicbrainz_id=target_mbid,
        ),
        paths=PathLayout(
            output_root=output,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=output / f".whipit-stage.{job_id}",
        ),
    )

    win._on_state_changed(WorkflowState.RIPPING, manifest)
    assert lookups == [target_mbid]


def test_caa_fallback_to_release_group_when_release_art_404(monkeypatch, temp_env):
    release_mbid = "902ba59e-fcad-428e-9f20-3d982fe6d323"
    rg_mbid = "3827ec3c-a99f-3e81-a7eb-ec3cfb4a8e24"

    valid_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 100

    def mock_fetch_bounded(url, headers, suffixes, max_bytes, timeout=12.0):
        if f"/release/{release_mbid}/" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if f"/ws/2/release/{release_mbid}" in url:
            return json.dumps({"release-group": {"id": rg_mbid}}).encode("utf-8")
        if f"/release-group/{rg_mbid}/" in url:
            return valid_jpeg
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr("whipit.audio.metadata.fetch_bounded", mock_fetch_bounded)
    monkeypatch.setattr("whipit.audio.metadata._mb_throttle", lambda *a: None)

    art_file = fetch_cover_art_from_caa(release_mbid)
    assert art_file is not None
    assert art_file.exists()
    assert art_file.read_bytes() == valid_jpeg


def test_single_disc_album_does_not_display_disc_1_suffix(win, temp_env):
    output = temp_env["output"]
    cache = get_cache_dir()
    job_id = "test-single-disc"

    single_manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        metadata=AlbumMetadata(
            artist="Foo Fighters",
            album="The Pretender",
            disc_number=1,
            total_discs=1,
        ),
        paths=PathLayout(
            output_root=output,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=output / f".whipit-stage.{job_id}",
        ),
    )

    win._on_state_changed(WorkflowState.RIPPING, single_manifest)
    assert win.album_title_lbl.text() == "Foo Fighters — The Pretender"
    assert "(Disc 1)" not in win.album_title_lbl.text()


def test_multi_disc_album_displays_disc_suffix(win, temp_env):
    output = temp_env["output"]
    cache = get_cache_dir()
    job_id = "test-multi-disc"

    multi_manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        metadata=AlbumMetadata(
            artist="Pink Floyd",
            album="The Wall",
            disc_number=2,
            total_discs=2,
        ),
        paths=PathLayout(
            output_root=output,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=output / f".whipit-stage.{job_id}",
        ),
    )

    win._on_state_changed(WorkflowState.RIPPING, multi_manifest)
    assert win.album_title_lbl.text() == "Pink Floyd — The Wall (Disc 2)"


def test_links_display_does_not_show_submit_when_mb_release_exists(win):
    win.session.mb_url = "https://musicbrainz.org/release/08d2710e-a2c1-40f3-a113-f577728b1f67"
    win.session.discogs_url = "https://www.discogs.com/release/2066268"
    win.session.mb_lookup_url = "https://musicbrainz.org/cdtoc/attach?toc=1+3+64410"

    win._update_links_display()

    rendered = win.external_links_lbl.text()
    assert "MusicBrainz" in rendered
    assert "Discogs" in rendered
    assert "Add this Disc to MusicBrainz" not in rendered
    assert "Copy Submission Note" not in rendered


def test_links_display_shows_submit_when_disc_uncataloged(win):
    win.session.mb_url = None
    win.session.discogs_url = None
    win.session.mb_lookup_url = "https://musicbrainz.org/cdtoc/attach?toc=1+3+64410"

    win._update_links_display()

    rendered = win.external_links_lbl.text()
    assert "Add this Disc to MusicBrainz" in rendered
    assert "Copy Submission Note" in rendered


def _make_png(path):
    from PyQt6.QtGui import QImage

    img = QImage(64, 64, QImage.Format.Format_RGB32)
    img.fill(0xFF3366)
    assert img.save(str(path), "PNG")
    return path


class _DummyWorker:
    """Plain stand-in for RipWorker/IdentifyWorker, same style as the other GUI tests."""

    def __init__(self, *args, **kwargs):
        sig = lambda: type("Sig", (), {"connect": lambda *a, **k: None})()
        self.workflow_finished = sig()
        self.line_received = sig()
        self.inspection_completed = sig()
        self.finished = sig()

    def start(self):
        pass

    def isRunning(self):
        return False

    def cancel(self):
        pass


def _use_dummy_workers(monkeypatch):
    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", _DummyWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window.IdentifyWorker", _DummyWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)


def test_start_rip_after_commit_keeps_art_chosen_after_commit(win, tmp_path, monkeypatch):
    _use_dummy_workers(monkeypatch)
    art = _make_png(tmp_path / "cover.png")

    win._set_pipeline_step("COMMITTED")
    # Simulate _choose_custom_art() picking cover.png after the previous rip finished.
    win.session.set_user_art(art)
    win._art_chosen_since_commit = True
    win.adapter.set_cover_art(art)

    win.start_rip()

    assert win.session.art_is_user_selected is True
    assert win.session.art_path == art
    assert win.adapter.get_cover_art() == art


def test_start_rip_after_commit_clears_art_left_over_from_previous_rip(win, tmp_path, monkeypatch):
    _use_dummy_workers(monkeypatch)
    art = _make_png(tmp_path / "old_cover.png")

    # Art was chosen BEFORE the previous rip committed, then the rip committed.
    win.session.set_user_art(art)
    win.adapter.set_cover_art(art)
    win._art_chosen_since_commit = True
    win._set_pipeline_step("COMMITTED")

    assert win._art_chosen_since_commit is False

    win.start_rip()

    assert win.session.art_is_user_selected is False
    assert win.adapter.get_cover_art() is None


def test_manual_identify_does_not_wipe_user_art(win, tmp_path, monkeypatch):
    _use_dummy_workers(monkeypatch)
    art = _make_png(tmp_path / "cover.png")
    win.session.set_user_art(art)
    win.adapter.set_cover_art(art)

    win.drive_combo.addItem("test", "/dev/sr0")
    win.drive_combo.setCurrentIndex(win.drive_combo.count() - 1)

    win.identify_disc(silent=True)

    assert win.session.art_is_user_selected is True
    assert win.adapter.get_cover_art() == art
