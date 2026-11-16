"""Subprocess-isolated verification that core models and services import cleanly without PyQt6."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_core_package_zero_qt_leakage() -> None:
    """Asserts that core models and domain services load with zero Qt dependency leakage."""
    code = (
        "import sys\n"
        "sys.modules['PyQt6'] = None\n"
        "sys.modules['PyQt6.QtCore'] = None\n"
        "sys.modules['PyQt6.QtGui'] = None\n"
        "sys.modules['PyQt6.QtWidgets'] = None\n"
        "sys.modules['PyQt6.QtDBus'] = None\n"
        "import whipit.models\n"
        "import whipit.services\n"
        "from whipit.services.validation_service import ValidationService\n"
        "from whipit.services.extraction_service import ExtractionService\n"
        "from whipit.services.transcode_service import TranscodeService\n"
        "from whipit.services.recovery_service import RecoveryService\n"
        "from whipit.services.publication_service import PublicationService\n"
        "from whipit.services.storage_service import StorageService\n"
        "from whipit.services.stage_metadata_service import StageMetadataService\n"
        "from whipit.services.drive_service import DriveService\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
    }
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Core import leaked Qt dependency:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
