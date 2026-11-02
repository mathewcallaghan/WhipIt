"""Linux optical drive hardware interrogation, kernel ioctl polling, and tray control."""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple

from whipit.naming import sanitise_string

if TYPE_CHECKING:
    from whipit.ui.base import UserInteractionPort

# Linux CD-ROM ioctls
CDROM_DRIVE_STATUS: int = 0x5326
CDROM_DISC_STATUS: int = 0x5327

# CDROM_DRIVE_STATUS (0x5326) return values
CDS_NO_INFO: int = 0
CDS_NO_DISC: int = 1
CDS_TRAY_OPEN: int = 2
CDS_DRIVE_NOT_READY: int = 3
CDS_DISC_OK: int = 4

# CDROM_DISC_STATUS (0x5327) return values
CDS_AUDIO: int = 100
CDS_DATA_1: int = 101
CDS_DATA_2: int = 102
CDS_XA_2_1: int = 103
CDS_XA_2_2: int = 104
CDS_MIXED: int = 105

_orig_subprocess_run = subprocess.run


def _cfg() -> Any:
    """Dynamically resolves whipit.config to honor test monkeypatching across the legacy interface."""
    return sys.modules.get("whipit.config", sys.modules[__name__])


def find_optical_drives() -> List[str]:
    """Returns all available optical drive device paths (e.g. ['/dev/sr0', '/dev/sr1'])."""
    drives = sorted([str(p) for p in Path("/dev").glob("sr[0-9]*")])
    if not drives and Path("/dev/cdrom").exists():
        drives = [str(Path("/dev/cdrom").resolve())]
    return drives


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


def get_drives_with_hardware_ids() -> List[Tuple[str, str]]:
    """Returns a list of (device_path, hardware_id) tuples for all detected optical drives."""
    cfg = _cfg()
    return [(d, cfg.get_drive_hardware_id(d)) for d in cfg.find_optical_drives()]


def get_drive_details(dev_path: str = "/dev/cdrom") -> Tuple[str, str, str]:
    """Returns (vendor, model, release) from sysfs or hardware ID."""
    try:
        resolved = Path(dev_path).resolve()
        block_name = resolved.name
        sys_device = Path(f"/sys/block/{block_name}/device")
        if sys_device.exists():
            vendor = (sys_device / "vendor").read_text(encoding="utf-8", errors="replace").strip() if (sys_device / "vendor").exists() else ""
            model = (sys_device / "model").read_text(encoding="utf-8", errors="replace").strip() if (sys_device / "model").exists() else ""
            rev = (sys_device / "rev").read_text(encoding="utf-8", errors="replace").strip() if (sys_device / "rev").exists() else ""
            if vendor and model:
                return vendor, model, rev or "1.00"
    except Exception:
        pass
    hw = _cfg().get_drive_hardware_id(dev_path)
    parts = hw.split("_", 1)
    vendor = parts[0] if len(parts) > 0 else "Unknown"
    model = parts[1] if len(parts) > 1 else "Drive"
    return vendor, model, "1.00"


def is_usb_drive(dev_path: str = "/dev/cdrom") -> bool:
    """Detects whether an optical drive is connected via USB using sysfs."""
    try:
        dev_name = Path(dev_path).resolve().name
        sys_path = Path(f"/sys/block/{dev_name}").resolve()
        return any("usb" in part.lower() for part in sys_path.parts)
    except Exception:
        return False


def disable_usb_autosuspend(dev_path: str = "/dev/cdrom") -> None:
    """Disables Linux USB autosuspend for the drive's USB device to prevent sleep during rips."""
    try:
        dev_name = Path(dev_path).resolve().name
        sys_path = Path(f"/sys/block/{dev_name}").resolve()
        curr = sys_path
        for _ in range(6):
            if not curr.exists() or curr == Path("/sys"):
                break
            pwr_ctrl = curr / "power" / "control"
            if pwr_ctrl.exists():
                try:
                    pwr_ctrl.write_text("on", encoding="utf-8")
                except OSError:
                    pass
            curr = curr.parent
    except Exception:
        pass


def reset_optical_drive(dev_path: str = "/dev/cdrom") -> bool:
    """Sends a hardware USB port reset to the optical drive controller to unstick deadlocked firmware."""
    try:
        resolved = Path(dev_path).resolve()
        block_name = resolved.name
        sys_device = Path(f"/sys/block/{block_name}/device")
        curr = sys_device
        for _ in range(6):
            if not curr.exists() or curr == Path("/sys"):
                break
            reset_file = curr / "reset"
            if reset_file.exists():
                try:
                    reset_file.write_text("1", encoding="utf-8")
                    time.sleep(1.0)
                    return True
                except OSError:
                    pass
            curr = curr.parent
    except Exception:
        pass
    return False


