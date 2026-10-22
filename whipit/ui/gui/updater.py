"""Background worker checking for updates via GitHub or local Git repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from whipit import __version__
from whipit.config import DEFAULT_GITHUB_REPO, get_cache_dir, get_config_dir
from whipit.launcher import install_launcher

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_TRUSTED_DL_HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
}
_MAX_PKG_BYTES = 50 * 1024 * 1024


def _trusted_https(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return parsed.scheme == "https" and (parsed.hostname or "").lower() in _TRUSTED_DL_HOSTS
    except Exception:
        return False


def _validate_repo(repo: Optional[str]) -> Optional[str]:
    if repo and _REPO_RE.fullmatch(repo.strip()):
        return repo.strip()
    return None


def get_configured_repo(source_dir: Optional[Path] = None) -> Optional[str]:
    """Resolves upstream GitHub repository (owner/repo) dynamically without hardcoded assumptions."""
    env_repo = os.environ.get("WHIPIT_GITHUB_REPO", "").strip()
    if env_repo:
        return _validate_repo(env_repo)

    cfg = get_config_dir() / "config"
    if cfg.exists():
        for line in cfg.read_text(errors="ignore").splitlines():
            if line.startswith("github_repo="):
                val = line.split("=", 1)[1].strip()
                if val:
                    return _validate_repo(val)

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
                        val = _validate_repo(m.group(1))
                        if val:
                            return val
            except Exception:
                pass

    if DEFAULT_GITHUB_REPO:
        val = _validate_repo(DEFAULT_GITHUB_REPO)
        if val:
            return val

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


def extract_expected_checksum(
    release_data: dict,
    target_filename: str,
    headers: dict,
    timeout: float = 12.0,
) -> Optional[str]:
    """
    Attempts to discover an expected SHA256 checksum for target_filename from
    release assets (e.g. SHA256SUMS, checksums.txt, <filename>.sha256) or the release body.
    """
    assets = release_data.get("assets", [])

    # 1. Look for asset named exactly target_filename + ".sha256"
    for asset in assets:
        if asset.get("name") in (f"{target_filename}.sha256", f"{target_filename}.sha256sum"):
            url = asset.get("browser_download_url")
            if url:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                        m = re.search(r"\b([a-fA-F0-9]{64})\b", text)
                        if m:
                            return m.group(1).lower()
                except Exception:
                    pass

    # 2. Look for checksum manifest files (SHA256SUMS, checksums.txt, etc.)
    for asset in assets:
        name = asset.get("name", "").lower()
        if name in ("sha256sums", "sha256sums.txt", "checksums.txt", "hashes.txt"):
            url = asset.get("browser_download_url")
            if url:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        text = resp.read().decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            if target_filename in line:
                                m = re.search(r"\b([a-fA-F0-9]{64})\b", line)
                                if m:
                                    return m.group(1).lower()
                except Exception:
                    pass

    # 3. Check release body for checksum matching target_filename
    body = release_data.get("body", "")
    if body:
        for line in body.splitlines():
            if target_filename in line:
                m = re.search(r"\b([a-fA-F0-9]{64})\b", line)
                if m:
                    return m.group(1).lower()
        m_body = re.search(r"(?:sha256|sha-256|hash)\s*[:=]\s*([a-fA-F0-9]{64})", body, re.I)
        if m_body:
            return m_body.group(1).lower()

    return None


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

            pip_res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--no-input",
                    "--disable-pip-version-check",
                    str(source_dir),
                ],
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
        """Fetches the latest tagged release from GitHub, verifies checksum, and upgrades via pip."""
        validated_repo = _validate_repo(repo)
        if not validated_repo:
            self.update_finished.emit(False, f"Invalid repository identifier: '{repo}'.", False)
            return

        api_url = f"https://api.github.com/repos/{validated_repo}/releases/latest"
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
            if err.code == 404:
                self.update_finished.emit(
                    False,
                    f"No published releases found for repository '{repo}'.\n\n"
                    "For security, WhipIt only installs pinned tagged releases.\n"
                    "To run development builds from git, clone the repository and use 'git pull'.",
                    False,
                )
                return
            self.update_finished.emit(False, f"GitHub API error {err.code}: {err.reason}", False)
            return
        except Exception as exc:
            self.update_finished.emit(False, f"Could not connect to GitHub to check for updates:\n{exc}", False)
            return

        if not release_data or "tag_name" not in release_data:
            self.update_finished.emit(
                False,
                f"No tagged releases available for repository '{repo}'.\n\n"
                "Unpinned branch builds are not installed automatically for security.",
                False,
            )
            return

        remote_tag = release_data["tag_name"].lstrip("v")
        remote_v = parse_version_tuple(remote_tag)
        local_v = parse_version_tuple(__version__)
        if remote_v <= local_v:
            self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
            return

        # Determine download URL: prefer published release asset, otherwise tagged tarball
        target_asset_name = f"whipit-{remote_tag}.tar.gz"
        download_url: Optional[str] = None
        for asset in release_data.get("assets", []):
            aname = asset.get("name", "")
            if aname.endswith((".tar.gz", ".whl", ".zip")):
                download_url = asset.get("browser_download_url")
                target_asset_name = aname
                break

        if not download_url:
            download_url = (
                release_data.get("tarball_url")
                or f"https://github.com/{validated_repo}/archive/refs/tags/{release_data['tag_name']}.tar.gz"
            )

        if not _trusted_https(download_url):
            self.update_finished.emit(
                False,
                f"Security Error: Download URL '{download_url}' is not on a trusted GitHub domain.",
                False,
            )
            return

        expected_hash = extract_expected_checksum(
            release_data,
            target_asset_name,
            headers=req_headers,
        )
        if not expected_hash:
            self.update_finished.emit(
                False,
                f"Security Error: Checksum verification failed: Release v{remote_tag} publishes no SHA-256 checksum.\n\n"
                "The update was aborted to protect your system.",
                False,
            )
            return

        cache_updates = get_cache_dir() / "updates"
        cache_updates.mkdir(parents=True, exist_ok=True)
        safe_asset_name = Path(target_asset_name).name
        temp_pkg = cache_updates / f"whipit_{remote_tag}_{os.getpid()}_{safe_asset_name}"

        try:
            download_req = urllib.request.Request(download_url, headers=req_headers)
            with urllib.request.urlopen(download_req, timeout=30.0) as resp:
                final_url = resp.geturl() if hasattr(resp, "geturl") else download_url
                if not _trusted_https(final_url):
                    self.update_finished.emit(
                        False,
                        f"Security Error: Download redirected to untrusted host '{final_url}'.",
                        False,
                    )
                    return
                try:
                    data = resp.read(_MAX_PKG_BYTES + 1)
                except TypeError:
                    data = resp.read()

            if len(data) > _MAX_PKG_BYTES:
                self.update_finished.emit(
                    False,
                    f"Security Error: Update package exceeds maximum allowed size ({_MAX_PKG_BYTES // (1024 * 1024)} MB).",
                    False,
                )
                return

            computed_hash = hashlib.sha256(data).hexdigest().lower()

            if computed_hash != expected_hash:
                self.update_finished.emit(
                    False,
                    f"Security Error: Checksum verification failed for release v{remote_tag}!\n\n"
                    f"Expected SHA256: {expected_hash}\n"
                    f"Computed SHA256: {computed_hash}\n\n"
                    "The update was aborted to protect your system.",
                    False,
                )
                return

            temp_pkg.write_bytes(data)

            pip_res = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    "--no-input",
                    "--disable-pip-version-check",
                    str(temp_pkg),
                ],
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
                f"WhipIt updated successfully: v{__version__} → v{remote_tag}!",
                True,
            )
        except subprocess.TimeoutExpired:
            self.update_finished.emit(False, "Timed out downloading package update.", False)
        except Exception as exc:
            self.update_finished.emit(False, f"Installation error: {exc}", False)
        finally:
            if temp_pkg.exists():
                temp_pkg.unlink(missing_ok=True)
