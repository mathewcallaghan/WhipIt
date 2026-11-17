"""EBU R128 ReplayGain calculation using loudgain."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def apply_replaygain(directory: Path, extension: str) -> bool:
    ext = extension.lstrip(".")
    files = sorted(directory.rglob(f"*.{ext}"))
    if not files:
        return True

    cmd = ["loudgain", "-a", "-k", "-s", "e"] + [str(f) for f in files]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120.0, check=False)
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("ReplayGain calculation failed for %s: %s", directory, exc)
        return False
