"""Background worker checking for updates via GitHub or local Git repository."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import zipfile
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from whipit import __version__
from whipit.config import DEFAULT_GITHUB_REPO, get_cache_dir, get_config_dir
from whipit.launcher import install_launcher
from whipit.net import fetch_bounded

_ALLOWED_REPOS = {"mathewcallaghan/WhipIt"}
_RELEASE_SIGNER_FPR = "4A26C4E137D7E99A8068A8D8B6F32185D9B05581"
_KEYRING = Path(__file__).resolve().parents[2] / "assets" / "release-keyring.gpg"
_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
_PINNED_URL = f"https://github.com/{DEFAULT_GITHUB_REPO}.git"

_REPO_RE = re.compile(r"^(?!\.)[A-Za-z0-9_.-]{1,100}/(?!\.)[A-Za-z0-9_.-]{1,100}$")
_VER_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")
_TAG_RE = _VER_RE
_WHEEL_RE = re.compile(r"^whipit-(\d+(?:\.\d+){1,3})-py3-none-any\.whl$")
_GITHUB_SUFFIXES = (
    "github.com",
    "githubusercontent.com",
)
_MAX_PKG_BYTES = 50 * 1024 * 1024


def _validate_repo(repo: Optional[str]) -> Optional[str]:
    if repo and _REPO_RE.fullmatch(repo.strip()) and repo.strip() in _ALLOWED_REPOS:
        return repo.strip()
    return None


def _recorded_source_dir() -> Optional[Path]:
    """Only returns the checkout directory recorded by install.sh if owned by current user."""
    cfg = get_config_dir() / "config"
    if cfg.is_file():
        for line in cfg.read_text(errors="ignore").splitlines():
            if line.startswith("source_dir="):
                d = Path(line.split("=", 1)[1].strip()).resolve()
                try:
                    if (d / ".git").is_dir() and d.stat().st_uid == os.getuid():
                        return d
                except OSError:
                    pass
    return None


def _verify_signature(pkg: Path, sig: Path) -> bool:
    gpgv_bin = shutil.which("gpgv") or shutil.which("gpgv2")
    if not gpgv_bin or not _KEYRING.exists():
        return False
    try:
        res = subprocess.run(
            [gpgv_bin, "--status-fd", "1", "--keyring", str(_KEYRING), str(sig), str(pkg)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
        return res.returncode == 0 and any(
            ln.startswith("[GNUPG:] VALIDSIG ") and _RELEASE_SIGNER_FPR in ln
            for ln in res.stdout.splitlines()
        )
    except subprocess.TimeoutExpired:
        return False


def _verify_tag(src: Path, tag: str) -> bool:
    """Verifies an annotated Git tag directly with the bundled keyring and pinned fingerprint."""
    try:
        raw = subprocess.run(
            _GIT + ["-C", str(src), "cat-file", "tag", tag],
            capture_output=True,
            check=False,
            timeout=15.0,
            env=_GIT_ENV,
        ).stdout
    except subprocess.TimeoutExpired:
        return False

    marker = b"-----BEGIN PGP SIGNATURE-----"
    payload, sep, sig = raw.partition(marker)
    if not sep:
        return False
    with tempfile.TemporaryDirectory(dir=get_cache_dir()) as td:
        p, s = Path(td) / "tag.txt", Path(td) / "tag.sig"
        p.write_bytes(payload)
        s.write_bytes(marker + sig)
        return _verify_signature(p, s)


def run_git(args: List[str], source_dir: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        _GIT + ["-C", str(source_dir), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_GIT_ENV,
    )


def get_configured_repo(source_dir: Optional[Path] = None) -> Optional[str]:
    """Resolves upstream GitHub repository, restricted strictly to authorized repositories."""
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

    search_dirs = [source_dir]
    for d in search_dirs:
        if d and (d / ".git").is_dir() and shutil.which("git"):
            try:
                res = subprocess.run(
                    _GIT + ["-C", str(d), "config", "--get", "remote.origin.url"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15.0,
                    env=_GIT_ENV,
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
    m = _VER_RE.fullmatch(v.strip())
    if not m:
        raise ValueError(f"Unsupported version string: {v!r}")
    return tuple(int(x) for x in m.groups() if x is not None)


class UpdateWorker(QThread):
    update_finished = pyqtSignal(bool, str, bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        source_dir = _recorded_source_dir()

        if source_dir and shutil.which("git"):
            self._update_via_git(source_dir)
            return

        repo = get_configured_repo(source_dir)
        if not repo:
            self.update_finished.emit(
                False,
                "No authorized GitHub repository configured for updates.\n\n"
                "Automatic updates are restricted to official releases from mathewcallaghan/WhipIt.",
                False,
            )
            return

        self._update_via_github_release(repo)

    def _update_via_git(self, source_dir: Path) -> None:
        """Verifies signed release tags and updates from a git clone."""
        try:
            try:
                run_git(
                    [
                        "-c",
                        "protocol.allow=never",
                        "-c",
                        "protocol.https.allow=always",
                        "fetch",
                        "--quiet",
                        _PINNED_URL,
                        "refs/tags/*:refs/tags/*",
                    ],
                    source_dir,
                    timeout=30.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Git timed out while fetching release tags. Please retry.",
                    False,
                )
                return

            try:
                tag_res = run_git(
                    ["tag", "--list", "v*", "[0-9]*", "--sort=-version:refname"],
                    source_dir,
                    timeout=15.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Git timed out while checking release tags. Please retry.",
                    False,
                )
                return

            tags = tag_res.stdout.split()
            if not tags:
                self.update_finished.emit(
                    False,
                    "Latest tag is missing or not signed by the release key; refusing to update.",
                    False,
                )
                return

            latest_tag = tags[0]
            clean_tag = latest_tag.lstrip("v")
            if not _TAG_RE.fullmatch(latest_tag):
                self.update_finished.emit(
                    False,
                    "Latest tag has an unexpected format.",
                    False,
                )
                return

            if parse_version_tuple(clean_tag) <= parse_version_tuple(__version__):
                self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
                return

            try:
                rev_res = run_git(
                    ["rev-parse", f"{latest_tag}^{{commit}}"],
                    source_dir,
                    timeout=15.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Git timed out while resolving release commit. Please retry.",
                    False,
                )
                return

            if rev_res.returncode != 0:
                self.update_finished.emit(False, "Could not resolve release tag commit.", False)
                return
            commit = rev_res.stdout.strip()

            try:
                merge_res = run_git(
                    ["merge", "--ff-only", commit],
                    source_dir,
                    timeout=30.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Git timed out while merging release commit. Please retry.",
                    False,
                )
                return

            if merge_res.returncode != 0:
                self.update_finished.emit(False, f"Git merge failed:\n{merge_res.stderr.strip()}", False)
                return

            req_lock = source_dir / "requirements.lock"
            if req_lock.is_file():
                try:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pip",
                            "install",
                            "--require-hashes",
                            "--no-input",
                            "--disable-pip-version-check",
                            "-r",
                            str(req_lock),
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=120.0,
                    )
                except subprocess.TimeoutExpired:
                    self.update_finished.emit(
                        False,
                        "Timed out installing locked dependencies. Please retry.",
                        False,
                    )
                    return

            try:
                pip_res = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "--no-input",
                        "--disable-pip-version-check",
                        "--no-deps",
                        "--no-build-isolation",
                        str(source_dir),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=90.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Timed out installing updated package. Please retry.",
                    False,
                )
                return

            if pip_res.returncode != 0:
                self.update_finished.emit(
                    False,
                    f"Package installation failed:\n{pip_res.stderr.strip()}",
                    False,
                )
                return

            try:
                install_launcher()
            except (ValueError, OSError):
                pass

            try:
                ver_chk = subprocess.run(
                    [sys.executable, "-c", "import whipit; print(whipit.__version__)"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15.0,
                )
            except subprocess.TimeoutExpired:
                self.update_finished.emit(
                    False,
                    "Timed out verifying updated package installation.",
                    False,
                )
                return

            if ver_chk.returncode != 0:
                self.update_finished.emit(
                    False,
                    "Updated package verification failed: could not import whipit.",
                    False,
                )
                return

            new_ver = ver_chk.stdout.strip() or clean_tag
            if parse_version_tuple(new_ver) < parse_version_tuple(clean_tag):
                self.update_finished.emit(
                    False,
                    f"Updated package version mismatch (expected {clean_tag}, got {new_ver}).",
                    False,
                )
                return

            self.update_finished.emit(True, f"WhipIt updated successfully to version {new_ver}!", True)
        except subprocess.TimeoutExpired as exc:
            self.update_finished.emit(False, f"Update timed out: {exc}", False)
        except Exception as exc:
            self.update_finished.emit(False, f"Git update error: {exc}", False)

    def _update_via_github_release(self, repo: str) -> None:
        """Fetches the latest signed wheel release from GitHub and upgrades via pip."""
        validated_repo = _validate_repo(repo)
        if not validated_repo:
            self.update_finished.emit(False, f"Invalid or unauthorized repository identifier: '{repo}'.", False)
            return

        api_url = f"https://api.github.com/repos/{validated_repo}/releases/latest"
        req_headers = {
            "User-Agent": f"WhipIt/{__version__} UpdateChecker",
            "Accept": "application/vnd.github.v3+json",
        }

        release_data = None
        try:
            raw_json = fetch_bounded(
                api_url,
                headers=req_headers,
                suffixes=_GITHUB_SUFFIXES,
                max_bytes=2 * 1024 * 1024,
                timeout=12.0,
            )
            release_data = json.loads(raw_json.decode("utf-8"))
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

        tag_name = str(release_data.get("tag_name", ""))
        if not _TAG_RE.fullmatch(tag_name):
            self.update_finished.emit(False, "Release tag has an unexpected format.", False)
            return

        remote_tag = tag_name.lstrip("v")
        remote_v = parse_version_tuple(remote_tag)
        local_v = parse_version_tuple(__version__)
        if remote_v <= local_v:
            self.update_finished.emit(True, f"WhipIt is already up to date (version {__version__}).", False)
            return

        asset = next((a for a in release_data.get("assets", []) if a.get("name", "").endswith(".whl")), None)
        sig = next((a for a in release_data.get("assets", []) if asset and a.get("name") == asset["name"] + ".sig"), None)
        if not asset or not sig:
            self.update_finished.emit(False, "Release has no signed wheel; refusing to update.", False)
            return

        wheel_url = asset.get("browser_download_url")
        sig_url = sig.get("browser_download_url")
        if not wheel_url or not sig_url:
            self.update_finished.emit(False, "Release asset download URL is missing.", False)
            return

        cache_updates = get_cache_dir() / "updates"
        cache_updates.mkdir(parents=True, exist_ok=True)
        safe_asset_name = Path(asset["name"]).name
        m = _WHEEL_RE.fullmatch(safe_asset_name)
        try:
            wheel_v = parse_version_tuple(m.group(1)) if m else None
        except ValueError:
            wheel_v = None
        if not m or wheel_v != remote_v:
            self.update_finished.emit(False, "Wheel version does not match release tag; refusing.", False)
            return

        tmpdir = Path(tempfile.mkdtemp(dir=cache_updates))
        temp_pkg = tmpdir / safe_asset_name
        temp_sig = tmpdir / f"{safe_asset_name}.sig"

        try:
            wheel_data = fetch_bounded(
                wheel_url,
                headers=req_headers,
                suffixes=_GITHUB_SUFFIXES,
                max_bytes=_MAX_PKG_BYTES,
                timeout=30.0,
            )
            sig_data = fetch_bounded(
                sig_url,
                headers=req_headers,
                suffixes=_GITHUB_SUFFIXES,
                max_bytes=64 * 1024,
                timeout=15.0,
            )

            temp_pkg.write_bytes(wheel_data)
            temp_sig.write_bytes(sig_data)

            if not _verify_signature(temp_pkg, temp_sig):
                self.update_finished.emit(False, "Signature verification failed; update aborted.", False)
                return

            try:
                with zipfile.ZipFile(temp_pkg) as z:
                    meta = next((n for n in z.namelist() if n.endswith(".dist-info/METADATA")), None)
                    ver = (
                        next(
                            (
                                ln[9:].strip()
                                for ln in z.read(meta).decode("utf-8", "replace").splitlines()
                                if ln.startswith("Version: ")
                            ),
                            None,
                        )
                        if meta
                        else None
                    )
            except Exception:
                ver = None

            try:
                meta_v = parse_version_tuple(ver) if ver else None
            except ValueError:
                meta_v = None

            if meta_v is None or meta_v != remote_v or remote_v <= local_v:
                self.update_finished.emit(False, "Signed wheel version mismatch; update aborted.", False)
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
                    "--only-binary=:all:",
                    "--no-deps",
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

            try:
                install_launcher()
            except (ValueError, OSError):
                pass

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
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)
