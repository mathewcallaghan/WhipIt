"""FLAC bitstream validation, Vorbis comment reader, tag writer, and decoded audio stream hashing."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from whipit.naming import (
    format_album_stem,
    format_track_filename,
    sanitize_filename,
    sanitise_string,
    truncate_to_bytes,
)

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


def get_flac_duration_seconds(path: Path) -> Optional[float]:
    """Calculates duration in seconds using metaflac total samples and sample rate."""
    if not path.exists():
        return None
    try:
        res = subprocess.run(
            ["metaflac", "--show-total-samples", "--show-sample-rate", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip().isdigit()]
        if len(lines) >= 2:
            total_samples = int(lines[0])
            sample_rate = int(lines[1])
            if sample_rate > 0:
                return total_samples / sample_rate
    except Exception:
        pass
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


def write_flac_tags(file_path: Path, tags_dict: Dict[str, str]) -> bool:
    """Updates or sets Vorbis comments on a FLAC file via metaflac."""
    if not file_path.exists() or not tags_dict:
        return file_path.exists()

    cmd = ["metaflac"]
    for k, v in tags_dict.items():
        if v is not None and str(v).strip() != "":
            clean_val = str(v).replace("\r", "").replace("\n", " ").strip()
            clean_key = k.upper().strip()
            cmd.extend([f"--remove-tag={clean_key}", f"--set-tag={clean_key}={clean_val}"])

    cmd.append(str(file_path))
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return res.returncode == 0
