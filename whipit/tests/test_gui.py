import threading
import time
from pathlib import Path
import pytest
import whipit.ui
from whipit.models import DeviceInfo, JobManifest, PathLayout, ReleaseOption, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.state_store import ManifestStore


def test_ui_import_without_eager_qt():
    assert hasattr(whipit.ui, "UserInteractionPort")
    assert hasattr(whipit.ui, "LogLevel")
    assert hasattr(whipit.ui, "ConflictChoice")


def test_gui_adapter_shutdown_unblocks_waiting_threads():
    pytest.importorskip("PyQt6")
    from whipit.ui.base import DiscPromptAction
    from whipit.ui.gui.adapter import GuiInteractionAdapter

    adapter = GuiInteractionAdapter()
    unblocked = threading.Event()

    def worker_thread():
        res = adapter.prompt_insert_disc("/dev/sr0", "TEST_HW")
        assert res.action == DiscPromptAction.ABORT
        unblocked.set()

    t = threading.Thread(target=worker_thread)
    t.start()

    adapter.shutdown()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert unblocked.is_set()


def test_prompt_request_resolves_only_once():
    from whipit.ui.gui.adapter import PromptRequest

    req = PromptRequest("prompt_insert_disc", None)
    assert req.is_resolved is False
    assert req.resolve("first_choice") is True
    assert req.is_resolved is True
    assert req.resolve("second_choice") is False
    assert req.reply_queue.get_nowait() == "first_choice"


def test_prompt_request_bounded_timeout():
    from whipit.ui.gui.adapter import GuiInteractionAdapter
    from whipit.ui.base import DiscPromptAction

    adapter = GuiInteractionAdapter()
    res = adapter._request_from_main_thread("prompt_insert_disc", None, timeout=0.05)
    assert res.action == DiscPromptAction.ABORT


def test_prompt_request_defaults_are_safe_cancellations():
    from whipit.ui.gui.adapter import GuiInteractionAdapter
    from whipit.ui.base import ConflictChoice, DiscPromptAction, ExtractionErrorAction

    assert GuiInteractionAdapter._default_for_prompt("prompt_conflict_resolution") == ConflictChoice.ABORT
    assert GuiInteractionAdapter._default_for_prompt("confirm_purge_stage") is False
    assert GuiInteractionAdapter._default_for_prompt("prompt_extraction_error").action == ExtractionErrorAction.ABORT
    assert GuiInteractionAdapter._default_for_prompt("prompt_insert_disc").action == DiscPromptAction.ABORT

def test_ui_adapter_cover_art_storage(tmp_path):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.adapter import GuiInteractionAdapter

    adapter = GuiInteractionAdapter()
    assert adapter.get_cover_art() is None
    adapter.set_cover_art(tmp_path / "custom.jpg")
    assert adapter.get_cover_art() == tmp_path / "custom.jpg"


def test_single_instance_guard_prevents_second_launch(temp_env, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QLockFile
    from PyQt6.QtWidgets import QMessageBox
    from whipit.ui.gui.app import main

    monkeypatch.setattr(QLockFile, "tryLock", lambda self, *a: False)

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: warned.append(a))

    res = main(["whipit"])
    assert res == 0
    assert len(warned) == 1
    assert "Already Running" in warned[0][1]


def test_start_rip_rejects_out_of_range_offset(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: warned.append(a))

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.offset_input.setText("99999")
        window.start_rip()
        assert len(warned) == 1
        assert "between -10000 and 10000" in warned[0][2]
    finally:
        window.close()

def test_main_window_labels_enforce_plain_text_and_safe_urls():
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        assert window.album_title_lbl.textFormat() == Qt.TextFormat.PlainText
        assert window.phase_detail_lbl.textFormat() == Qt.TextFormat.PlainText
        assert window.verification_lbl.textFormat() == Qt.TextFormat.PlainText
        assert window.disc_status_badge.textFormat() == Qt.TextFormat.PlainText
        assert window._is_safe_url("smb://x/discogs.com") is False
        assert window._is_safe_url("https://www.discogs.com/release/1") is True
        assert window._is_safe_url("https://musicbrainz.org/release/1") is True
        assert window._is_safe_url(r"https://evil.example\@discogs.com/x") is False
        assert window._is_safe_url("https://user:pass@discogs.com/release/1") is False
    finally:
        window.close()


