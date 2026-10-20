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
    "calibrate_drive",
    "ExtractionResult",
    "RippingEngine",
    "DiscInspectionResult",
    "inspect_disc",
    "find_primary_cue",
    "find_whipper_log",
    "parse_cue_expected_tracks",
    "parse_whipper_report",
    "WhipperResult",
    "WhipperRunner",
]
