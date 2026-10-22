"""Desktop entry launcher installation, verification, and uninstallation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple


def get_asset_icon_path() -> Path:
    return Path(__file__).parent / "assets" / "whipit.svg"


def get_applications_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    apps_dir = base / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    return apps_dir.resolve()


def get_desktop_file_path() -> Path:
    return get_applications_dir() / "whipit.desktop"


def get_icon_path() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    icon_dir = base / "icons" / "hicolor" / "scalable" / "apps"
    icon_dir.mkdir(parents=True, exist_ok=True)
    return (icon_dir / "whipit.svg").resolve()


def get_bin_link_path() -> Path:
    return Path.home() / ".local" / "bin" / "whipit"


def get_app_dir() -> Path:
    prefix = Path(sys.prefix).resolve()
    if prefix.name == "venv" and prefix.parent.name == "whipit":
        return prefix.parent
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return (base / "whipit").resolve()


def resolve_executable_path() -> str:
    venv_bin = Path(sys.prefix) / "bin" / "whipit"
    if venv_bin.exists():
        return str(venv_bin.resolve())
    local_bin = get_bin_link_path()
    if local_bin.is_symlink() or local_bin.exists():
        return str(local_bin.resolve())
    return f"{sys.executable} -m whipit"


def refresh_desktop_database() -> None:
    apps_dir = get_applications_dir()
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps_dir)], capture_output=True, check=False)
    icon_base = get_icon_path().parent.parent.parent.parent
    if shutil.which("gtk-update-icon-cache") and (icon_base / "hicolor").exists():
        subprocess.run(["gtk-update-icon-cache", "-q", "-t", str(icon_base / "hicolor")], capture_output=True, check=False)


def install_icon() -> Path:
    icon_file = get_icon_path()
    asset = get_asset_icon_path()
    if asset.exists():
        shutil.copy2(asset, icon_file)
    icon_file.chmod(0o644)
    return icon_file


def install_launcher(custom_exec: Optional[str] = None) -> Tuple[bool, Path]:
    desktop_file = get_desktop_file_path()
    exec_target = custom_exec or resolve_executable_path()
    if re.search(r'["`$\\%\n]', exec_target):
        raise ValueError("Executable path contains characters unsafe for a .desktop Exec line")
    icon_file = install_icon()
    if exec_target.endswith(" -m whipit"):
        py_bin = exec_target[: -len(" -m whipit")]
        exec_line = f'"{py_bin}" -m whipit'
    else:
        exec_line = f'"{exec_target}"'

    content = f"""[Desktop Entry]\nVersion=1.0\nType=Application\nName=WhipIt\nComment=Archival AccurateRip FLAC master and Opus 192k mirror\nExec={exec_line}\nIcon={icon_file}\nTerminal=false\nCategories=AudioVideo;Audio;\n"""
    desktop_file.write_text(content, encoding="utf-8")
    desktop_file.chmod(0o644)
    refresh_desktop_database()
    return True, desktop_file


def uninstall_launcher() -> Tuple[bool, str]:
    desktop_file = get_desktop_file_path()
    icon_file = get_icon_path()
    removed = []
    if desktop_file.exists():
        try:
            desktop_file.unlink()
            removed.append("launcher")
        except Exception:
            pass
    if icon_file.exists():
        try:
            icon_file.unlink()
            removed.append("icon")
        except Exception:
            pass
    refresh_desktop_database()
    return (True, f"Successfully removed {', '.join(removed)}.") if removed else (False, "Launcher files not found.")
