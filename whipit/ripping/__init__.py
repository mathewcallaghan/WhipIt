"""Optical disc ripping, verification reporting, and calibration."""

from whipit.ripping.calibration import CalibrationResult, CalibrationStatus, calibrate_drive
from whipit.ripping.engine import ExtractionResult, RippingEngine
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.ripping.reports import (
    find_primary_cue,
    find_whipper_log,
    parse_cue_expected_tracks,
    parse_whipper_report,
)
from whipit.ripping.whipper import WhipperResult, WhipperRunner

__all__ = [
    "CalibrationResult",
    "CalibrationStatus",
    "DiscInspectionResult",
    "ExtractionResult",
    "RippingEngine",
    "WhipperResult",
    "WhipperRunner",
    "calibrate_drive",
    "find_primary_cue",
    "find_whipper_log",
    "inspect_disc",
    "parse_cue_expected_tracks",
    "parse_whipper_report",
]
