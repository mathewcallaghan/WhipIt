
import pytest

from whipit.config import UserPreferences, load_preferences
from whipit.models import WorkflowState
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
    assert dlg.opus_bitrate_combo.isEnabled() is False
    assert dlg.opus_bitrate_combo.currentData() == 192
    assert dlg.speed_combo.currentData() == 4
    assert dlg.offset_edit.text() == "667"
    assert dlg.eject_chk.isChecked() is False

    # Toggling Opus checkbox enables Opus input and bitrate selector
    dlg.opus_chk.setChecked(True)
    assert dlg.opus_edit.isEnabled() is True
    assert dlg.opus_bitrate_combo.isEnabled() is True


def test_preferences_dialog_save_persists(qapp, temp_env):
    target_flac = (temp_env["root"] / "SAVED_FLAC").resolve()
    target_opus = (temp_env["root"] / "SAVED_OPUS").resolve()

    dlg = PreferencesDialog()
    dlg.flac_edit.setText(str(target_flac))
    dlg.opus_edit.setText(str(target_opus))
    dlg.opus_chk.setChecked(True)
    br_idx = dlg.opus_bitrate_combo.findData(256)
    assert br_idx >= 0
    dlg.opus_bitrate_combo.setCurrentIndex(br_idx)
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
    assert dlg.saved_preferences.opus_bitrate == 256
    assert dlg.saved_preferences.default_speed == 8
    assert dlg.saved_preferences.offset_override == -12
    assert dlg.saved_preferences.auto_eject is False

    # Confirm on-disk persistence
    loaded = load_preferences()
    assert loaded.flac_root == target_flac
    assert loaded.opus_bitrate == 256
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
    win.prefs.opus_bitrate = 256
    win.prefs.max_retries = 30
    win.prefs.auto_eject = False
    win.start_rip()

    assert len(spawned_args) == 1
    args = spawned_args[0]
    assert args["speed"] == 8
    assert args["manual_offset"] == 12
    assert args["generate_opus"] is False
    assert args["opus_bitrate"] == 256
    assert args["max_retries"] == 30
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


def test_accuraterip_verification_badge_displayed_on_state_changed(win, sample_manifest):
    sample_manifest.verification.is_accurate = True
    sample_manifest.verification.accuraterip_summary = "All tracks accurately ripped"
    sample_manifest.verification.health_status = "No errors occurred"

    win._on_state_changed(WorkflowState.RIP_VALIDATED, sample_manifest)

    assert win.verification_lbl.isHidden() is False
    assert "AccurateRip: Verified" in win.verification_lbl.text()


def test_accuraterip_unverified_badge_displayed_on_mismatch(win, sample_manifest):
    sample_manifest.verification.is_accurate = False
    sample_manifest.verification.accuraterip_summary = "Track not present in database"

    win._on_state_changed(WorkflowState.RIP_VALIDATED, sample_manifest)

    assert win.verification_lbl.isHidden() is False
    assert "not present in database" in win.verification_lbl.text().lower()


def test_whipper_stream_accuraterip_event_updates_verification_lbl(win):
    win._on_whipper_output("AccurateRip summary: All tracks accurately ripped\n")
    assert win.verification_lbl.isHidden() is False
    assert "AccurateRip: Verified" in win.verification_lbl.text()


def test_accuraterip_partial_match_pressing_variant_clarity(win, sample_manifest):
    sample_manifest.verification.is_accurate = False
    sample_manifest.verification.accuraterip_summary = (
        "Some tracks could not be verified as accurate (1/12 got no match)"
    )
    win._on_state_changed(WorkflowState.RIP_VALIDATED, sample_manifest)
    assert win.verification_lbl.isHidden() is False
    assert "11/12 Verified" in win.verification_lbl.text()
    assert "not in DB" in win.verification_lbl.text()


