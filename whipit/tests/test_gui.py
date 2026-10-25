import pytest
from pathlib import Path

from whipit.config import UserPreferences, load_preferences, save_preferences
from whipit.ui.gui.dialogs import PreferencesDialog
from whipit.ui.gui.main_window import MainWindow

_ORIG_STARTUP_CHECKS = MainWindow._startup_checks


@pytest.fixture
def qapp():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def disable_background_drive_polling(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)


def test_preferences_dialog_initial_values_and_toggle(qapp, temp_env):
    custom_flac = (temp_env["root"] / "FLAC_INIT").resolve()
    custom_opus = (temp_env["root"] / "OPUS_INIT").resolve()

    initial_prefs = UserPreferences(
        flac_root=custom_flac,
        opus_root=custom_opus,
        generate_opus=False,
        default_speed=4,
        offset_override=667,
        auto_eject=False,
    )

    dlg = PreferencesDialog(prefs=initial_prefs)
    assert dlg.flac_edit.text() == str(custom_flac)
    assert dlg.opus_edit.text() == str(custom_opus)
    assert dlg.opus_chk.isChecked() is False
    assert dlg.opus_edit.isEnabled() is False
    assert dlg.speed_combo.currentData() == 4
    assert dlg.offset_edit.text() == "667"
    assert dlg.eject_chk.isChecked() is False

    # Toggling Opus checkbox enables Opus input
    dlg.opus_chk.setChecked(True)
    assert dlg.opus_edit.isEnabled() is True


def test_preferences_dialog_save_persists(qapp, temp_env):
    target_flac = (temp_env["root"] / "SAVED_FLAC").resolve()
    target_opus = (temp_env["root"] / "SAVED_OPUS").resolve()

    dlg = PreferencesDialog()
    dlg.flac_edit.setText(str(target_flac))
    dlg.opus_edit.setText(str(target_opus))
    dlg.opus_chk.setChecked(True)
    dlg.offset_edit.setText("-12")
    dlg.eject_chk.setChecked(False)

    speed_idx = dlg.speed_combo.findData(8)
    assert speed_idx >= 0
    dlg.speed_combo.setCurrentIndex(speed_idx)

    dlg._on_save()
    assert dlg.saved_preferences is not None
    assert dlg.saved_preferences.flac_root == target_flac
    assert dlg.saved_preferences.opus_root == target_opus
    assert dlg.saved_preferences.generate_opus is True
    assert dlg.saved_preferences.default_speed == 8
    assert dlg.saved_preferences.offset_override == -12
    assert dlg.saved_preferences.auto_eject is False

    # Confirm on-disk persistence
    loaded = load_preferences()
    assert loaded.flac_root == target_flac
    assert loaded.default_speed == 8
    assert loaded.offset_override == -12
    assert loaded.auto_eject is False


def test_main_window_applies_preferences(win, temp_env):
    custom_flac = (temp_env["root"] / "STARTUP_FLAC").resolve()
    custom_opus = (temp_env["root"] / "STARTUP_OPUS").resolve()

    new_prefs = UserPreferences(
        flac_root=custom_flac,
        opus_root=custom_opus,
        generate_opus=False,
        default_speed=12,
        offset_override=6,
        auto_eject=False,
    )
    win._apply_preferences(new_prefs)

    assert win.prefs.flac_root == custom_flac
    assert win.prefs.default_speed == 12
    assert win.prefs.offset_override == 6
    assert "Disabled" in win.opus_progress_bar.format()


def test_main_window_start_rip_passes_preferences(win, monkeypatch):
    spawned_args = []

    class MockWorker:
        def __init__(self, **kwargs):
            spawned_args.append(kwargs)
            self.workflow_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()
        def start(self):
            pass
        def isRunning(self):
            return False

    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", MockWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    win.prefs.default_speed = 8
    win.prefs.offset_override = 12
    win.prefs.generate_opus = False
    win.prefs.auto_eject = False
    win.start_rip()

    assert len(spawned_args) == 1
    args = spawned_args[0]
    assert args["speed"] == 8
    assert args["manual_offset"] == 12
    assert args["generate_opus"] is False
    assert args["auto_eject"] is False
    assert args["flac_root"] == win.prefs.flac_root
    assert args["opus_root"] == win.prefs.opus_root


def test_main_window_startup_checks_uses_prefs_flac_root(win, monkeypatch):
    captured_roots = []

    class DummyStartupWorker:
        def __init__(self, ui, out_root, parent=None):
            captured_roots.append(out_root)
            self.checks_completed = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()

        def start(self):
            pass

        def isRunning(self):
            return False

        def deleteLater(self):
            pass

    monkeypatch.setattr("whipit.ui.gui.main_window.StartupCheckWorker", DummyStartupWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)
    _ORIG_STARTUP_CHECKS(win)
    assert len(captured_roots) == 1
    assert captured_roots[0] == win.prefs.flac_root.resolve()