def unlock_drive(dev_path: str = "/dev/cdrom") -> None:
    """Unlocks motorized tray lock (CDROM_LOCKDOOR) and asks kernel to allow ejection."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        subprocess.run(["eject", "-i", "0", dev_path], capture_output=True, check=False, timeout=4.0)
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, 0x5329, 0)
        finally:
            os.close(fd)
    except Exception:
        pass


def _is_ripper_holding_device(
    pid: int,
    target_devs: Set[str],
    target_binaries: Set[str],
    proc_root: Path = Path("/proc"),
) -> bool:
    pid_dir = proc_root / str(pid)
    try:
        comm = (pid_dir / "comm").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return False

    is_direct_target = comm in target_binaries
    is_python_whipper = False
    if not is_direct_target and comm.startswith("python"):
        try:
            cmdline_raw = (pid_dir / "cmdline").read_bytes()
            if b"whipper" in cmdline_raw:
                is_python_whipper = True
        except OSError:
            return False

    if not (is_direct_target or is_python_whipper):
        return False

    fd_dir = pid_dir / "fd"
    if fd_dir.is_dir():
        try:
            for fd_link in fd_dir.iterdir():
                try:
                    target = str(fd_link.resolve())
                    if target in target_devs:
                        return True
                except (OSError, RuntimeError):
                    continue
        except OSError:
            pass

    try:
        cmdline = (pid_dir / "cmdline").read_bytes().split(b"\x00")
        for raw_arg in cmdline:
            arg = raw_arg.decode("utf-8", errors="ignore").strip()
            if not arg:
                continue
            val = arg.split("=", 1)[-1]
            if val in target_devs:
                return True
            if arg.startswith("-d") and arg[2:] in target_devs:
                return True
    except OSError:
        pass

    return False


def _proc_starttime(pid: int, proc_root: Path = Path("/proc")) -> Optional[str]:
    try:
        stat_file = proc_root / str(pid) / "stat"
        return stat_file.read_text(encoding="utf-8", errors="ignore").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def release_drive_locks(dev_path: str = "/dev/cdrom", proc_root: Path = Path("/proc")) -> None:
    """Safely terminates orphaned ripping backends holding this specific device."""
    try:
        resolved = str(Path(dev_path).resolve())
        target_devs = {dev_path, resolved}
        target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}

        pids_to_kill: List[Tuple[int, Optional[str]]] = []
        proc = proc_root
        if proc.exists():
            for p in proc.iterdir():
                if p.name.isdigit():
                    try:
                        pid = int(p.name)
                        if pid != os.getpid() and _is_ripper_holding_device(pid, target_devs, target_binaries, proc_root=proc):
                            pids_to_kill.append((pid, _proc_starttime(pid, proc_root=proc)))
                    except (OSError, ValueError):
                        continue

        for pid, _ in pids_to_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

        if pids_to_kill:
            time.sleep(0.15)
            for pid, st in pids_to_kill:
                if st is not None and _proc_starttime(pid, proc_root=proc) == st:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
    except Exception:
        pass

    _cfg().unlock_drive(dev_path)


def eject_drive(dev_path: str = "/dev/cdrom") -> None:
    """Safely unlocks and opens the optical drive tray, recovering automatically from EBUSY, locks, or USB bridges."""
    if os.environ.get("PYTEST_CURRENT_TEST") and subprocess.run is _orig_subprocess_run:
        return

    from whipit.inhibit import unmount_cdda_device

    cfg = _cfg()
    cfg.unlock_drive(dev_path)
    try:
        res = subprocess.run(["eject", dev_path], capture_output=True, text=True, check=False, timeout=4.0)
        if res.returncode == 0:
            return
    except (subprocess.TimeoutExpired, OSError):
        pass

    unmount_cdda_device(dev_path)
    cfg.release_drive_locks(dev_path)
    cfg.unlock_drive(dev_path)

    max_attempts = 1 if os.environ.get("PYTEST_CURRENT_TEST") else 6
    for attempt in range(max_attempts):
        if attempt > 0:
            time.sleep(0.5)
            unmount_cdda_device(dev_path)
            cfg.release_drive_locks(dev_path)
            cfg.unlock_drive(dev_path)

        try:
            res = subprocess.run(["eject", dev_path], capture_output=True, text=True, check=False, timeout=4.0)
            if res.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        try:
            res2 = subprocess.run(["eject", "-s", dev_path], capture_output=True, text=True, check=False, timeout=4.0)
            if res2.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        if shutil.which("udisksctl"):
            try:
                res_u = subprocess.run(["udisksctl", "eject", "-b", dev_path], capture_output=True, text=True, check=False, timeout=4.0)
                if res_u.returncode == 0:
                    return
            except (subprocess.TimeoutExpired, OSError):
                pass

        try:
            res_r = subprocess.run(["eject", "-r", dev_path], capture_output=True, text=True, check=False, timeout=4.0)
            if res_r.returncode == 0:
                return
        except (subprocess.TimeoutExpired, OSError):
            pass

        if not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    fcntl.ioctl(fd, 0x5329, 0)
                    fcntl.ioctl(fd, 0x5309, 0)
                    return
                finally:
                    os.close(fd)
            except OSError:
                pass

        drive_st, _ = cfg.get_disc_status(dev_path)
        if drive_st in (CDS_TRAY_OPEN, CDS_NO_DISC):
            return


def close_tray(dev_path: str = "/dev/cdrom") -> None:
    """Closes motorized drive tray if supported."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        subprocess.run(["eject", "-t", dev_path], capture_output=True, check=False, timeout=4.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def get_disc_status(dev_path: str) -> Tuple[int, int]:
    """Queries Linux kernel ioctls for drive status and disc type."""
    drive_st = CDS_NO_INFO
    disc_st = CDS_NO_INFO
    try:
        fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            drive_st = fcntl.ioctl(fd, CDROM_DRIVE_STATUS)
            if drive_st == CDS_DISC_OK:
                try:
                    disc_st = fcntl.ioctl(fd, CDROM_DISC_STATUS)
                except Exception:
                    disc_st = CDS_NO_INFO
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno == 123 or getattr(exc, "errno", 0) == getattr(errno, "ENOMEDIUM", 123) or "medium" in str(exc).lower():
            return CDS_NO_DISC, CDS_NO_INFO
        if exc.errno == errno.EBUSY:
            return CDS_NO_INFO, CDS_NO_INFO
    except Exception:
        pass
    return drive_st, disc_st