def test_pro_audio_qss_theme_tokens_applied(win):
    from whipit.ui.gui.styles import (
        COLOR_ACCENT_AMBER,
        STATUS_BADGE_DATA,
        VERIFICATION_BADGE_UNVERIFIED,
        get_application_stylesheet,
    )

    qss = get_application_stylesheet()
    for token in ("#18181C", "#222228", "#33333C", "#00E5FF", "#30D158", "#FF453A", "QFrame#pipelineFrame"):
        assert token in qss
    assert COLOR_ACCENT_AMBER == "#FF9F0A"
    assert "#FF9F0A" in STATUS_BADGE_DATA
    assert "#FF9F0A" in VERIFICATION_BADGE_UNVERIFIED
    assert "#18181C" in win.styleSheet()
    assert "QWidget#mainScrollContent" in qss
    assert "QWidget#drivePanel" in qss

    from PyQt6.QtCore import Qt

    scroll_child = win.centralWidget().widget()
    assert scroll_child is not None
    assert scroll_child.objectName() == "mainScrollContent"
    assert win.drive_panel.objectName() == "drivePanel"
    assert win.drive_panel.testAttribute(Qt.WidgetAttribute.WA_StyledBackground) is True
    assert "&&" in win.console_toggle_btn.text()


def test_override_checkboxes_have_plain_labels_tooltips_and_auto_uncataloged(win) -> None:
    from whipit.ripping.identify import DiscInspectionResult

    for chk in (win.keep_going_chk, win.unknown_chk, win.overwrite_chk):
        assert "--keep-going" not in chk.text()
        assert "(-U)" not in chk.text()
        assert bool(chk.toolTip().strip())

    uncat = DiscInspectionResult(
        success=False,
        tracks_count=10,
        mb_lookup_url="https://musicbrainz.org/cdtoc/attach?toc=1+10+200000",
        error_message="Disc detected, but not catalogued in MusicBrainz database.",
    )
    win._on_identify_finished(uncat, silent=True)
    assert win.unknown_chk.isChecked() is True

    cat = DiscInspectionResult(
        success=True,
        artist="Radiohead",
        title="OK Computer",
        mbid="12345678-1234-1234-1234-123456789abc",
        mb_url="https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc",
    )
    win._on_identify_finished(cat, silent=True)
    assert win.unknown_chk.isChecked() is False


def test_preferences_dialog_opus_bitrate_roundtrip_and_toggle(qapp, temp_env):
    """Verifies Opus bitrate dropdown initialization, enable toggle, and disk persistence."""
    initial_prefs = UserPreferences(
        generate_opus=True,
        opus_bitrate=256,
    )

    dlg = PreferencesDialog(prefs=initial_prefs)
    assert dlg.opus_bitrate_combo.isEnabled() is True
    assert dlg.opus_bitrate_combo.currentData() == 256

    # Select 128 kbps and save
    idx = dlg.opus_bitrate_combo.findData(128)
    assert idx >= 0
    dlg.opus_bitrate_combo.setCurrentIndex(idx)

    dlg._on_save()
    assert dlg.saved_preferences is not None
    assert dlg.saved_preferences.opus_bitrate == 128

    # Verify on-disk persistence
    loaded = load_preferences()
    assert loaded.opus_bitrate == 128

    # Verify unchecking Opus disables bitrate combo
    dlg.opus_chk.setChecked(False)
    assert dlg.opus_bitrate_combo.isEnabled() is False


def test_preferences_dialog_max_retries_ui(qapp, temp_env):
    initial_prefs = UserPreferences(max_retries=30)
    dlg = PreferencesDialog(prefs=initial_prefs)
    assert dlg.retries_combo.currentData() == 30

    r_idx = dlg.retries_combo.findData(20)
    assert r_idx >= 0
    dlg.retries_combo.setCurrentIndex(r_idx)
    dlg._on_save()

    assert dlg.saved_preferences is not None
    assert dlg.saved_preferences.max_retries == 20
    assert load_preferences().max_retries == 20


def test_close_drive_uses_eject_t_and_falls_back_to_toggle(monkeypatch):
    import subprocess

    from whipit.config import close_drive

    cmds = []

    def mock_run(cmd, *a, **kw):
        cmds.append(cmd)
        if "-t" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("whipit.services.drive_service.unlock_drive", lambda dev: None)
    monkeypatch.setattr("subprocess.run", mock_run)

    close_drive("/dev/sr0")
    assert cmds == [["eject", "-t", "/dev/sr0"], ["eject", "-T", "/dev/sr0"]]


