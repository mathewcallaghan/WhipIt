import pytest

from whipit.inhibit import suppress_media_dialogs


@pytest.fixture(autouse=True)
def disable_background_drive_polling(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)


def test_inhibit_pool_reference_counting(monkeypatch):
    """Verifies that multiple concurrent rips keep automount suppressed until the last rip exits."""
    started_count = 0
    stopped_count = 0

    class MockInhibitor:
        def __init__(self, dev=None):
            self.device = dev

        def start(self, ui=None):
            nonlocal started_count
            started_count += 1

        def stop(self):
            nonlocal stopped_count
            stopped_count += 1

    monkeypatch.setattr("whipit.inhibit.MediaDialogInhibitor", MockInhibitor)
    monkeypatch.setattr("whipit.inhibit.unmount_cdda_device", lambda dev: None)

    # 1. First rip starts -> inhibitor started (count = 1)
    with suppress_media_dialogs("/dev/sr0") as inh1:
        assert started_count == 1
        assert stopped_count == 0

        # 2. Second rip starts concurrently -> inhibitor reused, NOT restarted (count = 2)
        with suppress_media_dialogs("/dev/sr1") as inh2:
            assert inh1 is inh2
            assert started_count == 1
            assert stopped_count == 0

        # 3. Second rip finishes -> inhibitor NOT stopped yet because First rip is still active
        assert started_count == 1
        assert stopped_count == 0

    # 4. First rip finishes -> all rips done -> inhibitor stopped exactly once
    assert started_count == 1
    assert stopped_count == 1


def test_mainwindow_creates_tabs_for_each_detected_drive(monkeypatch):
    """Verifies that MainWindow instantiates independent DrivePanels and tabs for all optical drives."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "ASUS_BW-16D1HT"), ("/dev/sr1", "HL-DT-ST_WH16NS40")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_drive_hardware_id", lambda d: "MOCK_HW")

    win = MainWindow()
    try:
        assert win.tab_widget.count() == 2
        assert "/dev/sr0" in win.drive_panels
        assert "/dev/sr1" in win.drive_panels

        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]
        assert panel0 is not panel1
        assert panel0.device_path == "/dev/sr0"
        assert panel1.device_path == "/dev/sr1"

        win.tab_widget.setCurrentIndex(0)
        assert win.drive_panel is panel0

        win.tab_widget.setCurrentIndex(1)
        assert win.drive_panel is panel1
    finally:
        win.close()
        win.deleteLater()


def test_multi_disc_set_detection(monkeypatch):
    """Verifies that MainWindow detects multi-disc sets loaded across distinct optical drives."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        panel0.session.artist = "Pink Floyd"
        panel0.session.title = "The Wall"
        panel0.session.disc_number = 1

        panel1.session.artist = "Pink Floyd"
        panel1.session.title = "The Wall"
        panel1.session.disc_number = 2

        detected_set = win._detect_multi_disc_sets()
        assert detected_set is not None
        assert "Multi-disc set detected" in detected_set
        assert "The Wall" in detected_set
        assert "Disc 1" in detected_set
        assert "Disc 2" in detected_set
    finally:
        win.close()
        win.deleteLater()


