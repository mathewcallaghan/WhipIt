import pytest
from whipit.inhibit import suppress_media_dialogs, _inhibitor_lock


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