def test_drive_panel_toggle_eject_close_and_poll_sync(qapp, monkeypatch):
    from whipit.config import CDS_AUDIO, CDS_DISC_OK, CDS_NO_DISC, CDS_NO_INFO, CDS_TRAY_OPEN
    from whipit.ui.gui.drive_panel import DrivePanel

    panel = DrivePanel(device="/dev/sr0")
    ejected = []
    closed = []

    class FakeEjectWorker:
        def __init__(self, dev, parent=None):
            self.eject_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()

        def start(self):
            ejected.append(True)

        def isRunning(self):
            return False

    class _FakeSig:
        def __init__(self):
            self._cbs = []

        def connect(self, fn):
            self._cbs.append(fn)

        def emit(self, *args):
            for fn in list(self._cbs):
                fn(*args)

    class FakeCloseWorker:
        def __init__(self, dev, parent=None):
            self._dev = dev
            self.close_finished = _FakeSig()
            self.finished = _FakeSig()

        def start(self):
            closed.append(self._dev)
            self.close_finished.emit(self._dev)
            self.finished.emit()

        def isRunning(self):
            return False

    monkeypatch.setattr("whipit.ui.gui.main_window.EjectWorker", FakeEjectWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window.CloseWorker", FakeCloseWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    assert panel.is_tray_open is False
    assert "Eject" in panel.eject_btn.text()

    panel.toggle_eject_close()
    assert ejected == [True]
    assert panel.is_tray_open is True
    assert "Close Tray" in panel.eject_btn.text()

    panel.toggle_eject_close()
    assert closed == ["/dev/sr0"]
    assert panel.is_tray_open is False
    assert "Eject" in panel.eject_btn.text()

    # Physical tray open via poll switches to Close Tray, and pushing shut resets to Eject
    panel.handle_drive_status(CDS_TRAY_OPEN, CDS_NO_INFO)
    assert panel.is_tray_open is True
    assert "Close Tray" in panel.eject_btn.text()
    assert panel.disc_status_badge.text() == "Tray Open"

    panel.handle_drive_status(CDS_NO_DISC, CDS_NO_INFO)
    assert panel.is_tray_open is False
    assert "Eject" in panel.eject_btn.text()

    # Inserting an Audio CD ensures tray is marked closed
    monkeypatch.setattr(panel, "identify_disc", lambda silent=True: None)
    panel.handle_drive_status(CDS_TRAY_OPEN, CDS_NO_INFO)
    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert panel.is_tray_open is False


def test_mainwindow_batch_eject_and_close_toggle(qapp, monkeypatch):
    monkeypatch.setattr(
        "whipit.ui.gui.main_window.get_drives_with_hardware_ids",
        lambda: [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")],
    )

    win = MainWindow()
    try:
        p0 = win.drive_panels["/dev/sr0"]
        p1 = win.drive_panels["/dev/sr1"]

        calls = []
        monkeypatch.setattr(p0, "eject_drive", lambda: (setattr(p0, "is_tray_open", True), calls.append(("eject", "/dev/sr0"))))
        monkeypatch.setattr(p1, "eject_drive", lambda: (setattr(p1, "is_tray_open", True), calls.append(("eject", "/dev/sr1"))))
        monkeypatch.setattr(p0, "close_drive_tray", lambda: (setattr(p0, "is_tray_open", False), calls.append(("close", "/dev/sr0"))))
        monkeypatch.setattr(p1, "close_drive_tray", lambda: (setattr(p1, "is_tray_open", False), calls.append(("close", "/dev/sr1"))))

        assert "Eject All Drives" in win.eject_all_btn.text()
        win.eject_all_drives()
        assert calls == [("eject", "/dev/sr0"), ("eject", "/dev/sr1")]
        assert "Close All Trays" in win.eject_all_btn.text()

        calls.clear()
        win.eject_all_drives()
        assert calls == [("close", "/dev/sr0"), ("close", "/dev/sr1")]
        assert "Eject All Drives" in win.eject_all_btn.text()
    finally:
        win.close()
        win.deleteLater()


def test_tray_controls_decoupled_from_post_processing(qapp, sample_manifest, monkeypatch):
    from whipit.ui.gui.drive_panel import DrivePanel

    panel = DrivePanel(device="/dev/sr0")
    panel.prefs.auto_eject = True
    panel.session.is_disc_loaded = True
    panel.session.artist = "Pink Floyd"
    panel.session.title = "The Wall"

    closed = []

    class FakeRunningRipWorker:
        def __init__(self, **kwargs):
            self.workflow_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()

        def start(self):
            pass

        def isRunning(self):
            return True

    class _FakeSig:
        def __init__(self):
            self._cbs = []

        def connect(self, fn):
            self._cbs.append(fn)

        def emit(self, *args):
            for fn in list(self._cbs):
                fn(*args)

    class FakeCloseWorker:
        def __init__(self, dev, parent=None):
            self._dev = dev
            self.close_finished = _FakeSig()
            self.finished = _FakeSig()

        def start(self):
            closed.append(self._dev)
            self.close_finished.emit(self._dev)
            self.finished.emit()

        def isRunning(self):
            return False

    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", FakeRunningRipWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window.CloseWorker", FakeCloseWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    # 1. During active CD sector extraction, tray controls are blocked
    panel.start_rip()
    assert panel.is_extracting_audio is True
    assert panel.eject_btn.isEnabled() is False

    # 2. Once extraction completes (RIP_EXTRACTED -> Opus encoding), tray controls re-enable immediately
    panel._on_state_changed(WorkflowState.RIP_EXTRACTED, sample_manifest)
    assert panel.is_extracting_audio is False
    assert panel.is_tray_open is True
    assert panel.eject_btn.isEnabled() is True
    assert "Close Tray" in panel.eject_btn.text()

    # 3. User can close tray while RipWorker is still post-processing in background
    panel.toggle_eject_close()
    assert closed == ["/dev/sr0"]
    assert panel.is_tray_open is False
    assert panel.eject_btn.isEnabled() is True
    assert panel.session.title == "The Wall"


def test_mainwindow_multidisc_auto_assigns_standalone_disc_numbers(qapp, temp_env, monkeypatch):
    from whipit.config import get_cache_dir
    from whipit.models import DeviceInfo, JobManifest, PathLayout
    from whipit.services.publication_service import PublicationService

    monkeypatch.setattr(
        "whipit.ui.gui.main_window.get_drives_with_hardware_ids",
        lambda: [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")],
    )

    win = MainWindow()
    try:
        p0 = win.drive_panels["/dev/sr0"]
        p1 = win.drive_panels["/dev/sr1"]

        # Scenario A: Disc 1 matched as standalone release (disc_number=None, total_discs=1), Disc 2 is 2/2
        p0.session.artist = "Gloria Estefan"
        p0.session.title = "Into the Light"
        p0.session.disc_number = None
        p0.session.total_discs = 1
        p0.session.mbid = "11111111-1111-1111-1111-111111111111"

        p1.session.artist = "Gloria Estefan"
        p1.session.title = "Into the Light"
        p1.session.disc_number = 2
        p1.session.total_discs = 2
        p1.session.mbid = "22222222-2222-2222-2222-222222222222"

        win._update_batch_bar()

        assert p0.session.disc_number == 1
        assert p0.session.total_discs == 2
        assert p0.session.is_multi_disc is True
        assert "(Disc 1)" in p0.album_title_lbl.text()
        assert p1.session.disc_number == 2
        assert p1.session.total_discs == 2
        assert p0.session.mb_release_overridden is True
        assert p1.session.mb_release_overridden is True

        flac_root = temp_env["output"]
        m0 = JobManifest(
            job_id="test-disc1-subfolder",
            device=DeviceInfo(path="/dev/sr0", hardware_id="DRIVE_A"),
            paths=PathLayout(
                flac_root=flac_root,
                opus_root=flac_root,
                generate_opus=False,
                whipper_stage=get_cache_dir() / "stage_test-disc1-subfolder",
                candidate_stage=flac_root / ".whipit-stage.test-disc1-subfolder",
            ),
        )
        PublicationService.resolve_paths(m0, p0.session.to_album_metadata(), flac_root, None, False)
        assert m0.paths.final_flac_dir is not None
        assert m0.paths.final_flac_dir.name == "Disc 1"

        # Scenario B: Disc 2 is in first drive and standalone Disc 1 is in second drive (no collision)
        p0.session.disc_number = 2
        p0.session.total_discs = 2
        p1.session.disc_number = None
        p1.session.total_discs = 1

        win._update_batch_bar()
        assert p0.session.disc_number == 2
        assert p1.session.disc_number == 1
        assert p1.session.total_discs == 2
        assert p1.session.is_multi_disc is True
        assert "(Disc 1)" in p1.album_title_lbl.text()
    finally:
        win.close()
        win.deleteLater()