def drive_has_disc(
    dev_path: str,
    wait_ready: bool = False,
    timeout: float = 8.0,
    audio_only: bool = True,
) -> bool:
    """Checks if a disc is loaded and ready, optionally verifying audio track format."""
    cfg = _cfg()
    start = time.time()
    while True:
        drive_st, disc_st = cfg.get_disc_status(dev_path)

        if drive_st == CDS_DISC_OK:
            if not audio_only:
                return True
            if disc_st in (CDS_AUDIO, CDS_MIXED):
                return True
            if disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2):
                return False
            if disc_st == CDS_NO_INFO:
                return True

        if not wait_ready or (time.time() - start >= timeout):
            return False

        time.sleep(0.5)


def wait_for_disc_ready(
    dev_path: str,
    timeout: float = 15.0,
    ui: Optional[UserInteractionPort] = None,
) -> bool:
    """Waits for an optical drive to spin up and read the TOC, emitting progress via UI port."""
    if ui is None:
        from whipit.ui.base import NullInteractionAdapter
        ui = NullInteractionAdapter()

    cfg = _cfg()
    cfg.close_tray(dev_path)
    hw_name = cfg.get_drive_hardware_id(dev_path)
    ui.on_disc_spinup_poll(dev_path, hw_name, 0.0, None)

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            break
        if cfg.drive_has_disc(dev_path, wait_ready=False):
            ui.on_disc_spinup_poll(dev_path, hw_name, elapsed, True)
            return True
        ui.on_disc_spinup_poll(dev_path, hw_name, elapsed, None)
        time.sleep(0.6)

    ui.on_disc_spinup_poll(dev_path, hw_name, time.time() - start, False)
    return False


