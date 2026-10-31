import logging
import os
from pathlib import Path
import pytest
from whipit import __version__
from whipit.ripping.calibration import CalibrationResult, CalibrationStatus
from whipit.ui.gui.app import main


def test_cli_help_exits_cleanly(capsys):
    rc = main(["--help"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out
    assert "--device" in captured.out
    assert "--output-dir" in captured.out
    assert "--calibrate-drive" in captured.out
    assert "--version" in captured.out


def test_cli_version_exits_cleanly(capsys):
    rc = main(["-v"])
    assert rc == 0
    captured = capsys.readouterr()
    assert f"whipit {__version__}" in captured.out or f"whipit {__version__}" in captured.err

    rc2 = main(["--version"])
    assert rc2 == 0
    captured2 = capsys.readouterr()
    assert f"whipit {__version__}" in captured2.out or f"whipit {__version__}" in captured2.err


def test_cli_output_dir_sets_env(monkeypatch, tmp_path):
    target = tmp_path / "custom_cli_out"
    monkeypatch.delenv("WHIPIT_OUTPUT_DIR", raising=False)

    monkeypatch.setattr("whipit.ui.gui.app._run_cli_calibration", lambda device: 0)
    rc = main(["-o", str(target), "--calibrate-drive"])
    assert rc == 0
    assert os.environ.get("WHIPIT_OUTPUT_DIR") == str(target.resolve())


def test_cli_device_sets_env(monkeypatch):
    monkeypatch.delenv("WHIPIT_DRIVE", raising=False)

    called_dev = []
    monkeypatch.setattr("whipit.ui.gui.app._run_cli_calibration", lambda device: (called_dev.append(device), 0)[1])
    rc = main(["-d", "/dev/sr7", "--calibrate-drive"])
    assert rc == 0
    assert os.environ.get("WHIPIT_DRIVE") == "/dev/sr7"
    assert called_dev == ["/dev/sr7"]


def test_env_whipit_drive_resolves_when_unspecified(monkeypatch):
    from whipit.config import resolve_optical_drive

    monkeypatch.setattr("whipit.config.find_optical_drives", lambda: ["/dev/sr0", "/dev/sr1"])
    monkeypatch.setattr("whipit.config.drive_has_disc", lambda dev, **kw: True)
    monkeypatch.setattr("whipit.config.get_drive_hardware_id", lambda dev: "DRIVE_1")

    monkeypatch.setenv("WHIPIT_DRIVE", "/dev/sr1")
    resolved = resolve_optical_drive(requested_device=None)
    assert resolved == "/dev/sr1"


def test_env_log_level_configures_logging(monkeypatch):
    from whipit.ui.gui.app import _configure_logging

    monkeypatch.setenv("WHIPIT_LOG_LEVEL", "DEBUG")
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG

    monkeypatch.setenv("WHIPIT_LOG_LEVEL", "WARNING")
    _configure_logging()
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_cli_calibrate_drive_success_and_failure(monkeypatch, capsys):
    monkeypatch.setattr("whipit.config.resolve_optical_drive", lambda dev, ui: "/dev/sr0")

    monkeypatch.setattr(
        "whipit.ripping.calibration.calibrate_drive",
        lambda dev, ui: CalibrationResult(CalibrationStatus.SUCCESS, offset=6),
    )
    rc_ok = main(["--calibrate-drive"])
    assert rc_ok == 0
    out_ok = capsys.readouterr().out
    assert "Calibration successful" in out_ok
    assert "+6" in out_ok

    monkeypatch.setattr(
        "whipit.ripping.calibration.calibrate_drive",
        lambda dev, ui: CalibrationResult(CalibrationStatus.ABORTED),
    )
    rc_fail = main(["--calibrate-drive"])
    assert rc_fail == 1
