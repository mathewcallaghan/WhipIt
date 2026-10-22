"""Log and metadata report parsers for Whipper and CUE sheets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple


def parse_cue_expected_tracks(cue_path: Path) -> int:
    if not cue_path.exists():
        return 0
    count = 0
    for line in cue_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^\s*TRACK\s+\d+\s+AUDIO", line, re.IGNORECASE):
            count += 1
    return count


def _last_match(pattern: str, content: str) -> Optional[str]:
    found = re.findall(pattern, content, re.MULTILINE)
    return found[-1].strip() if found else None


def parse_whipper_report(report_path: Path) -> Tuple[str, str, int, int]:
    """Extract (health_status, accuraterip_summary, ok_tracks, listed_tracks)."""
    if not report_path.exists():
        return "Unknown", "Unknown", 0, 0

    content = report_path.read_text(encoding="utf-8", errors="replace")

    health = _last_match(r"^\s*Health status:\s*(.+)$", content) or "Unknown"
    accuraterip = _last_match(r"^\s*AccurateRip summary:\s*(.+)$", content) or "Unknown"

    ok_tracks = len(re.findall(r"^\s*Status:\s*Copy OK", content, re.MULTILINE))
    listed_tracks = len(re.findall(r"^\s*Filename:", content, re.MULTILINE))

    return health, accuraterip, ok_tracks, listed_tracks


def find_primary_cue(stage_dir: Path) -> Optional[Path]:
    for path in stage_dir.rglob("*.cue"):
        return path
    return None


def find_whipper_log(stage_dir: Path) -> Optional[Path]:
    for path in stage_dir.rglob("*.log"):
        if path.name != "whipper_exec.log":
            return path
    return None