def get_drive_max_speed(dev_path: str = "/dev/cdrom", proc_info_path: Optional[Path] = None) -> Optional[int]:
    """Reads maximum hardware speed (e.g. 48 for 48x) for this drive from /proc/sys/dev/cdrom/info."""
    info_file = proc_info_path or Path("/proc/sys/dev/cdrom/info")
    if not info_file.exists():
        return None
    try:
        dev_name = Path(dev_path).resolve().name
        lines = info_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        drive_names: List[str] = []
        drive_speeds: List[int] = []
        for line in lines:
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            if key == "drive name":
                drive_names = val.strip().split()
            elif key == "drive speed":
                drive_speeds = [int(s) for s in val.strip().split() if s.isdigit()]
        if dev_name in drive_names:
            idx = drive_names.index(dev_name)
            if idx < len(drive_speeds):
                return drive_speeds[idx]
    except Exception:
        pass
    return None


def resolve_optical_drive(
    requested_device: Optional[str] = None,
    ui: Optional[UserInteractionPort] = None,
) -> Optional[str]:
    """Resolves and confirms an optical drive with an Audio CD present using the UI port."""
    from whipit.ui.base import DiscPromptAction, LogLevel, NullInteractionAdapter

    if ui is None:
        ui = NullInteractionAdapter()

    if not requested_device:
        requested_device = os.environ.get("WHIPIT_DRIVE")

    cfg = _cfg()
    drives = cfg.find_optical_drives()
    if not drives:
        ui.on_log(LogLevel.ERROR, "No optical drive detected on this system.")
        return None

    if requested_device:
        if requested_device not in drives and not Path(requested_device).exists():
            ui.on_log(LogLevel.ERROR, f"Optical drive '{requested_device}' does not exist.")
            return None

        hw_name = cfg.get_drive_hardware_id(requested_device)
        if cfg.drive_has_disc(requested_device, wait_ready=True):
            ui.on_log(LogLevel.INFO, f"Target optical drive: {requested_device} ({hw_name}) [Audio CD Loaded]")
            return requested_device

        if not ui.is_interactive():
            ui.on_log(LogLevel.ERROR, f"No Audio CD detected in {requested_device}.")
            return None

        while True:
            hw_name = cfg.get_drive_hardware_id(requested_device)
            res = ui.prompt_insert_disc(requested_device, hw_name)
            if res.action == DiscPromptAction.ABORT:
                return None
            if res.action == DiscPromptAction.EJECT:
                cfg.eject_drive(requested_device)
                if not ui.wait_for_user_reload(
                    requested_device,
                    f"Ejected {requested_device}. Insert Audio CD and press [Enter] when ready...\n",
                ):
                    return None

            if cfg.wait_for_disc_ready(requested_device, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {requested_device}.")
                return requested_device

    loaded = [d for d in drives if cfg.drive_has_disc(d)]

    if len(loaded) == 1:
        selected = loaded[0]
        hw_name = cfg.get_drive_hardware_id(selected)
        ui.on_log(LogLevel.INFO, f"Auto-detected Audio CD in {selected} ({hw_name}).")
        return selected

    if len(loaded) > 1:
        loaded_with_hw = [(d, cfg.get_drive_hardware_id(d)) for d in loaded]
        return ui.select_loaded_drive(loaded_with_hw)

    if not ui.is_interactive():
        ui.on_log(LogLevel.ERROR, "No Audio CD detected in any drive.")
        return None

    if len(drives) == 1:
        dev = drives[0]
        hw_name = cfg.get_drive_hardware_id(dev)
        ui.on_log(LogLevel.INFO, f"Optical drive: {dev} ({hw_name})")
        while True:
            res = ui.prompt_insert_disc(dev, hw_name)
            if res.action == DiscPromptAction.ABORT:
                return None
            if res.action == DiscPromptAction.EJECT:
                cfg.eject_drive(dev)
                if not ui.wait_for_user_reload(
                    dev,
                    f"Ejected {dev}. Insert Audio CD and press [Enter] when ready...\n",
                ):
                    return None

            if cfg.wait_for_disc_ready(dev, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {dev}.")
                return dev

    while True:
        drives_with_hw = cfg.get_drives_with_hardware_ids()
        res = ui.prompt_empty_multi_drives(drives_with_hw)

        if res.action == DiscPromptAction.ABORT:
            return None

        if res.action == DiscPromptAction.EJECT and res.selected_device:
            target = res.selected_device
            cfg.eject_drive(target)
            if not ui.wait_for_user_reload(
                target,
                f"\nEjected {target}. Insert Audio CD and press [Enter] (or 'q' to cancel)...\n",
            ):
                continue

            if cfg.wait_for_disc_ready(target, ui=ui):
                ui.on_log(LogLevel.OK, f"Audio CD ready in {target}.")
                return target

            while True:
                hw_name = cfg.get_drive_hardware_id(target)
                retry_res = ui.prompt_insert_disc(target, hw_name, allow_main_menu=True)
                if retry_res.action == DiscPromptAction.ABORT:
                    return None
                if retry_res.action == DiscPromptAction.MAIN_MENU:
                    break
                if retry_res.action == DiscPromptAction.EJECT:
                    cfg.eject_drive(target)
                    if not ui.wait_for_user_reload(
                        target,
                        f"Ejected {target}. Insert disc and press [Enter] when ready...\n",
                    ):
                        return None
                if cfg.wait_for_disc_ready(target, ui=ui):
                    ui.on_log(LogLevel.OK, f"Audio CD ready in {target}.")
                    return target
            continue

        ui.on_log(LogLevel.INFO, "Scanning drives for loaded media...")
        found_loaded: List[str] = []
        poll_start = time.time()
        while time.time() - poll_start < 4.0:
            found_loaded = [d for d in drives if cfg.drive_has_disc(d, wait_ready=False)]
            if found_loaded:
                break
            time.sleep(0.5)

        if len(found_loaded) == 1:
            selected = found_loaded[0]
            hw_name = cfg.get_drive_hardware_id(selected)
            ui.on_log(LogLevel.OK, f"Auto-detected Audio CD in {selected} ({hw_name}).")
            return selected
        if len(found_loaded) > 1:
            loaded_with_hw = [(d, cfg.get_drive_hardware_id(d)) for d in found_loaded]
            return ui.select_loaded_drive(loaded_with_hw)

        ui.on_log(LogLevel.WARN, "Still no disc detected. Please make sure the tray is closed.")


class DriveService:
    """Service encapsulating optical hardware discovery, tray management, and device locks."""

    @staticmethod
    def find_drives() -> List[str]:
        return _cfg().find_optical_drives()

    @staticmethod
    def get_hardware_id(dev_path: str = "/dev/cdrom") -> str:
        return _cfg().get_drive_hardware_id(dev_path)

    @staticmethod
    def get_drives_with_hw() -> List[Tuple[str, str]]:
        return _cfg().get_drives_with_hardware_ids()

    @staticmethod
    def is_usb(dev_path: str = "/dev/cdrom") -> bool:
        return _cfg().is_usb_drive(dev_path)

    @staticmethod
    def disable_autosuspend(dev_path: str = "/dev/cdrom") -> None:
        _cfg().disable_usb_autosuspend(dev_path)

    @staticmethod
    def reset(dev_path: str = "/dev/cdrom") -> bool:
        return _cfg().reset_optical_drive(dev_path)

    @staticmethod
    def unlock(dev_path: str = "/dev/cdrom") -> None:
        _cfg().unlock_drive(dev_path)

    @staticmethod
    def release_locks(dev_path: str = "/dev/cdrom", proc_root: Path = Path("/proc")) -> None:
        _cfg().release_drive_locks(dev_path, proc_root=proc_root)

    @staticmethod
    def eject(dev_path: str = "/dev/cdrom") -> None:
        _cfg().eject_drive(dev_path)

    @staticmethod
    def close(dev_path: str = "/dev/cdrom") -> None:
        _cfg().close_tray(dev_path)

    @staticmethod
    def status(dev_path: str) -> Tuple[int, int]:
        return _cfg().get_disc_status(dev_path)

    @staticmethod
    def has_disc(dev_path: str, wait_ready: bool = False, timeout: float = 8.0, audio_only: bool = True) -> bool:
        return _cfg().drive_has_disc(dev_path, wait_ready=wait_ready, timeout=timeout, audio_only=audio_only)

    @staticmethod
    def wait_ready(dev_path: str, timeout: float = 15.0, ui: Optional[UserInteractionPort] = None) -> bool:
        return _cfg().wait_for_disc_ready(dev_path, timeout=timeout, ui=ui)

    @staticmethod
    def max_speed(dev_path: str = "/dev/cdrom", proc_info_path: Optional[Path] = None) -> Optional[int]:
        return _cfg().get_drive_max_speed(dev_path, proc_info_path=proc_info_path)

    @staticmethod
    def resolve_drive(requested_device: Optional[str] = None, ui: Optional[UserInteractionPort] = None) -> Optional[str]:
        return _cfg().resolve_optical_drive(requested_device, ui=ui)
