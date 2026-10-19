"""Sequential track parity and inventory validation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple


def validate_track_sequence(directory: Path, expected_count: int) -> Tuple[bool, List[str]]:
    flacs = sorted(list(directory.glob("*.flac")))
    errors = []

    if len(flacs) != expected_count:
        errors.append(f"Track count mismatch in {directory.name}: found {len(flacs)}, expected exactly {expected_count}.")
        return False, errors

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
        if not track_str.isdigit():
            errors.append(f"Track {f.name} missing valid TRACKNUMBER tag: '{raw_val}'.")
            continue

        num = int(track_str)
        if num < 1 or num > expected_count:
            errors.append(f"Track number {num} in {f.name} out of bounds (expected 1..{expected_count}).")
            continue

        if num in seen_tracks:
            errors.append(f"Duplicate track number {num} detected: {f.name} and {seen_tracks[num]}.")
        seen_tracks[num] = f.name

    for i in range(1, expected_count + 1):
        if i not in seen_tracks:
            errors.append(f"Track sequence gap: Track {i} is missing.")

    return len(errors) == 0, errors
