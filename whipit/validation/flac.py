"""FLAC bitstream validation and Vorbis comment reader."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional


def verify_flac_integrity(files: List[Path]) -> bool:
    if not files:
        return False
    cmd = ["flac", "-t", "-s"] + [str(f) for f in files]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode == 0


def read_flac_tag(file_path: Path, tag_name: str) -> Optional[str]:
    cmd = [f"--show-tag={tag_name.upper()}"]
    res = subprocess.run(["metaflac"] + cmd + [str(file_path)], capture_output=True, text=True)
    for line in res.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip().upper() == tag_name.upper():
                return v.strip()
    return None
