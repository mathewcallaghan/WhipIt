"""Background worker checking for updates via GitHub or local Git repository."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from whipit import __version__
from whipit.config import DEFAULT_GITHUB_REPO, get_config_dir
from whipit.launcher import install_launcher


def get_configured_repo(source_dir: Optional[Path] = None) -> Optional[str]:
    """Resolves upstream GitHub repository (owner/repo) dynamically without hardcoded assumptions."""
    env_repo = os.environ.get("WHIPIT_GITHUB_REPO", "").strip()
    if env_repo and "/" in env_repo:
        return env_repo

    cfg = get_config_dir() / "config"
    if cfg.exists():
        for line in cfg.read_text(errors="ignore").splitlines():
            if line.startswith("github_repo="):
                val = line.split("=", 1)[1].strip()
                if "/" in val:
                    return val

    search_dirs = [source_dir, Path.cwd(), Path(__file__).resolve().parent.parent.parent]
    for d in search_dirs:
        if d and (d / ".git").is_dir() and shutil.which("git"):
            try:
                res = subprocess.run(
                    ["git", "-C", str(d), "config", "--get", "remote.origin.url"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", res.stdout.strip())
                    if m:
                        return m.group(1)
            except Exception:
                pass

    if DEFAULT_GITHUB_REPO and "/" in DEFAULT_GITHUB_REPO:
        return DEFAULT_GITHUB_REPO

    return None


def parse_version_tuple(v: str) -> tuple[int, ...]:
    """Converts a version string like '2.3.8' into a comparable integer tuple (2, 3, 8)."""
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
        config_file = get_config_dir() / "config"
        source_dir: Optional[Path] = None
        if config_file.exists():
            for line in config_file.read_text(errors="ignore").splitlines():
                if line.startswith("source_dir="):
                    cand = Path(line.split("=", 1)[1].strip()).expanduser().resolve()
                    if cand.is_dir() and (cand / ".git").is_dir():
                        source_dir = cand
                        break

        if source_dir and shutil.which("git"):
            self._update_via_git(source_dir)
            return

        repo = get_configured_repo(source_dir)
        if not repo:
            self.update_finished.emit(
                False,
                "No GitHub repository configured for updates.\n\n"
                "To enable automatic updates, configure your repository in:\n"
                "  ~/.config/whipit/config (add line: github_repo=owner/repo)\n"
                "or run WhipIt from a Git clone.",
                False,
            )
            return

        self._update_via_github_release(repo)

    def _update_via_git(self, source_dir: Path) -> None:
        """Fast incremental update for developers running from a git clone."""
        try:
            subprocess.run(["git", "-C", str(source_dir), "fetch", "--quiet"], check=False, timeout=15.0)
            status_res = subprocess.run(["git", "-C", str(source_dir), "status", "-uno"], capture_output=True, text=True, check=False)

            if "Your branch is up to date" in status_res.stdout or ("nothing to commit" in status_res.stdout and "behind" not in status_res.stdout):
                self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
                return

            pull_res = subprocess.run(["git", "-C", str(source_dir), "pull", "--ff-only"], capture_output=True, text=True, check=False)
            if pull_res.returncode != 0:
                self.update_finished.emit(False, f"Git pull failed:\n{pull_res.stderr.strip()}", False)
                return

            # Upgrade via pip with dependencies enabled
            pip_res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", str(source_dir)],
                capture_output=True,
                text=True,
                check=False,
                timeout=90.0,
            )
            if pip_res.returncode != 0:
                self.update_finished.emit(
                    False,
                    f"Package installation failed:\n{pip_res.stderr.strip()}",
                    False,
                )
                return

            install_launcher()

            ver_chk = subprocess.run([sys.executable, "-c", "import whipit; print(whipit.__version__)"], capture_output=True, text=True, check=False)
            new_ver = ver_chk.stdout.strip() or __version__
            self.update_finished.emit(True, f"WhipIt updated successfully to version {new_ver}!", True)
        except Exception as exc:
            self.update_finished.emit(False, f"Git update error: {exc}", False)

    def _update_via_github_release(self, repo: str) -> None:
        """Fetches the latest release from GitHub and upgrades via pip with dependencies."""
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        req_headers = {
            "User-Agent": f"WhipIt/{__version__} UpdateChecker",
            "Accept": "application/vnd.github.v3+json",
        }

        release_data = None
        try:
            req = urllib.request.Request(api_url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                if resp.status == 200:
                    release_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code != 404:
                self.update_finished.emit(False, f"GitHub API error {err.code}: {err.reason}", False)
                return
        except Exception as exc:
            self.update_finished.emit(False, f"Could not connect to GitHub to check for updates:\n{exc}", False)
            return

        if release_data and "tag_name" in release_data:
            remote_tag = release_data["tag_name"].lstrip("v")
            remote_v = parse_version_tuple(remote_tag)
            local_v = parse_version_tuple(__version__)
            if remote_v <= local_v:
                self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
                return
            download_url = release_data.get("zipball_url") or f"https://github.com/{repo}/archive/refs/tags/{release_data['tag_name']}.zip"
            new_version_str = remote_tag
        else:
            raw_url = f"https://raw.githubusercontent.com/{repo}/main/whipit/__init__.py"
            try:
                raw_req = urllib.request.Request(raw_url, headers={"User-Agent": f"WhipIt/{__version__}"})
                with urllib.request.urlopen(raw_req, timeout=12.0) as resp:
                    remote_code = resp.read().decode("utf-8", errors="replace")
                m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', remote_code)
                if not m:
                    self.update_finished.emit(False, "Could not parse version from remote repository.", False)
                    return
                remote_ver = m.group(1).strip()
                if parse_version_tuple(remote_ver) <= parse_version_tuple(__version__):
                    self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
                    return
                download_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"
                new_version_str = remote_ver
            except Exception as exc:
                self.update_finished.emit(False, f"Could not check remote version from GitHub:\n{exc}", False)
                return

        try:
            pip_res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", download_url],
                capture_output=True,
                text=True,
                check=False,
                timeout=120.0,
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
                f"WhipIt updated successfully: v{__version__} → v{new_version_str}!",
                True,
            )
        except subprocess.TimeoutExpired:
            self.update_finished.emit(False, "Timed out downloading package update.", False)
        except Exception as exc:
            self.update_finished.emit(False, f"Installation error: {exc}", False)
