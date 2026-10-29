"""System, hardware, calibration, and dependency diagnostics collector for WhipIt."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from whipit import __version__
from whipit.bootstrap import OPTIONAL_TOOLS, REQUIRED_TOOLS, detect_package_manager
from whipit.config import (
    find_optical_drives,
    get_cache_dir,
    get_config_dir,
    get_drive_hardware_id,
    get_drive_max_speed,
    get_log_dir,
    get_stored_drive_offset,
    get_whipper_conf,
    get_whipper_defeats_cache,
    get_whipper_drive_offset,
    is_usb_drive,
    load_preferences,
)
from whipit.net import fetch_bounded


def _can_read_device(dev: str) -> Tuple[bool, str]:
    try:
        with open(dev, "rb"):
            return True, "Readable"
    except PermissionError:
        return False, "Permission Denied (Check user groups)"
    except OSError as exc:
        # ENOMEDIUM is standard when the tray is empty; the device node is still accessible
        if exc.errno == 123 or "medium" in str(exc).lower():
            return True, "Accessible (No disc loaded)"
        return True, f"Accessible ({exc.strerror or exc})"
    except Exception as exc:
        return False, str(exc)


def _check_user_groups() -> Tuple[bool, str]:
    try:
        import grp
        user_gids = os.getgroups()
        group_names = [grp.getgrgid(g).gr_name for g in user_gids if g in [g_obj.gr_gid for g_obj in grp.getgrall()]]
        for target in ("cdrom", "optical", "disk"):
            if target in group_names:
                return True, f"Member of '{target}' group"
        return False, f"Not in 'cdrom' or 'optical' group (Active groups: {', '.join(group_names[:6])})"
    except Exception:
        return True, "Group check unavailable"


def _get_binary_version(cmd: str) -> Optional[str]:
    bin_path = shutil.which(cmd)
    if not bin_path:
        return None
    try:
        res = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=2.0)
        output = (res.stdout or res.stderr).splitlines()
        if output:
            first = output[0].strip()
            return first[:80]
    except Exception:
        pass
    return "Installed"


def _check_api_endpoint(url: str, suffixes: Sequence[str]) -> Tuple[bool, str]:
    try:
        headers = {"User-Agent": f"WhipIt/{__version__} DiagnosticCollector"}
        fetch_bounded(url, headers=headers, suffixes=suffixes, max_bytes=8192, timeout=4.0)
        return True, "Connected (HTTP 200)"
    except Exception as exc:
        err_msg = str(exc)
        if "404" in err_msg:
            return True, "Reachable (HTTP 404)"
        return False, f"Unavailable: {err_msg[:70]}"


def generate_diagnostic_report(selected_device: Optional[str] = None, check_network: bool = True) -> str:
    """Collects and formats an environmental and hardware diagnostic report."""
    lines: List[str] = []

    def section(title: str) -> None:
        lines.append(f"\n[{title}]")

    def item(key: str, val: str, status: str = "") -> None:
        tag = f" [{status}]" if status else ""
        lines.append(f"• {key}: {val}{tag}")

    lines.append("=" * 70)
    lines.append(f"WhipIt System & Hardware Diagnostic Report (v{__version__})")
    lines.append("=" * 70)

    # 1. System Environment
    section("System Environment")
    distro = "Linux"
    if Path("/etc/os-release").exists():
        for row in Path("/etc/os-release").read_text(errors="ignore").splitlines():
            if row.startswith("PRETTY_NAME="):
                distro = row.split("=", 1)[1].strip('"\'')
                break
    item("Operating System", f"{distro} (Kernel {platform.release()})")
    item("Python Version", f"{platform.python_version()} ({platform.python_implementation()})")
    item("Python Executable", sys.executable)
    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION") or "Unknown"
    item("Desktop Environment", desktop)

    # 2. Optical Drive & Hardware
    section("Optical Drive & Hardware")
    drives = find_optical_drives()
    if not drives:
        item("Detected Drives", "None", "FAIL")
        lines.append("  (No optical drive detected under /dev/sr* or /dev/cdrom)")
    else:
        target_dev = selected_device if selected_device in drives else drives[0]
        item("Detected Drive(s)", ", ".join(drives))
        item("Active Drive Node", target_dev)

        hw_id = get_drive_hardware_id(target_dev)
        item("Drive Hardware ID", hw_id)

        is_usb = is_usb_drive(target_dev)
        conn_str = "USB External (Cache defeat safely managed)" if is_usb else "SATA / Internal"
        item("Interface Connection", conn_str)

        max_spd = get_drive_max_speed(target_dev)
        spd_str = f"{max_spd}x" if max_spd else "Unknown (Standard CD-ROM)"
        item("Rated Hardware Speed", spd_str)

        can_read, read_msg = _can_read_device(target_dev)
        item("Device Node Access", read_msg, "OK" if can_read else "WARN")

        in_group, grp_msg = _check_user_groups()
        item("User Permissions", grp_msg, "OK" if in_group else "WARN")

    # 3. AccurateRip & Calibration
    section("AccurateRip & Calibration")
    if drives:
        active_dev = selected_device if selected_device in drives else drives[0]
        whipit_offset = get_stored_drive_offset(active_dev)
        whipper_offset = get_whipper_drive_offset(active_dev)

        if whipit_offset is not None:
            item("Stored Read Offset", f"{whipit_offset:+d}", "OK")
        else:
            item("Stored Read Offset", "Unconfigured (Run Drive Calibration Wizard)", "WARN")

        whipper_conf_file = get_whipper_conf()
        conf_status = f"Found ({whipper_conf_file})" if whipper_conf_file.exists() else "Not found yet"
        item("Whipper Config File", conf_status)

        defeats_cache = get_whipper_defeats_cache(active_dev)
        if defeats_cache is not None:
            cache_status = "True" if defeats_cache else "False (Recommended for USB)"
            item("Whipper defeats_cache", cache_status)
    else:
        item("Stored Read Offset", "No drive connected to check offset", "INFO")
        whipper_conf_file = get_whipper_conf()
        conf_status = f"Found ({whipper_conf_file})" if whipper_conf_file.exists() else "Not found yet"
        item("Whipper Config File", conf_status)

        offsets_file = get_config_dir() / "offsets"
        if offsets_file.exists() and offsets_file.stat().st_size > 0:
            saved_entries = [ln.strip() for ln in offsets_file.read_text(errors="ignore").splitlines() if "=" in ln]
            if saved_entries:
                item("Stored Offset Profiles", "; ".join(saved_entries))
        else:
            item("Stored Offset Profiles", "None saved yet")

    # 4. Audio Toolchain
    section("Audio Toolchain")
    pm_info = detect_package_manager()
    item("System Package Manager", str(pm_info.get("manager", "Unknown")) if pm_info else "Undetected")

    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            ver = _get_binary_version(tool) or "OK"
            item(tool, f"{path} ({ver})", "OK")
        else:
            item(tool, "Not Found", "FAIL")

    paranoia_bin = shutil.which("cd-paranoia") or shutil.which("cdparanoia")
    if paranoia_bin:
        item("cd-paranoia backend", paranoia_bin, "OK")
    else:
        item("cd-paranoia backend", "Missing cd-paranoia / cdparanoia", "FAIL")

    for opt_tool in OPTIONAL_TOOLS:
        opt_path = shutil.which(opt_tool)
        status = "OK" if opt_path else "OPTIONAL (Missing)"
        item(opt_tool, opt_path or "Not installed", status)

    # 5. Storage & Output Paths
    section("Storage & Output Paths")
    prefs = load_preferences()
    item("Generate Opus Mirror", str(prefs.generate_opus))
    item("Auto-Eject Drive", str(prefs.auto_eject))
    if prefs.default_speed:
        item("Configured Speed Limit", f"{prefs.default_speed}x")

    paths_to_verify = [("FLAC Archival Root", prefs.flac_root)]
    if prefs.generate_opus and prefs.opus_root:
        paths_to_verify.append(("Opus Mirror Root", prefs.opus_root))

    for label, root_dir in paths_to_verify:
        try:
            root_dir.mkdir(parents=True, exist_ok=True)
            usage = shutil.disk_usage(root_dir)
            free_gb = usage.free / (1024 ** 3)
            is_writable = os.access(root_dir, os.W_OK)
            stat_msg = f"{root_dir} [Writable: {'YES' if is_writable else 'NO'}, Free: {free_gb:.1f} GB]"
            item(label, stat_msg, "OK" if is_writable and free_gb >= 2.0 else "WARN")
        except Exception as exc:
            item(label, f"{root_dir} (Error: {exc})", "FAIL")

    # 6. Online Services
    section("Online Services")
    if check_network:
        try:
            mb_ok, mb_msg = _check_api_endpoint(
                "https://musicbrainz.org/ws/2/discid/2zK1z6n_U_u67vSgK887E0bZkE0-?fmt=json",
                ("musicbrainz.org",),
            )
            item("MusicBrainz Web API", mb_msg, "OK" if mb_ok else "WARN")
        except Exception as exc:
            item("MusicBrainz Web API", f"Check error: {exc}", "WARN")

        try:
            caa_ok, caa_msg = _check_api_endpoint(
                "https://coverartarchive.org/release/08d2710e-a2c1-40f3-a113-f577728b1f67",
                ("coverartarchive.org", "archive.org"),
            )
            item("Cover Art Archive", caa_msg, "OK" if caa_ok else "WARN")
        except Exception as exc:
            item("Cover Art Archive", f"Check error: {exc}", "WARN")
    else:
        item("Network Verification", "Skipped")

    lines.append("=" * 70)
    lines.append("End of Diagnostic Report\n")
    return "\n".join(lines)
