"""Directory comparison and release candidate validation."""

from __future__ import annotations

import filecmp
from pathlib import Path
from typing import List, Tuple
from whipit.validation.flac import verify_flac_integrity
from whipit.validation.tracks import validate_track_sequence


def compare_trees(source_dir: Path, target_dir: Path, ignored_files: List[str]) -> bool:
    dcmp = filecmp.dircmp(source_dir, target_dir, ignore=ignored_files)
    if dcmp.left_only or dcmp.right_only or dcmp.diff_files:
        return False
    for sub in dcmp.common_dirs:
        if not compare_trees(source_dir / sub, target_dir / sub, ignored_files):
            return False
    return True


def verify_release_candidate(candidate_dir: Path, expected_tracks: int) -> Tuple[bool, List[str]]:
    flac_files = sorted(list(candidate_dir.glob("*.flac")))
    errors = []

    if len(flac_files) != expected_tracks:
        errors.append(f"Candidate contains {len(flac_files)} tracks, expected {expected_tracks}.")

    seq_ok, seq_errors = validate_track_sequence(candidate_dir, expected_tracks)
    if not seq_ok:
        errors.extend(seq_errors)

    if not verify_flac_integrity(flac_files):
        errors.append("FLAC decodability stream check (flac -t -s) failed on one or more tracks.")

    return len(errors) == 0, errors
