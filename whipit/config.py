"""Runtime configuration, directory layout, and optical drive inspection."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from whipit.ui.base import UserInteractionPort

DEFAULT_GITHUB_REPO = "mathew/WhipIt"  # Change to your GitHub username/repo


def get_output_root() -> Path:
    env_dir = os.environ.get("WHIPIT_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / "WhipIt").resolve()


def get_log_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    d = base / "whipit"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def get_cache_dir() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    d = base / "whipit"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def get_config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    d = base / "whipit"
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def get_whipper_conf() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "whipper" / "whipper.conf"


def sanitise_string(val: str) -> str:
    s = val.replace("/", "-").replace("\\", "-")
    s = re.sub(r'[:*?"<>|]', "_", s)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^[\s.\-]+", "", s)
    s = re.sub(r"[\s.\-]+$", "", s)
    return s if s else "Unknown"


def find_optical_drives() -> List[str]:
    """Returns all available optical drive device paths (e.g. ['/dev/sr0', '/dev/sr1'])."""
    drives = sorted([str(p) for p in Path("/dev").glob("sr[0-9]*")])
    if not drives and Path("/dev/cdrom").exists():
        drives = [str(Path("/dev/cdrom").resolve())]
    return drives


def get_drives_with_hardware_ids() -> List[Tuple[str, str]]:
    """Returns a list of (device_path, hardware_id) tuples for all detected optical drives."""
    return [(d, get_drive_hardware_id(d)) for d in find_optical_drives()]


def unlock_drive(dev_path: str = "/dev/cdrom") -> None:
    """Unlocks motorized tray lock (CDROM_LOCKDOOR) and asks kernel to allow ejection."""
    subprocess.run(["eject", "-i", "0", dev_path], capture_output=True, check=False)
    try:
        fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            # 0x5329 = CDROM_LOCKDOOR; 0 = unlock
            fcntl.ioctl(fd, 0x5329, 0)
        finally:
            os.close(fd)
    except Exception:
        pass


def release_drive_locks(dev_path: str = "/dev/cdrom") -> None:
    """Forcefully terminates any orphaned reading processes and releases device locks."""
    block_name = Path(dev_path).name
    # Kill any orphaned cd-paranoia or cdrdao processes holding the block device
    subprocess.run(["fuser", "-k", "-TERM", dev_path], capture_output=True, check=False)
    subprocess.run(["pkill", "-9", "-f", f"cd-paranoia.*{block_name}"], capture_output=True, check=False)
    subprocess.run(["pkill", "-9", "-f", f"cdparanoia.*{block_name}"], capture_output=True, check=False)
    unlock_drive(dev_path)


def eject_drive(dev_path: str = "/dev/cdrom") -> None:
    """Safely unlocks and opens the optical drive tray, recovering automatically from EBUSY."""
    unlock_drive(dev_path)
    res = subprocess.run(["eject", dev_path], capture_output=True, text=True, check=False)
    if res.returncode != 0 and "busy" in res.stderr.lower():
        # Handle was held by an orphaned reader; force release and retry
        release_drive_locks(dev_path)
        subprocess.run(["eject", dev_path], capture_output=True, check=False)


def close_tray(dev_path: str = "/dev/cdrom") -> None:
    """Closes motorized drive tray if supported (harmless on manual laptop trays)."""
    subprocess.run(["eject", "-t", dev_path], capture_output=True, check=False)


def drive_has_disc(dev_path: str, wait_ready: bool = False, timeout: float = 8.0) -> bool:
    """Uses Linux kernel ioctl (CDROM_DRIVE_STATUS) to check if a disc is loaded and ready."""
    start = time.time()
    while True:
        try:
            fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                # 0x5326 = CDROM_DRIVE_STATUS; 4 = CDS_DISC_OK
                status = fcntl.ioctl(fd, 0x5326)
                if status == 4:
                    return True
            finally:
                os.close(fd)
        except Exception:
            pass

        if not wait_ready or (time.time() - start >= timeout):
            return False

        time.sleep(0.5)


def wait_for_disc_ready(
    dev_path: str,
    timeout: float = 12.0,
    ui: Optional[UserInteractionPort] = None,
) -> bool:
    """Waits for an optical drive to spin up and read the TOC, emitting progress via UI port."""
    if ui is None:
        from whipit.ui.base import NullInteractionAdapter
        ui = NullInteractionAdapter()

    close_tray(dev_path)
    hw_name = get_drive_hardware_id(dev_path)
    ui.on_disc_spinup_poll(dev_path, hw_name, 0.0, None)

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            break
        if drive_has_disc(dev_path, wait_ready=False):
            ui.on_disc_spinup_poll(dev_path, hw_name, elapsed, True)
            return True
        ui.on_disc_spinup_poll(dev_path, hw_name, elapsed, None)
        time.sleep(0.6)

    ui.on_disc_spinup_poll(dev_path, hw_name, time.time() - start, False)
    return False


def get_drive_hardware_id(dev_path: str = "/dev/cdrom") -> str:
    try:
        resolved = Path(dev_path).resolve()
        block_name = resolved.name
        sys_device = Path(f"/sys/block/{block_name}/device")
        if sys_device.exists():
            vendor = (sys_device / "vendor").read_text(encoding="utf-8", errors="replace").strip() if (sys_device / "vendor").exists() else ""
            model = (sys_device / "model").read_text(encoding="utf-8", errors="replace").strip() if (sys_device / "model").exists() else ""
            combined = f"{vendor}_{model}".strip("_")
            sanitised = sanitise_string(combined)
            if sanitised and sanitised != "Unknown":
                return sanitised
        return sanitise_string(block_name)
    except Exception:
        return sanitise_string(Path(dev_path).name)


def get_stored_drive_offset(dev_path: str = "/dev/cdrom") -> Optional[int]:
    hw_id = get_drive_hardware_id(dev_path)
    offsets_file = get_config_dir() / "offsets"
    if not offsets_file.exists():
        return None
    for line in offsets_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            if key.strip() == hw_id:
                try:
                    return int(val.strip())
                except ValueError:
                    return None
    return None


def store_drive_offset(dev_path: str, offset: int) -> None:
    hw_id = get_drive_hardware_id(dev_path)
    offsets_file = get_config_dir() / "offsets"
    lines = []
    if offsets_file.exists():
        for line in offsets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _ = line.split("=", 1)
                if k.strip() != hw_id:
                    lines.append(line)
    lines.append(f"{hw_id}={offset}")
    tmp_file = offsets_file.with_suffix(".tmp")
    tmp_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp_file, offsets_file)


def resolve_optical_drive(
    requested_device: Optional[str] = None,
    ui: Optional[UserInteractionPort] = None,
) -> Optional[str]:
    """Resolves and confirms an optical drive with an Audio CD present using the UI port."""
    from whipit.ui.base import DiscPromptAction, LogLevel
    from whipit.ui.base import DiscPromptAction, LogLevel, NullInteractionAdapter

    if ui is None:
        ui = NullInteractionAdapter()

    drives = find_optical_drives()
    if not drives:
        ui.on_log(LogLevel.ERROR, "No optical drive detected on this system.")
        return None

    if requested_device:
        if requested_device not in drives and not Path(requested_device).exists():
            ui.on_log(LogLevel.ERROR, f"Optical drive '{requested_device}' does not exist.")
            return None

        hw_name = get_drive_hardware_id(requested_device)
        if drive_has_disc(requested_device, wait_ready=True):
            ui.on_log(LogLevel.INFO, f"Target optical drive: {requested_device} ({hw_name}) [Audio CD Loaded]")
            return requested_device

        if not ui.is_interactive():
            ui.on_log(LogLevel.ERROR, f"No Audio CD detected in {requested_device}.")
            return None

        while True:
            hw_name = get_drive_hardware_id(requested_device)
            res = ui.prompt_insert_disc(requested_device, hw_name)
            if res.action == DiscPromptAction.ABORT:
                return None
            if res.action == DiscPromptAction.EJECT:
                eject_drive(requested_device)
                if not ui.wait_for_user_reload(
                    requested_device,
                    f"Ejected {requested_device}. Insert Audio CD and press [Enter] when ready...\n",
                ):
                    return None

            if wait_for_disc_ready(requested_device, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {requested_device}.")
                return requested_device

    loaded = [d for d in drives if drive_has_disc(d)]

    if len(loaded) == 1:
        selected = loaded[0]
        hw_name = get_drive_hardware_id(selected)
        ui.on_log(LogLevel.INFO, f"Auto-detected Audio CD in {selected} ({hw_name}).")
        return selected

    if len(loaded) > 1:
        loaded_with_hw = [(d, get_drive_hardware_id(d)) for d in loaded]
        return ui.select_loaded_drive(loaded_with_hw)

    if not ui.is_interactive():
        ui.on_log(LogLevel.ERROR, "No Audio CD detected in any drive.")
        return None

    if len(drives) == 1:
        dev = drives[0]
        hw_name = get_drive_hardware_id(dev)
        ui.on_log(LogLevel.INFO, f"Optical drive: {dev} ({hw_name})")
        while True:
            res = ui.prompt_insert_disc(dev, hw_name)
            if res.action == DiscPromptAction.ABORT:
                return None
            if res.action == DiscPromptAction.EJECT:
                eject_drive(dev)
                if not ui.wait_for_user_reload(
                    dev,
                    f"Ejected {dev}. Insert Audio CD and press [Enter] when ready...\n",
                ):
                    return None

            if wait_for_disc_ready(dev, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {dev}.")
                return dev

    while True:
        drives_with_hw = get_drives_with_hardware_ids()
        res = ui.prompt_empty_multi_drives(drives_with_hw)

        if res.action == DiscPromptAction.ABORT:
            return None

        if res.action == DiscPromptAction.EJECT and res.selected_device:
            target = res.selected_device
            eject_drive(target)
            if not ui.wait_for_user_reload(
                target,
                f"\nEjected {target}. Insert Audio CD and press [Enter] (or 'q' to cancel)...\n",
            ):
                continue

            if wait_for_disc_ready(target, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {target}.")
                return target

            while True:
                hw_name = get_drive_hardware_id(target)
                retry_res = ui.prompt_insert_disc(target, hw_name, allow_main_menu=True)
                if retry_res.action == DiscPromptAction.ABORT:
                    return None
                if retry_res.action == DiscPromptAction.MAIN_MENU:
                    break
                if retry_res.action == DiscPromptAction.EJECT:
                    eject_drive(target)
                    if not ui.wait_for_user_reload(
                        target,
                        f"Ejected {target}. Insert disc and press [Enter] when ready...\n",
                    ):
                        return None
                if wait_for_disc_ready(target, ui=ui):
                    ui.on_log(LogLevel.OK, f"Audio CD ready in {target}.")
                    return target
            continue

        ui.on_log(LogLevel.INFO, "Scanning drives for loaded media...")
        found_loaded: List[str] = []
        poll_start = time.time()
        while time.time() - poll_start < 4.0:
            found_loaded = [d for d in drives if drive_has_disc(d, wait_ready=False)]
            if found_loaded:
                break
            time.sleep(0.5)

        if len(found_loaded) == 1:
            selected = found_loaded[0]
            hw_name = get_drive_hardware_id(selected)
            ui.on_log(LogLevel.OK, f"Auto-detected Audio CD in {selected} ({hw_name}).")
            return selected
        if len(found_loaded) > 1:
            loaded_with_hw = [(d, get_drive_hardware_id(d)) for d in found_loaded]
            return ui.select_loaded_drive(loaded_with_hw)

        ui.on_log(LogLevel.WARN, "Still no disc detected. Please make sure the tray is closed.")
