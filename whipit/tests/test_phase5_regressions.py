"""Regression tests for Phase 5 GUI prompt dispatching, thread lifecycle, session metadata, and close handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from whipit.models import ReleaseOption
from whipit.ripping.calibration import CalibrationAction


def test_dispatch_modal_prompt_handles_reload_cdr_unknown_and_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QInputDialog, QMessageBox

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    monkeypatch.setattr("whipit.ui.gui.drive_panel.prompt_wait_for_reload_dialog", lambda p, d, m: True)
    assert panel._dispatch_modal_prompt("wait_for_user_reload", ("/dev/sr0", "Insert CD")) is True

    monkeypatch.setattr(QMessageBox, "exec", lambda self: int(QMessageBox.StandardButton.Yes))
    assert panel._dispatch_modal_prompt("prompt_cdr_detected", "/dev/sr0") is True
    assert panel._dispatch_modal_prompt("prompt_unknown_disc", None) is True
    assert panel._dispatch_modal_prompt("confirm_install_dependencies", (["flac"], "apt install flac", "apt")) is True
    assert panel._dispatch_modal_prompt("confirm_purge_stage", (Path("/tmp/stage"), 3)) is True

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("/dev/sr1 (DRIVE_1)", True))
    assert (
        panel._dispatch_modal_prompt("select_loaded_drive", [("/dev/sr0", "DRIVE_0"), ("/dev/sr1", "DRIVE_1")])
        == "/dev/sr1"
    )

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("8x (Moderate)", True))
    assert panel._dispatch_modal_prompt("prompt_extraction_speed", None) == 8


def test_prompt_calibration_retry_dialog_forwards_error_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QDialog, QLabel

    from whipit.ui.gui.dialogs import prompt_calibration_retry_dialog

    _ = QApplication.instance() or QApplication([])
    captured_labels: list[str] = []
    orig_exec = QDialog.exec

    def spy_exec(self: QDialog) -> int:
        for lbl in self.findChildren(QLabel):
            captured_labels.append(lbl.text())
        return int(QDialog.DialogCode.Accepted)

    assert orig_exec is not None
    monkeypatch.setattr(QDialog, "exec", spy_exec)
    choice = prompt_calibration_retry_dialog(None, "/dev/sr0", error_reason="Specific drive read error")
    assert choice.action == CalibrationAction.MANUAL_OFFSET
    assert any("Specific drive read error" in txt for txt in captured_labels)


def test_eject_drive_preserves_detached_worker_cleanup_on_identify_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    class FakeIdentifyWorker(QObject):
        line_received = pyqtSignal(str)
        inspection_completed = pyqtSignal(object)
        finished = pyqtSignal()

        def __init__(self) -> None:
            super().__init__()
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

        def isRunning(self) -> bool:
            return False

    fake = FakeIdentifyWorker()
    detached_cleaned = []
    fake.line_received.connect(panel.log_emitted.emit)
    fake.finished.connect(panel._clear_identify_worker)
    fake.finished.connect(lambda: detached_cleaned.append(True))
    panel.identify_worker = fake  # type: ignore[assignment]

    panel.eject_drive()
    assert fake.cancelled is True
    assert panel.identify_worker is None
    fake.finished.emit()
    assert detached_cleaned == [True]


def test_calibration_disables_drive_controls_until_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    panel.session.is_disc_loaded = True

    class DummyCalWorker(QObject):
        calibration_finished = pyqtSignal(bool, object, str)
        finished = pyqtSignal()

        def __init__(self, dev: str, adapter: object, parent: object = None) -> None:
            super().__init__()
            self.running = False

        def start(self) -> None:
            self.running = True

        def isRunning(self) -> bool:
            return self.running

    monkeypatch.setattr("whipit.ui.gui.main_window.CalibrationWorker", DummyCalWorker, raising=False)
    panel._on_calibrate_clicked()
    assert panel.start_btn.isEnabled() is False
    assert panel.identify_btn.isEnabled() is False
    assert panel.calibrate_btn.isEnabled() is False

    panel.calibrate_worker.running = False
    panel.calibrate_worker.calibration_finished.emit(True, 6, "OK")
    panel.calibrate_worker.finished.emit()
    assert panel.calibrate_btn.isEnabled() is True
    assert panel.identify_btn.isEnabled() is True
    assert panel.start_btn.isEnabled() is True


def test_active_session_to_album_metadata_preserves_country_and_cat_no() -> None:
    from whipit.ui.gui.session import ActiveSession

    session = ActiveSession()
    session.select_release(
        ReleaseOption(
            mbid="11111111-2222-3333-4444-555555555555",
            artist="Pink Floyd",
            title="Animals",
            country="UK",
            cat_no="SHVL 815",
            year="1977",
        )
    )
    meta = session.to_album_metadata()
    assert meta.country == "UK"
    assert meta.cat_no == "SHVL 815"
    assert meta.year == "1977"


def test_close_event_cancels_active_rip_without_duplicate_panel_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])
    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: [("/dev/sr0", "DRIVE_A")])

    win = MainWindow()
    try:
        panel = win.drive_panels["/dev/sr0"]
        calls = {"cancel": 0, "wait": 0, "prompts": 0}

        class RunningRipWorker:
            def isRunning(self) -> bool:
                return True

            def cancel(self) -> None:
                calls["cancel"] += 1

            def wait(self, msecs: int = 0) -> bool:
                calls["wait"] += 1
                return True

        panel.worker = RunningRipWorker()  # type: ignore[assignment]

        def mock_question(*a: object, **kw: object) -> QMessageBox.StandardButton:
            calls["prompts"] += 1
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", mock_question)
        ev = QCloseEvent()
        win.closeEvent(ev)
        assert ev.isAccepted() is True
        assert calls["prompts"] == 1
        assert calls["cancel"] == 1
        assert calls["wait"] == 1
    finally:
        win.deleteLater()
