"""FLAC bitstream validation, Vorbis comment reader, and decoded audio stream hashing."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional


def verify_flac_integrity(files: List[Path]) -> bool:
    if not files:
        return False
    cmd = ["flac", "-t", "-s"] + [str(f) for f in files]
    res = subprocess.run(cmd, capture_output=True)
    return res.returncode == 0


def flac_audio_sha256(path: Path) -> Optional[str]:
    """Calculates SHA-256 of decoded audio samples, remaining valid across metadata edits."""
    if not path.exists():
        return None
    h = hashlib.sha256()
    try:
        proc = subprocess.Popen(
            ["flac", "-d", "-c", "-s", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if proc.stdout is None:
            return None
        for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest() if proc.wait() == 0 else None
    except Exception:
        return None


def read_flac_tag(file_path: Path, tag_name: str) -> Optional[str]:
    cmd = [f"--show-tag={tag_name.upper()}"]
    res = subprocess.run(["metaflac"] + cmd + [str(file_path)], capture_output=True, text=True)
    for line in res.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip().upper() == tag_name.upper():
                return v.strip()
    return None
