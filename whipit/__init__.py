"""whipit: AccurateRip + MusicBrainz Archival CD Ripper & Transcoder."""

import shutil
import subprocess
from pathlib import Path

__version__ = "2.6.3"

try:
    _root = Path(__file__).resolve().parent.parent
    if (_root / ".git").is_dir() and shutil.which("git"):
        _res = subprocess.run(
            ["git", "-C", str(_root), "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if _res.returncode == 0:
            _tag = _res.stdout.strip().lstrip("v")
            if _tag:
                __version__ = _tag
except Exception:
    pass
