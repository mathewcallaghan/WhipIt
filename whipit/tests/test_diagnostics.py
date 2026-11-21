from whipit.diagnostics import generate_diagnostic_report


def test_diagnostic_report_structure(temp_env, monkeypatch):
    """Asserts that all essential report sections and headers are produced cleanly."""
    monkeypatch.setattr("whipit.diagnostics.find_optical_drives", lambda: ["/dev/sr0"])
    monkeypatch.setattr("whipit.diagnostics.get_drive_hardware_id", lambda dev: "TEST_DRIVE_MODEL")
    monkeypatch.setattr("whipit.diagnostics.is_usb_drive", lambda dev: True)
    monkeypatch.setattr("whipit.diagnostics.get_drive_max_speed", lambda dev: 48)
    monkeypatch.setattr("whipit.diagnostics.get_stored_drive_offset", lambda dev: 6)
    monkeypatch.setattr("whipit.diagnostics.get_whipper_defeats_cache", lambda dev: False)
    monkeypatch.setattr("whipit.diagnostics._can_read_device", lambda dev: (True, "Readable"))
    monkeypatch.setattr("whipit.diagnostics._check_user_groups", lambda: (True, "Member of 'cdrom' group"))

    report = generate_diagnostic_report(selected_device="/dev/sr0", check_network=False)

    assert "[System Environment]" in report
    assert "[Optical Drive & Hardware]" in report
    assert "[AccurateRip & Calibration]" in report
    assert "[Audio Toolchain]" in report
    assert "[Storage & Output Paths]" in report
    assert "[Online Services]" in report

    assert "/dev/sr0" in report
    assert "TEST_DRIVE_MODEL" in report
    assert "USB External" in report
    assert "48x" in report
    assert "+6" in report
    assert "cd-paranoia backend" in report


def test_diagnostic_report_handles_missing_drive(monkeypatch):
    """Ensures report generation doesn't crash on headless systems with no optical drive."""
    monkeypatch.setattr("whipit.diagnostics.find_optical_drives", list)

    report = generate_diagnostic_report(selected_device=None, check_network=False)
    assert "[FAIL]" in report
    assert "No optical drive detected" in report


def test_diagnostic_report_offline_resilience(monkeypatch):
    """Ensures network timeouts are caught cleanly without raising exceptions."""
    def failing_fetch(*args, **kwargs):
        raise OSError("Simulated connection timeout")

    monkeypatch.setattr("whipit.diagnostics.fetch_bounded", failing_fetch)
    report = generate_diagnostic_report(selected_device=None, check_network=True)
    assert "Simulated connection timeout" in report


def test_knoppix_diagnostic_html_formatter_and_dialog(monkeypatch):
    import pytest

    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.dialogs import DiagnosticDialog, _format_knoppix_diagnostic_html

    sample = (
        "======================================================================\n"
        "WhipIt System & Hardware Diagnostic Report (v2.6.7)\n"
        "[System Environment]\n"
        "• Operating System: Linux <Test> [OK]\n"
        "[Optical Drive & Hardware]\n"
        "• Detected Drives: None [FAIL]\n"
        "• Stored Read Offset: Unconfigured [WARN]\n"
        "• Note: Info line [INFO]\n"
    )
    rendered, ok_cnt, warn_cnt, fail_cnt = _format_knoppix_diagnostic_html(sample)
    assert ok_cnt == 1
    assert warn_cnt == 1
    assert fail_cnt == 1
    assert "&gt;&gt;&gt; [System Environment]" in rendered
    assert "&lt;Test&gt;" in rendered
    assert "[  OK  ]" in rendered
    assert "[ WARN ]" in rendered
    assert "[ FAIL ]" in rendered
    assert "[ INFO ]" in rendered

    _ = QApplication.instance() or QApplication([])
    monkeypatch.setattr("whipit.ui.gui.dialogs._DiagWorker.start", lambda self: None)
    dlg = DiagnosticDialog(parent=None, selected_device="/dev/sr0")
    try:
        assert dlg.text_box.objectName() == "diagnosticConsole"
        assert dlg.hud_lbl.objectName() == "diagnosticSummaryHud"
        dlg._on_report_ready(sample)
        assert dlg.hud_lbl.text() == "Checks: 1 Passed  |  1 Warnings  |  1 Failed"
        assert ">>> [System Environment]" in dlg.text_box.toPlainText()
        dlg._copy_to_clipboard()
        cb = QApplication.clipboard()
        if cb:
            assert cb.text() == sample
        assert "copied to clipboard" in dlg.hud_lbl.text()
    finally:
        dlg.deleteLater()
