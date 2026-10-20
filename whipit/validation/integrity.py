"""Directory comparison and release candidate validation."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple
from whipit.validation.flac import verify_flac_integrity
from whipit.validation.tracks import validate_track_sequence


def _files_content_equal(path_a: Path, path_b: Path) -> bool:
    """Performs chunked binary content verification between two files."""
    try:
        if path_a.stat().st_size != path_b.stat().st_size:
            return False
        with open(path_a, "rb") as f1, open(path_b, "rb") as f2:
            while True:
                chunk1 = f1.read(65536)
                chunk2 = f2.read(65536)
                if chunk1 != chunk2:
                    return False
                if not chunk1:
                    return True
    except OSError:
        return False


def compare_trees(source_dir: Path, target_dir: Path, ignored_files: Optional[List[str]] = None) -> bool:
    """
    Verifies that target_dir is an exact byte-for-byte duplicate of source_dir,
    ignoring files named in ignored_files (e.g. transient logs).
    """
    if not source_dir.is_dir() or not target_dir.is_dir():
        return False

    ignored = set(ignored_files or [])

    source_files = {
        p.relative_to(source_dir): p
        for p in source_dir.rglob("*")
        if p.is_file() and p.name not in ignored
    }
    target_files = {
        p.relative_to(target_dir): p
        for p in target_dir.rglob("*")
        if p.is_file() and p.name not in ignored
    }

    if set(source_files.keys()) != set(target_files.keys()):
        return False

    for rel_path, src_file in source_files.items():
        tgt_file = target_files[rel_path]
        if not _files_content_equal(src_file, tgt_file):
            return False

    return True


def verify_release_candidate(
    candidate_dir: Path,
    expected_count: int,
    total_tracks: Optional[int] = None,
    allow_partial: bool = False,
) -> Tuple[bool, List[str]]:
    flac_files = sorted(list(candidate_dir.glob("*.flac")))
    errors = []

    if len(flac_files) != expected_count:
        errors.append(f"Candidate contains {len(flac_files)} tracks, expected {expected_count}.")

    seq_ok, seq_errors = validate_track_sequence(
        candidate_dir,
        expected_count=expected_count,
        total_tracks=total_tracks,
        allow_partial=allow_partial,
    )
    if not seq_ok:
        errors.extend(seq_errors)

    if not verify_flac_integrity(flac_files):
        errors.append("FLAC decodability stream check (flac -t -s) failed on one or more tracks.")

    return len(errors) == 0, errors
