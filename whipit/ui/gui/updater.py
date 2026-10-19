"""Background worker checking for updates via GitHub or local Git repository."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from whipit import __version__
from whipit.config import DEFAULT_GITHUB_REPO, get_config_dir
from whipit.launcher import install_launcher


def get_configured_repo() -> str:
    """Returns the GitHub repository (owner/repo) from config or defaults."""
    cfg = get_config_dir() / "config"
    if cfg.exists():
        for line in cfg.read_text(errors="ignore").splitlines():
            if line.startswith("github_repo="):
                val = line.split("=", 1)[1].strip()
                if "/" in val:
                    return val
    return DEFAULT_GITHUB_REPO


def parse_version_tuple(v: str) -> tuple[int, ...]:
    """Converts a version string like '2.3.4' into a comparable integer tuple (2, 3, 4)."""
    clean = re.sub(r"^[^\d]*", "", v.strip())
    parts = []
    for x in clean.split("."):
        digits = "".join(filter(str.isdigit, x))
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


class UpdateWorker(QThread):
    update_finished = pyqtSignal(bool, str, bool)  # (success, message, restart_needed)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        github_repo = get_configured_repo()

        # Check if running inside a developer Git clone
        config_file = get_config_dir() / "config"
        source_dir: Optional[Path] = None
        if config_file.exists():
            for line in config_file.read_text(errors="ignore").splitlines():
                if line.startswith("source_dir="):
                    cand = Path(line.split("=", 1)[1].strip()).expanduser().resolve()
                    if cand.is_dir() and (cand / ".git").is_dir():
                        source_dir = cand
                        break

        # Path A: Developer Git Clone
        if source_dir and shutil.which("git"):
            self._update_via_git(source_dir)
            return

        # Path B: Standard End-User (ZIP / Release / Direct GitHub Download)
        self._update_via_github_archive(github_repo)

    def _update_via_github_archive(self, repo: str) -> None:
        """Fetches the latest version directly from GitHub and upgrades via pip."""
        raw_url = f"https://raw.githubusercontent.com/{repo}/main/whipit/__init__.py"
        zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"

        # 1. Fetch remote version string
        try:
            import urllib.error
            req = urllib.request.Request(
                raw_url,
                headers={"User-Agent": f"WhipIt/{__version__} UpdateChecker"},
            )
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                remote_code = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                self.update_finished.emit(
                    False,
                    f"Repository '{repo}' not found on GitHub (HTTP 404).\n\n"
                    "Once you create and upload your repo to GitHub, configure it in:\n"
                    "  ~/.config/whipit/config (add line: github_repo=yourusername/reponame)\n"
                    "or change DEFAULT_GITHUB_REPO in whipit/config.py.",
                    False,
                )
                return
            self.update_finished.emit(
                False,
                f"GitHub returned HTTP Error {err.code}: {err.reason}",
                False,
            )
            return
        except Exception as exc:
            self.update_finished.emit(
                False,
                f"Could not connect to GitHub to check for updates:\n{exc}",
                False,
            )
            return

        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', remote_code)
        if not m:
            self.update_finished.emit(
                False,
                "Could not parse remote version from GitHub.",
                False,
            )
            return

        remote_ver = m.group(1).strip()
        local_v = parse_version_tuple(__version__)
        remote_v = parse_version_tuple(remote_ver)

        # 2. Check if already up to date
        if remote_v <= local_v:
            self.update_finished.emit(
                True,
                f"WhipIt is already up to date (version {__version__}).",
                False,
            )
            return

        # 3. Upgrade via pip directly into the venv from GitHub's ZIP
        try:
            pip_res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", zip_url],
                capture_output=True,
                text=True,
                check=False,
                timeout=90.0,
            )
            if pip_res.returncode != 0:
                self.update_finished.emit(
                    False,
                    f"Installation of the update failed:\n{pip_res.stderr.strip()}",
                    False,
                )
                return

            install_launcher()
            self.update_finished.emit(
                True,
                f"WhipIt updated successfully: v{__version__} → v{remote_ver}!",
                True,
            )
        except subprocess.TimeoutExpired:
            self.update_finished.emit(False, "Timed out downloading package update.", False)
        except Exception as exc:
            self.update_finished.emit(False, f"Installation error: {exc}", False)

    def _update_via_git(self, source_dir: Path) -> None:
        """Fast incremental update for developers running from a git clone."""
        try:
            subprocess.run(["git", "-C", str(source_dir), "fetch", "--quiet"], check=False, timeout=15.0)
            status_res = subprocess.run(["git", "-C", str(source_dir), "status", "-uno"], capture_output=True, text=True, check=False)
            
            if "Your branch is up to date" in status_res.stdout or "nothing to commit" in status_res.stdout and "behind" not in status_res.stdout:
                self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
                return

            pull_res = subprocess.run(["git", "-C", str(source_dir), "pull", "--ff-only"], capture_output=True, text=True, check=False)
            if pull_res.returncode != 0:
                self.update_finished.emit(False, f"Git pull failed:\n{pull_res.stderr.strip()}", False)
                return

            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", str(source_dir)], check=False)
            install_launcher()

            ver_chk = subprocess.run([sys.executable, "-c", "import whipit; print(whipit.__version__)"], capture_output=True, text=True, check=False)
            new_ver = ver_chk.stdout.strip() or __version__
            self.update_finished.emit(True, f"WhipIt updated successfully to version {new_ver}!", True)
        except Exception as exc:
            self.update_finished.emit(False, f"Git update error: {exc}", False)
