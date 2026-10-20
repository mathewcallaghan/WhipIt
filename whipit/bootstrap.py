"""First-run bootstrapping, dependency checks, and desktop launcher setup."""

from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, Optional
from whipit.config import get_config_dir, get_whipper_conf
from whipit.launcher import install_launcher
from whipit.ui.base import LogLevel, NullInteractionAdapter, UserInteractionPort

REQUIRED_TOOLS = [
    "whipper",
    "flac",
    "metaflac",
    "opusenc",
    "loudgain",
    "eject",
    "pkill",
    "udisksctl",
    "gio",
]

OPTIONAL_TOOLS = [
    "notify-send",
]

UNMANAGED_PACKAGES: Dict[str, Dict[str, str]] = {
    "pacman": {
        "loudgain": "Install from AUR (e.g. 'yay -S loudgain' or 'paru -S loudgain').",
    },
    "dnf": {
        "loudgain": "Install from COPR or build from source (https://github.com/vparal/loudgain).",
    },
    "zypper": {
        "loudgain": "Install from openSUSE multimedia/OBS repo or build from source.",
    },
}


def detect_package_manager() -> Optional[Dict[str, object]]:
    """Detect local package manager and return binary name, command template, and pkg map."""
    managers = [
        ("apt-get", "apt-get update && apt-get install -y {packages}"),
        ("dnf", "dnf install -y {packages}"),
        ("pacman", "pacman -S --noconfirm {packages}"),
        ("zypper", "zypper install -y {packages}"),
    ]
    for mgr, cmd_template in managers:
        if shutil.which(mgr):
            pkg_map = {
                "apt-get": {
                    "cd-paranoia": "libcdio-utils",
                    "opusenc": "opus-tools",
                    "metaflac": "flac",
                    "pkill": "procps",
                    "udisksctl": "udisks2",
                    "gio": "libglib2.0-bin",
                    "notify-send": "libnotify-bin",
                },
                "dnf": {
                    "cd-paranoia": "libcdio-paranoia",
                    "opusenc": "opus-tools",
                    "metaflac": "flac",
                    "pkill": "procps-ng",
                    "udisksctl": "udisks2",
                    "gio": "glib2",
                    "notify-send": "libnotify",
                },
                "pacman": {
                    "cd-paranoia": "libcdio-paranoia",
                    "opusenc": "opus-tools",
                    "metaflac": "flac",
                    "pkill": "procps-ng",
                    "udisksctl": "udisks2",
                    "gio": "glib2",
                    "notify-send": "libnotify",
                },
                "zypper": {
                    "cd-paranoia": "libcdio-paranoia",
                    "opusenc": "opus-tools",
                    "metaflac": "flac",
                    "pkill": "procps",
                    "udisksctl": "udisks2",
                    "gio": "glib2-tools",
                    "notify-send": "libnotify-tools",
                },
            }
            return {"manager": mgr, "template": cmd_template, "pkg_map": pkg_map.get(mgr, {})}
    return None


def get_missing_dependencies() -> List[str]:
    """Returns missing required dependencies."""
    missing = []
    for tool in REQUIRED_TOOLS:
        if not shutil.which(tool):
            missing.append(tool)

    if not (shutil.which("cd-paranoia") or shutil.which("cdparanoia")):
        missing.append("cd-paranoia")

    return missing


def get_missing_optional_dependencies() -> List[str]:
    """Returns missing optional dependencies."""
    return [tool for tool in OPTIONAL_TOOLS if not shutil.which(tool)]


def check_or_install_dependencies(
    interactive: bool = True,
    ui: Optional[UserInteractionPort] = None,
) -> bool:
    if ui is None:
        ui = NullInteractionAdapter()

    missing_required = get_missing_dependencies()
    missing_optional = get_missing_optional_dependencies()

    if not missing_required:
        if missing_optional:
            ui.on_log(
                LogLevel.INFO,
                f"Optional tool(s) not installed: {', '.join(missing_optional)} "
                f"(desktop notifications may be limited).",
            )
        return True

    ui.on_log(LogLevel.WARN, f"Missing required system dependencies: {', '.join(missing_required)}")

    pm_info = detect_package_manager()
    if not pm_info:
        ui.on_log(
            LogLevel.ERROR,
            f"Could not automatically determine system package manager. "
            f"Please install missing tools manually: {', '.join(missing_required)}",
        )
        return False

    pkg_map = pm_info["pkg_map"]
    assert isinstance(pkg_map, dict)
    template = str(pm_info["template"])
    manager = str(pm_info["manager"])

    unmanaged = UNMANAGED_PACKAGES.get(manager, {})
    all_needed = missing_required + missing_optional
    installable_needed = [pkg for pkg in all_needed if pkg not in unmanaged]
    unmanaged_needed = [pkg for pkg in all_needed if pkg in unmanaged]

    for pkg in unmanaged_needed:
        hint = unmanaged[pkg]
        ui.on_log(LogLevel.WARN, f"'{pkg}' is not in official {manager} repositories. {hint}")

    if not installable_needed:
        ui.on_log(
            LogLevel.INFO,
            f"No remaining packages can be installed automatically via {manager}. "
            f"Please install manually: {', '.join(unmanaged_needed)}",
        )
        return len(get_missing_dependencies()) == 0

    packages_to_install = list(dict.fromkeys([pkg_map.get(pkg, pkg) for pkg in installable_needed]))
    inner_cmd = template.format(packages=" ".join(packages_to_install))

    use_pkexec = bool(shutil.which("pkexec"))
    if use_pkexec:
        exec_args = ["pkexec", "sh", "-c", inner_cmd]
        display_cmd = f"pkexec sh -c '{inner_cmd}'"
    else:
        exec_args = ["sudo", "sh", "-c", inner_cmd]
        display_cmd = f"sudo {inner_cmd}"

    needed_required_installable = [p for p in installable_needed if p in missing_required]
    if ui.confirm_install_dependencies(needed_required_installable or installable_needed, display_cmd, manager):
        try:
            ui.on_log(LogLevel.INFO, f"Executing installer: {display_cmd}")
            proc = subprocess.Popen(
                exec_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if proc.stdout:
                for line in proc.stdout:
                    clean = line.strip()
                    if clean:
                        ui.on_log(LogLevel.INFO, clean)
            proc.wait()

            if proc.returncode == 0:
                ui.on_log(LogLevel.OK, "Repository packages installed successfully!")
                remaining_required = get_missing_dependencies()
                if not remaining_required:
                    return True
                ui.on_log(
                    LogLevel.WARN,
                    f"Additional manual installation still required for: {', '.join(remaining_required)}",
                )
                return False

            ui.on_log(
                LogLevel.ERROR,
                f"Package installation failed (exit code {proc.returncode}). "
                f"You can install manually with:\n  {display_cmd}",
            )
        except Exception as e:
            ui.on_log(LogLevel.ERROR, f"Error executing package manager: {e}")
    else:
        ui.on_log(LogLevel.INFO, f"To install available tools manually, run:\n  {display_cmd}")

    return len(get_missing_dependencies()) == 0


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

    install_desktop_launcher()

    whipper_conf = get_whipper_conf()
    has_whipper_config = whipper_conf.exists() and "read_offset" in whipper_conf.read_text(errors="ignore")

    if not has_whipper_config:
        ui.on_log(LogLevel.HEADER, "Initializing first-time drive setup...")

    config_file.write_text("initialized=true\n", encoding="utf-8")