def test_main_window_reset_disc_state_clears_cover_art(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        art_file = tmp_path / "disc1.jpg"
        art_file.touch()
        window._current_art_path = art_file
        window._downloaded_art_path = art_file
        window._art_is_user_selected = True
        window.adapter.set_cover_art(art_file)

        window._reset_disc_state(clear_user_art=True)
        assert window._current_art_path is None
        assert window._downloaded_art_path is None
        assert window._art_is_user_selected is False
        assert window.adapter.get_cover_art() is None
    finally:
        window.close()


def test_cover_failed_clears_stale_art(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        art_file = tmp_path / "stale.jpg"
        art_file.touch()
        window._current_art_path = art_file
        window.adapter.set_cover_art(art_file)

        window._on_cover_art_failed("dummy-mbid")
        assert window._current_art_path is None
        assert window.adapter.get_cover_art() is None
    finally:
        window.close()


def test_gui_prompt_select_release_cancel_returns_none(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QInputDialog
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("", False))

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        releases = [
            ReleaseOption(mbid="mbid-1", title="Release 1"),
            ReleaseOption(mbid="mbid-2", title="Release 2"),
        ]
        assert window._dispatch_modal_prompt("prompt_select_release", releases) is None
    finally:
        window.close()


def test_gui_select_loaded_drive_cancel_returns_none(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QInputDialog
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("", False))

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        drives = [("/dev/sr0", "Drive 1"), ("/dev/sr1", "Drive 2")]
        assert window._dispatch_modal_prompt("select_loaded_drive", drives) is None
    finally:
        window.close()


def test_refresh_action_buttons_disables_start_when_dependencies_missing(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        monkeypatch.setattr("whipit.ui.gui.main_window.get_missing_dependencies", lambda: ["whipper", "loudgain"])
        window._refresh_action_buttons()
        assert not window.start_btn.isEnabled()
        assert not window.identify_btn.isEnabled()

        monkeypatch.setattr("whipit.ui.gui.main_window.get_missing_dependencies", lambda: [])
        window._refresh_action_buttons()
        assert window.start_btn.isEnabled()
        assert window.identify_btn.isEnabled()
    finally:
        window.close()


def test_eject_worker_runs_off_main_thread(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import EjectWorker

    app = QApplication.instance() or QApplication([])
    ejected = []
    monkeypatch.setattr("whipit.ui.gui.main_window.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ui.gui.worker.eject_drive", lambda dev: ejected.append(dev))

    worker = EjectWorker("/dev/sr0")
    results = []
    worker.eject_finished.connect(results.append)
    worker.start()
    while worker.isRunning() or not results:
        app.processEvents()
        worker.wait(20)

    assert ejected == ["/dev/sr0"]
    assert results == ["/dev/sr0"]


def test_discard_worker_runs_off_main_thread(sample_manifest, temp_env):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import DiscardWorker

    app = QApplication.instance() or QApplication([])
    store = ManifestStore()
    store.save(sample_manifest)
    coordinator = RecoveryCoordinator(store)

    worker = DiscardWorker(coordinator, sample_manifest.job_id, "Test Album")
    finished = []
    worker.discard_finished.connect(lambda ok, acts, stem: finished.append((ok, stem)))
    worker.start()
    while worker.isRunning() or not finished:
        app.processEvents()
        worker.wait(20)

    assert len(finished) == 1
    assert finished[0][0] is True
    assert finished[0][1] == "Test Album"


def test_main_window_close_event_gracefully_handles_running_workers(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    class SlowWorker(QThread):
        def run(self):
            time.sleep(0.3)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    slow_worker = SlowWorker(parent=None)
    slow_worker.start()
    window.details_worker = slow_worker

    window.close()
    assert not slow_worker.isRunning()


def test_pipeline_badge_on_abort_marks_current_step_or_resets():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow
    from whipit.models import JobManifest, DeviceInfo, PathLayout, WorkflowState
    from whipit.config import get_cache_dir

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window._set_pipeline_step("RIPPING")
        assert window._current_step_key == "RIPPING"
        assert window.badge_labels["RIPPING"].objectName() == "stepActive"
        assert window.badge_labels["PUBLISHING"].objectName() == "stepInactive"

        dummy_manifest = JobManifest(
            job_id="test-abort-badge",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
            paths=PathLayout(
                output_root=Path("/tmp/whipit_test_badge"),
                whipper_stage=get_cache_dir() / "stage_test-abort-badge",
                candidate_stage=Path("/tmp/whipit_test_badge/.whipit-stage.test-abort-badge"),
            ),
        )
        window._on_state_changed(WorkflowState.ABORTED, dummy_manifest)
        assert window.badge_labels["RIPPING"].objectName() == "stepError"
        assert window.badge_labels["PUBLISHING"].objectName() == "stepInactive"

        window._on_workflow_finished(False, "Rip cancelled by user.")
        assert window.badge_labels["RIPPING"].objectName() == "stepInactive"
        assert window.badge_labels["PUBLISHING"].objectName() == "stepInactive"
    finally:
        window.close()


def test_startup_check_worker_handles_unexpected_exception(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.main_window import StartupCheckWorker
    from whipit.ui.base import NullInteractionAdapter

    def failing_restore():
        raise RuntimeError("Simulated unhandled startup failure")

    monkeypatch.setattr("whipit.inhibit.MediaDialogInhibitor.restore_persisted_state", failing_restore)

    worker = StartupCheckWorker(NullInteractionAdapter(), Path("/tmp"))
    results = []
    worker.checks_completed.connect(lambda ok, m, p, a: results.append((ok, a)))
    worker.run()

    assert len(results) == 1
    assert results[0][0] is False
    assert any("Startup checks failed" in act for act in results[0][1])


def test_on_whipper_output_does_not_clobber_album_title_with_track_title():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window.album_title_lbl.setText("Pink Floyd — The Wall")
        window._on_whipper_output("Copying track 02: Title: Comfortably Numb\n")
        assert window.album_title_lbl.text() == "Pink Floyd — The Wall"
    finally:
        window.close()


def test_choose_custom_art_rejects_oversized_file(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox
    from whipit.ui.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    huge_file = tmp_path / "huge.jpg"
    with open(huge_file, "wb") as f:
        f.seek(31 * 1024 * 1024)
        f.write(b"\0")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(huge_file), ""))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: warned.append(a))

    window = MainWindow()
    try:
        window._choose_custom_art()
        assert len(warned) == 1
        assert "30 MB" in warned[0][2]
        assert window.adapter.get_cover_art() is None
    finally:
        window.close()


def test_choose_custom_art_reencodes_mismatched_image(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication, QFileDialog
    from whipit.ui.gui.main_window import MainWindow
    from whipit.audio.metadata import _image_ext

    app = QApplication.instance() or QApplication([])
    fake_jpg = tmp_path / "actually_png.jpg"
    img = QImage(100, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    img.save(str(fake_jpg), "PNG")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(fake_jpg), ""))

    window = MainWindow()
    try:
        window._choose_custom_art()
        selected_art = window.adapter.get_cover_art()
        assert selected_art is not None
        assert selected_art.exists()
        assert selected_art != fake_jpg
        assert selected_art.name.startswith("custom_")
        assert selected_art.suffix == ".jpg"
        with open(selected_art, "rb") as fh:
            assert _image_ext(fh.read(16)) == ".jpg"
    finally:
        window.close()
