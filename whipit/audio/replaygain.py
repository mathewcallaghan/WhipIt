"""EBU R128 ReplayGain calculation using loudgain."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List


def apply_replaygain(directory: Path, extension: str) -> bool:
    files = sorted(list(directory.rglob(f"*.{extension}")))
    if not files:
        return True

    cmd = ["loudgain", "-a", "-k", "-s", "e"] + [str(f) for f in files]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.returncode == 0
