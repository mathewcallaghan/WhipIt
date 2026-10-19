"""First-run bootstrapping, dependency checks, and desktop launcher setup."""

from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, Optional
from whipit.config import get_config_dir, get_whipper_conf
from whipit.launcher import install_launcher
from whipit.ui.base import LogLevel, UserInteractionPort
from whipit.ui.base import LogLevel, NullInteractionAdapter, UserInteractionPort

REQUIRED_TOOLS = [
    "whipper",
    "flac",
    "metaflac",
    "opusenc",
    "loudgain",
    "eject",
    "diff",
    "notify-send",
]


def detect_package_manager() -> Optional[Dict[str, object]]:
    """Detect local package manager and return binary name and install template."""
    managers = [
        ("apt-get", "sudo apt-get update && sudo apt-get install -y {packages}"),
        ("dnf", "sudo dnf install -y {packages}"),
        ("pacman", "sudo pacman -S --noconfirm {packages}"),
        ("zypper", "sudo zypper install -y {packages}"),
    ]
    for mgr, cmd_template in managers:
        if shutil.which(mgr):
            pkg_map = {
                "apt-get": {"opusenc": "opus-tools", "diff": "diffutils", "notify-send": "libnotify-bin"},
                "dnf": {"opusenc": "opus-tools", "metaflac": "flac", "notify-send": "libnotify"},
                "pacman": {"opusenc": "opus-tools", "metaflac": "flac", "notify-send": "libnotify"},
                "zypper": {"opusenc": "opus-tools", "metaflac": "flac", "notify-send": "libnotify-tools"},
            }
            return {"manager": mgr, "template": cmd_template, "pkg_map": pkg_map.get(mgr, {})}
    return None


def get_missing_dependencies() -> List[str]:
    missing = []
    for tool in REQUIRED_TOOLS:
        if not shutil.which(tool):
            missing.append(tool)

    if not (shutil.which("cd-paranoia") or shutil.which("cdparanoia")):
        missing.append("cd-paranoia")

    return missing


def check_or_install_dependencies(
    interactive: bool = True,
    ui: Optional[UserInteractionPort] = None,
) -> bool:
    if ui is None:
        ui = NullInteractionAdapter()

    missing = get_missing_dependencies()
    if not missing:
        return True

    ui.on_log(LogLevel.WARN, f"Missing required system dependencies: {', '.join(missing)}")

    pm_info = detect_package_manager()
    if not pm_info:
        ui.on_log(
            LogLevel.ERROR,
            f"Could not automatically determine your system package manager. "
            f"Please install the missing tools manually: {', '.join(missing)}",
        )
        return False

    pkg_map = pm_info["pkg_map"]
    assert isinstance(pkg_map, dict)
    template = str(pm_info["template"])
    manager = str(pm_info["manager"])

    packages_to_install = [pkg_map.get(pkg, pkg) for pkg in missing]
    install_cmd = template.format(packages=" ".join(packages_to_install))

    if ui.confirm_install_dependencies(missing, install_cmd, manager):
        try:
            ui.on_log(LogLevel.INFO, f"Executing: {install_cmd}")
            ret = subprocess.run(install_cmd, shell=True)
            if ret.returncode == 0:
                ui.on_log(LogLevel.OK, "Dependencies installed successfully!")
                return True
            ui.on_log(LogLevel.ERROR, f"Package installation failed with code {ret.returncode}.")
        except Exception as e:
            ui.on_log(LogLevel.ERROR, f"Error running package manager: {e}")
    else:
        ui.on_log(LogLevel.INFO, f"To install manually, run:\n  {install_cmd}")

    return False


def install_desktop_launcher() -> bool:
    """Delegates desktop launcher and SVG icon installation to launcher module."""
    ok, _ = install_launcher()
    return ok


def ensure_first_run_initialized(
    dev_path: str = "/dev/cdrom",
    ui: Optional[UserInteractionPort] = None,
) -> None:
    """Orchestrates first-run environment bootstrapping."""
    if ui is None:
        ui = NullInteractionAdapter()

    config_dir = get_config_dir()
    config_file = config_dir / "config"

    if config_file.exists():
        return

    # 1. Install user launcher and scalable SVG icon
    install_desktop_launcher()

    # 2. Check if Whipper is already configured
    whipper_conf = get_whipper_conf()
    has_whipper_config = whipper_conf.exists() and "read_offset" in whipper_conf.read_text(errors="ignore")

    if not has_whipper_config:
        ui.on_log(LogLevel.HEADER, "Initializing first-time drive setup...")

    # 3. Mark setup as completed
    config_file.write_text("initialized=true\n", encoding="utf-8")