def test_rip_all_ready_discs_batch_action(monkeypatch):
    """Verifies that clicking 'Rip All Ready Discs' triggers start_rip across all loaded drives."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        panel0.session.is_disc_loaded = True
        panel1.session.is_disc_loaded = True

        started_rips = []
        monkeypatch.setattr(panel0, "start_rip", lambda: started_rips.append("/dev/sr0"))
        monkeypatch.setattr(panel1, "start_rip", lambda: started_rips.append("/dev/sr1"))

        win.rip_all_ready_discs()
        assert started_rips == ["/dev/sr0", "/dev/sr1"]
    finally:
        win.close()
        win.deleteLater()


def test_identification_state_disables_start_button_and_shows_progress(monkeypatch):
    """Asserts that starting disc inspection sets button to identifying and disables ripping until ready."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.models import TrackInfo
    from whipit.ripping.identify import DiscInspectionResult
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    class DummyIdentifyWorker:
        def __init__(self, *a, **kw):
            self.line_received = type("Sig", (), {"connect": lambda *s: None})()
            self.inspection_completed = type("Sig", (), {"connect": lambda *s: None})()
            self.finished = type("Sig", (), {"connect": lambda *s: None})()
        def start(self): pass
        def isRunning(self): return True

    monkeypatch.setattr("whipit.ui.gui.main_window.IdentifyWorker", DummyIdentifyWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    panel.identify_disc(silent=True)
    assert "Identifying" in panel.start_btn.text()
    assert panel.start_btn.isEnabled() is False
    assert panel.is_ready_to_rip is False
    assert panel.rip_progress_bar.format() == "Reading Table of Contents & querying MusicBrainz..."

    panel.session.is_disc_loaded = True
    mock_res = DiscInspectionResult(
        success=True,
        artist="Test Artist",
        title="Test Album",
        mbid="mbid-single",
        tracks=[TrackInfo(number=1, title="Track 1", duration="03:00")],
    )
    panel._on_identify_finished(mock_res)
    panel.identify_worker = None
    panel._clear_identify_worker()

    assert panel.start_btn.text() == "Start Rip (sr0)"
    assert panel.start_btn.isEnabled() is True
    assert panel.is_ready_to_rip is True
    assert panel.rip_progress_bar.format() == "Idle"


def test_pressing_selector_dropdown_visibility_multi_vs_single():
    """Verifies that the pressing selection dropdown appears only when multiple releases match."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.models import ReleaseOption
    from whipit.ripping.identify import DiscInspectionResult
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    single_res = DiscInspectionResult(
        success=True,
        artist="The John Butler Trio",
        title="Grand National",
        mbid="mbid-single",
        matching_releases=[ReleaseOption(mbid="mbid-single", title="Grand National")],
    )
    panel._on_identify_finished(single_res)
    assert panel.release_combo_row.isHidden() is True

    multi_releases = [
        ReleaseOption(mbid="mbid-1", title="Grand National", country="AU", year="2007"),
        ReleaseOption(mbid="mbid-2", title="Grand National", country="US", year="2007", cat_no="ATO0037"),
    ]
    multi_res = DiscInspectionResult(
        success=True,
        artist="The John Butler Trio",
        title="Grand National",
        mbid="mbid-1",
        matching_releases=multi_releases,
    )
    panel._on_identify_finished(multi_res)
    assert panel.release_combo_row.isHidden() is False
    assert panel.release_combo.count() == 2
    assert "AU" in panel.release_combo.itemText(0)
    assert "ATO0037" in panel.release_combo.itemText(1)


def test_multi_disc_cover_art_auto_inheritance_across_drives(monkeypatch, tmp_path):
    """Verifies that sibling drives loaded with Disc 2 automatically inherit Disc 1's artwork."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        art_file = tmp_path / "shared_cover.jpg"
        art_file.write_bytes(b"JPEG_ART_DATA")

        panel0.session.artist = "Pink Floyd"
        panel0.session.title = "The Wall"
        panel0.session.disc_number = 1
        panel0.session.set_downloaded_art(art_file)

        panel1.session.artist = "Pink Floyd"
        panel1.session.title = "The Wall"
        panel1.session.disc_number = 2
        panel1.session.clear_art()

        win._update_batch_bar()

        assert panel1.session.art_path == art_file
        assert panel1.session.art_is_user_selected is False
        assert "Inherited cover art from Disc 1" in panel1.phase_detail_lbl.text()
    finally:
        win.close()
        win.deleteLater()


def test_multi_disc_cover_art_user_override_protected(monkeypatch, tmp_path):
    """Verifies that if Disc 2 has a user-selected custom cover art, auto-inheritance never overwrites it."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        art_disc1 = tmp_path / "disc1_cover.jpg"
        art_disc2 = tmp_path / "custom_disc2_override.jpg"
        art_disc1.write_bytes(b"DISC_1_ART")
        art_disc2.write_bytes(b"DISC_2_CUSTOM_ART")

        panel0.session.artist = "Pink Floyd"
        panel0.session.title = "The Wall"
        panel0.session.disc_number = 1
        panel0.session.set_downloaded_art(art_disc1)

        panel1.session.artist = "Pink Floyd"
        panel1.session.title = "The Wall"
        panel1.session.disc_number = 2
        panel1.session.set_user_art(art_disc2)  # User explicitly overrode artwork on Disc 2

        win._update_batch_bar()

        # Disc 2 must retain its user-selected artwork
        assert panel1.session.art_path == art_disc2
        assert panel1.session.art_is_user_selected is True
        assert panel1.session.art_path != art_disc1
    finally:
        win.close()
        win.deleteLater()


def test_mainwindow_auto_switches_tab_on_disc_insertion(monkeypatch):
    """Verifies that inserting a disc auto-switches to that drive tab unless another drive is ripping."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.config import CDS_AUDIO, CDS_DISC_OK
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.DrivePanel.identify_disc", lambda self, silent=True: None)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        # Default tab is sr0
        win.tab_widget.setCurrentWidget(panel0)
        assert win.tab_widget.currentWidget() is panel0

        # Insert disc into sr1 -> auto-activates tab 1 (sr1)
        panel1.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
        assert win.tab_widget.currentWidget() is panel1

        # Simulate active rip on sr1
        class FakeRippingWorker:
            def isRunning(self):
                return True
        panel1.worker = FakeRippingWorker()

        # Insert disc into sr0 while sr1 is ripping -> does NOT steal focus
        panel0.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
        assert win.tab_widget.currentWidget() is panel1

        # Finish rip on sr1
        panel1.worker = None

        # Re-eject and re-insert into sr0 -> now safely switches focus to sr0
        panel0.handle_drive_status(0, 0) # empty
        panel0.handle_drive_status(CDS_DISC_OK, CDS_AUDIO) # inserted
        assert win.tab_widget.currentWidget() is panel0
    finally:
        win.close()
        win.deleteLater()


def test_switch_drive_handoff_clears_old_panel_and_suppresses_identify_on_target(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.config import CDS_AUDIO, CDS_DISC_OK
    from whipit.models import TrackInfo
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        class FakeRipWorker:
            def __init__(self):
                self.workflow_finished = type("Sig", (), {"connect": lambda *a: None, "disconnect": lambda *a: None})()
                self.finished = type("Sig", (), {"connect": lambda *a: None, "disconnect": lambda *a: None})()
            def isRunning(self):
                return True

        fake_worker = FakeRipWorker()
        panel0.worker = fake_worker
        panel0.session.artist = "Pink Floyd"
        panel0.session.title = "Animals"
        panel0.session.tracks = [TrackInfo(number=1, title="Pigs on the Wing 1", duration="01:25")]
        panel0.transition_to(panel0.drive_state.RIPPING)
        orig_adapter = panel0.adapter

        identify_calls = []
        monkeypatch.setattr(panel1, "identify_disc", lambda silent=True: identify_calls.append(silent))

        win._on_panel_device_switched("/dev/sr0", "/dev/sr1")

        # Old drive panel is cleared and idle
        assert panel0.worker is None
        assert panel0.drive_state.value == "EMPTY"
        assert panel0.adapter is not orig_adapter
        assert panel0.cancel_btn.isEnabled() is False
        assert panel0.session.artist == "Unknown Artist"
        assert panel0.session.tracks == []
        assert "Idle" in panel0.rip_progress_bar.format()

        # Target drive panel owns the rip, adapter, and session, and focuses the tab
        assert panel1.worker is fake_worker
        assert panel1.adapter is orig_adapter
        assert panel1.drive_state.value == "RIPPING"
        assert panel1.cancel_btn.isEnabled() is True
        assert panel1.session.artist == "Pink Floyd"
        assert panel1.session.title == "Animals"
        assert panel1.track_table.rowCount() == 1
        assert panel1.track_table.item(0, 1).text() == "Pigs on the Wing 1"
        assert win.tab_widget.currentWidget() is panel1

        # Poll detecting newly loaded Audio CD in sr1 must NOT start IdentifyWorker
        panel1.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
        assert identify_calls == []
    finally:
        panel0.worker = None
        panel1.worker = None
        win.close()
        win.deleteLater()


def test_extraction_error_dialog_excludes_current_drive_and_disables_empty_partial(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QDialog

    from whipit.ui.gui.dialogs import prompt_extraction_error_dialog

    _ = QApplication.instance() or QApplication([])

    seen_combos = []
    seen_partial_enabled = []
    seen_switch_enabled = []

    def spy_exec(self):
        from PyQt6.QtWidgets import QComboBox, QRadioButton
        for cb in self.findChildren(QComboBox):
            seen_combos.append([cb.itemData(i) for i in range(cb.count())])
        for rb in self.findChildren(QRadioButton):
            if "Switch to another" in rb.text():
                seen_switch_enabled.append(rb.isEnabled())
            if "Accept partial rip" in rb.text():
                seen_partial_enabled.append(rb.isEnabled())
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", spy_exec)

    prompt_extraction_error_dialog(
        None,
        "/dev/sr0",
        [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")],
        error_reason="Whipper giving up on track 8 after 10 times.",
        has_partial_tracks=False,
    )
    assert seen_combos == [["/dev/sr1"]]
    assert seen_switch_enabled == [True]
    assert seen_partial_enabled == [False]


def test_insert_disc_dialog_excludes_current_drive(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QComboBox, QDialog

    from whipit.ui.gui.dialogs import prompt_insert_disc_dialog

    _ = QApplication.instance() or QApplication([])
    seen_combos = []

    def spy_exec(self):
        for cb in self.findChildren(QComboBox):
            seen_combos.append([cb.itemData(i) for i in range(cb.count())])
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(QDialog, "exec", spy_exec)
    prompt_insert_disc_dialog(
        None,
        "/dev/sr0",
        "DRIVE_A",
        [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")],
        allow_main_menu=False,
    )
    assert seen_combos == [["/dev/sr1"]]


def test_switch_drive_end_to_end_signal_chain_and_second_switch(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.base import LogLevel
    from whipit.ui.gui.main_window import MainWindow

    _ = QApplication.instance() or QApplication([])

    mock_drives = [("/dev/sr0", "DRIVE_A"), ("/dev/sr1", "DRIVE_B")]
    monkeypatch.setattr("whipit.ui.gui.main_window.get_drives_with_hardware_ids", lambda: mock_drives)

    win = MainWindow()
    try:
        panel0 = win.drive_panels["/dev/sr0"]
        panel1 = win.drive_panels["/dev/sr1"]

        class FakeRipWorker:
            def __init__(self):
                self.workflow_finished = type("Sig", (), {"connect": lambda *a: None, "disconnect": lambda *a: None})()
                self.finished = type("Sig", (), {"connect": lambda *a: None, "disconnect": lambda *a: None})()
            def isRunning(self):
                return True

        panel0.worker = FakeRipWorker()
        panel0.transition_to(panel0.drive_state.RIPPING)
        orig_adapter = panel0.adapter

        # 1. Switch sr0 -> sr1 via adapter signal chain
        orig_adapter.on_active_device_changed("/dev/sr0", "/dev/sr1")
        QApplication.processEvents()

        assert win.tab_widget.currentWidget() is panel1
        assert panel1.adapter is orig_adapter
        assert panel0.worker is None

        # Verify adapter events now route to panel1, not panel0
        orig_adapter.on_log(LogLevel.INFO, "Ripping on sr1")
        QApplication.processEvents()
        assert "Ripping on sr1" in panel1.phase_detail_lbl.text()
        assert "Ripping on sr1" not in panel0.phase_detail_lbl.text()

        # 2. Switch back sr1 -> sr0 via the same adapter signal chain
        orig_adapter.on_active_device_changed("/dev/sr1", "/dev/sr0")
        QApplication.processEvents()

        assert win.tab_widget.currentWidget() is panel0
        assert panel0.adapter is orig_adapter
        assert panel1.worker is None
        assert panel0.worker is not None
    finally:
        panel0.worker = None
        panel1.worker = None
        win.close()
        win.deleteLater()
