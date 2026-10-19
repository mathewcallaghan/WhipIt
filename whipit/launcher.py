"""Desktop entry launcher installation, verification, and uninstallation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple
from whipit.config import get_cache_dir, get_config_dir, get_log_dir


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
    which_bin = shutil.which("whipit")
    if which_bin:
        return str(Path(which_bin).resolve())
    venv_bin = Path(sys.prefix) / "bin" / "whipit"
    if venv_bin.exists():
        return str(venv_bin.resolve())
    local_bin = get_bin_link_path()
    if local_bin.exists():
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
    icon_file = install_icon()
    if exec_target.endswith(" -m whipit"):
        py_bin = exec_target.replace(" -m whipit", "")
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


def remove_bin_link() -> Tuple[bool, str]:
    removed = []
    legacy = Path.home() / ".local" / "bin" / "whipit-gui"
    for link in (get_bin_link_path(), legacy):
        if link.is_symlink() or link.exists():
            try:
                link.unlink()
                removed.append(str(link))
            except Exception as e:
                return False, f"Failed to remove {link}: {e}"
    return (True, f"Removed command links: {', '.join(removed)}") if removed else (True, "No command found in ~/.local/bin")


def schedule_self_deletion(target_dir: Path) -> None:
    pid = os.getpid()
    quoted = shlex.quote(str(target_dir))
    script = f"while kill -0 {pid} 2>/dev/null; do sleep 0.1; done; rm -rf {quoted}"
    try:
        subprocess.Popen(["sh", "-c", script], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)


def uninstall_application(purge: bool = False, interactive: bool = True) -> bool:
    print("\n\033[1m=== whipit Uninstallation ===\033[0m")
    bin_link = get_bin_link_path()
    app_dir = get_app_dir()
    desktop_file = get_desktop_file_path()
    icon_file = get_icon_path()
    if interactive and not purge and sys.stdin.isatty():
        print(f"\nThis will remove:\n  * Desktop Application Launcher ({desktop_file.name})\n  * Custom Application Icon      ({icon_file.name})\n  * Executable Command           ({bin_link})\n  * Application Files & Venv     ({app_dir})")
        try:
            confirm = input("\nProceed with uninstallation? [y/N]: ").strip().lower()
            if confirm != "y":
                print("Uninstallation cancelled.")
                return False
        except (EOFError, KeyboardInterrupt):
            print("\nUninstallation cancelled.")
            return False
    ok, msg = uninstall_launcher()
    print(f"\033[0;32m[OK]\033[0m {msg}" if ok else f"\033[0;36m[INFO]\033[0m {msg}")
    bin_ok, bin_msg = remove_bin_link()
    if bin_ok and "Removed" in bin_msg:
        print(f"\033[0;32m[OK]\033[0m {bin_msg}")
    config_dir, cache_dir, log_dir = get_config_dir(), get_cache_dir(), get_log_dir()
    should_purge = purge
    if not purge and interactive and sys.stdin.isatty():
        try:
            reply = input("\nDo you also want to purge configuration, cache, and logs? [y/N]: ").strip().lower()
            should_purge = (reply == "y")
        except (EOFError, KeyboardInterrupt):
            should_purge = False
    if should_purge:
        for d in (config_dir, cache_dir, log_dir):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                print(f"\033[0;32m[OK]\033[0m Purged: {d}")
    else:
        print("\033[0;36m[INFO]\033[0m Configuration and rip logs preserved.")
    if app_dir.exists():
        schedule_self_deletion(app_dir)
        print(f"\033[0;32m[OK]\033[0m Removed application directory: {app_dir}")
    is_pipx = "pipx" in sys.prefix or "pipx" in Path(sys.prefix).parts
    if is_pipx and shutil.which("pipx"):
        pid = os.getpid()
        try:
            subprocess.Popen(["sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 0.1; done; pipx uninstall whipit"], start_new_session=True)
            print("\033[0;32m[OK]\033[0m Scheduled pipx package removal.")
        except Exception:
            pass
    print("\n\033[0;32m[OK]\033[0m whipit has been successfully uninstalled!\n")
    return True
