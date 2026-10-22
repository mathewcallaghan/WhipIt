"""Sequential track parity and inventory validation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def validate_track_sequence(
    directory: Path,
    expected_count: int,
    total_tracks: Optional[int] = None,
    allow_partial: bool = False,
) -> Tuple[bool, List[str]]:
    flacs = sorted(list(directory.glob("*.flac")))
    errors = []

    if len(flacs) != expected_count:
        errors.append(f"Track count mismatch in {directory.name}: found {len(flacs)}, expected exactly {expected_count}.")
        return False, errors

    max_track_bound = total_tracks or expected_count

    seen_tracks = {}
    for f in flacs:
        res = subprocess.run(
            ["metaflac", "--show-tag=TRACKNUMBER", str(f)],
            capture_output=True,
            text=True,
        )
        raw_val = ""
        for line in res.stdout.splitlines():
            if line.upper().startswith("TRACKNUMBER="):
                raw_val = line.split("=", 1)[1].strip()
                break

        track_str = raw_val.split("/")[0].strip()
        if not (track_str.isascii() and track_str.isdecimal() and 0 < len(track_str) <= 4):
            errors.append(f"Track {f.name} missing valid TRACKNUMBER tag: '{raw_val}'.")
            continue

        num = int(track_str)
        if num < 1 or num > max_track_bound:
            errors.append(f"Track number {num} in {f.name} out of bounds (expected 1..{max_track_bound}).")
            continue

        if num in seen_tracks:
            errors.append(f"Duplicate track number {num} detected: {f.name} and {seen_tracks[num]}.")
        seen_tracks[num] = f.name

    if not allow_partial:
        for i in range(1, max_track_bound + 1):
            if i not in seen_tracks:
                errors.append(f"Track sequence gap: Track {i} is missing.")

    return len(errors) == 0, errors
