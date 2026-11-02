"""Service encapsulating track sequence verification, release candidate validation, and tree comparison."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from whipit.ripping.reports import parse_cue_expected_tracks, parse_whipper_report
from whipit.validation.flac import verify_flac_integrity
from whipit.validation.integrity import compare_trees, verify_release_candidate
from whipit.validation.tracks import validate_track_sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)


class ValidationService:
    """Consolidated audio track validation, CUE/log report parsing, and stream decodability."""

    @staticmethod
    def validate_candidate(
        candidate_dir: Path,
        expected_count: int,
        total_tracks: Optional[int] = None,
        allow_partial: bool = False,
    ) -> ValidationResult:
        ok, errors = verify_release_candidate(
            candidate_dir=candidate_dir,
            expected_count=expected_count,
            total_tracks=total_tracks,
            allow_partial=allow_partial,
        )
        return ValidationResult(is_valid=ok, errors=errors)

    @staticmethod
    def validate_sequence(
        directory: Path,
        expected_count: int,
        total_tracks: Optional[int] = None,
        allow_partial: bool = False,
    ) -> Tuple[bool, List[str]]:
        return validate_track_sequence(
            directory=directory,
            expected_count=expected_count,
            total_tracks=total_tracks,
            allow_partial=allow_partial,
        )

    @staticmethod
    def verify_trees(source_dir: Path, target_dir: Path, ignored_files: Optional[List[str]] = None) -> bool:
        return compare_trees(source_dir, target_dir, ignored_files=ignored_files)

    @staticmethod
    def verify_flacs(flac_files: List[Path]) -> bool:
        return verify_flac_integrity(flac_files)

    @staticmethod
    def parse_report(report_path: Path) -> Tuple[str, str, int, int]:
        return parse_whipper_report(report_path)

    @staticmethod
    def parse_cue_tracks(cue_path: Path) -> int:
        return parse_cue_expected_tracks(cue_path)
