"""Runtime configuration, directory layout, and optical drive inspection."""

from __future__ import annotations

import configparser
import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from whipit.naming import (
    format_album_stem,
    format_track_filename,
    sanitize_filename,
    sanitise_string,
    truncate_to_bytes,
)

if TYPE_CHECKING:
    from whipit.ui.base import UserInteractionPort

logger = logging.getLogger(__name__)

# Linux CD-ROM ioctls
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327

# CDROM_DRIVE_STATUS (0x5326) return values
CDS_NO_INFO = 0
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# CDROM_DISC_STATUS (0x5327) return values
CDS_AUDIO = 100
CDS_DATA_1 = 101
CDS_DATA_2 = 102
CDS_XA_2_1 = 103
CDS_XA_2_2 = 104
CDS_MIXED = 105

DEFAULT_GITHUB_REPO = "mathewcallaghan/WhipIt"


def default_flac_root() -> Path:
    env_dir = os.environ.get("WHIPIT_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    music_dir = os.environ.get("XDG_MUSIC_DIR")
    base = Path(music_dir).expanduser() if music_dir else (Path.home() / "Music")
    return (base / "FLAC" / "CD").resolve()


def default_opus_root() -> Path:
    env_dir = os.environ.get("WHIPIT_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    music_dir = os.environ.get("XDG_MUSIC_DIR")
    base = Path(music_dir).expanduser() if music_dir else (Path.home() / "Music")
    return (base / "Opus" / "CD").resolve()


class UserPreferences(BaseModel):
    flac_root: Path = Field(default_factory=default_flac_root)
    opus_root: Path = Field(default_factory=default_opus_root)
    generate_opus: bool = True
    default_speed: Optional[int] = None
    offset_override: Optional[int] = None
    auto_eject: bool = True

    @field_validator("flac_root", "opus_root", mode="after")
    @classmethod
    def _validate_root_path(cls, v: Path) -> Path:
        if any(part == ".." for part in v.parts):
            raise ValueError(f"Path traversal detected in configured root: {v}")
        return v.expanduser().resolve()

    @field_validator("default_speed")
    @classmethod
    def _validate_speed(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 1 or v > 100):
            raise ValueError(f"default_speed must be between 1 and 100, got {v}")
        return v

    @field_validator("offset_override")
    @classmethod
    def _validate_offset(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not -10000 <= v <= 10000:
            raise ValueError(f"offset_override must be between -10000 and 10000, got {v}")
        return v


def get_settings_file() -> Path:
    return get_config_dir() / "settings.json"


def load_preferences() -> UserPreferences:
    """Loads user preferences from settings.json, returning default preferences on missing or corrupt files."""
    sf = get_settings_file()
    if not sf.exists():
        return UserPreferences()

    try:
        content = sf.read_text(encoding="utf-8")
        return UserPreferences.model_validate_json(content)
    except Exception as exc:
        logger.warning("Corrupt or invalid preferences in %s (%s); falling back to defaults", sf, exc)
        try:
            bad_backup = sf.with_suffix(".json.bad")
            shutil.copy2(sf, bad_backup)
        except Exception:
            pass
        return UserPreferences()


def save_preferences(prefs: UserPreferences) -> bool:
    """Durably and atomically writes user preferences to settings.json with 0o600 permissions."""
    sf = get_settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(f".tmp.{os.getpid()}")
    try:
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prefs.model_dump_json(indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sf)
        try:
            dir_fd = os.open(sf.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        return True
    except OSError as exc:
        logger.warning("Failed saving preferences to %s: %s", sf, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def get_output_root() -> Path:
    env_dir = os.environ.get("WHIPIT_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return (Path.home() / "WhipIt").resolve()


def _private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d.resolve()


def _test_sandbox_path(sub: str) -> Path:
    import tempfile
    sb = Path(tempfile.gettempdir()) / "whipit_test_sandbox" / sub
    sb.mkdir(parents=True, exist_ok=True)
    return sb


def get_log_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    if not state_home and os.environ.get("PYTEST_CURRENT_TEST"):
        base = _test_sandbox_path("state")
    else:
        base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return _private_dir(base / "whipit")


def get_cache_dir() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if not cache_home and os.environ.get("PYTEST_CURRENT_TEST"):
        base = _test_sandbox_path("cache")
    else:
        base = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return _private_dir(base / "whipit")


def get_config_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if not config_home and os.environ.get("PYTEST_CURRENT_TEST"):
        base = _test_sandbox_path("config")
    else:
        base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return _private_dir(base / "whipit")


def get_whipper_conf() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if not config_home and os.environ.get("PYTEST_CURRENT_TEST"):
        base = _test_sandbox_path("config")
    else:
        base = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return base / "whipper" / "whipper.conf"


def find_optical_drives() -> List[str]:
    """Returns all available optical drive device paths (e.g. ['/dev/sr0', '/dev/sr1'])."""
    drives = sorted([str(p) for p in Path("/dev").glob("sr[0-9]*")])
    if not drives and Path("/dev/cdrom").exists():
        drives = [str(Path("/dev/cdrom").resolve())]
    return drives


def get_drives_with_hardware_ids() -> List[Tuple[str, str]]:
    """Returns a list of (device_path, hardware_id) tuples for all detected optical drives."""
    return [(d, get_drive_hardware_id(d)) for d in find_optical_drives()]


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
            # 0x5329 = CDROM_LOCKDOOR; 0 = unlock
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
    """Verifies if a specific PID is an orphaned ripping backend targeting target_devs."""
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
    """
    Safely terminates orphaned ripping backends holding this specific device.
    Uses exact device matching (sr1 will not match sr10) and leaves media players intact.
    """
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

    unlock_drive(dev_path)


_orig_subprocess_run = subprocess.run


def eject_drive(dev_path: str = "/dev/cdrom") -> None:
    """Safely unlocks and opens the optical drive tray, recovering automatically from EBUSY or locks."""
    if os.environ.get("PYTEST_CURRENT_TEST") and subprocess.run is _orig_subprocess_run:
        return
    unlock_drive(dev_path)
    try:
        res = subprocess.run(["eject", dev_path], capture_output=True, text=True, check=False, timeout=5.0)
        if res.returncode != 0:
            from whipit.inhibit import unmount_cdda_device
            unmount_cdda_device(dev_path)
            release_drive_locks(dev_path)
            res2 = subprocess.run(["eject", dev_path], capture_output=True, text=True, check=False, timeout=5.0)
            if res2.returncode != 0:
                subprocess.run(["eject", "-r", dev_path], capture_output=True, text=True, check=False, timeout=5.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def close_tray(dev_path: str = "/dev/cdrom") -> None:
    """Closes motorized drive tray if supported (harmless on manual laptop trays)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        subprocess.run(["eject", "-t", dev_path], capture_output=True, check=False, timeout=4.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def get_disc_status(dev_path: str) -> Tuple[int, int]:
    """
    Queries Linux kernel ioctls for drive status and disc type.
    Returns: (drive_status, disc_status)
    """
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
    """
    Checks if a disc is loaded and ready. If audio_only=True, validates via
    CDROM_DISC_STATUS (0x5327) that the disc contains audio tracks (CDS_AUDIO or CDS_MIXED).
    """
    start = time.time()
    while True:
        drive_st, disc_st = get_disc_status(dev_path)

        if drive_st == CDS_DISC_OK:
            if not audio_only:
                return True
            # Audio disc (CDS_AUDIO) or Enhanced/Mixed-mode CD (CDS_MIXED)
            if disc_st in (CDS_AUDIO, CDS_MIXED):
                return True
            # Reject pure data discs (Data CD, DVD, Blu-ray)
            if disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2):
                return False
            # When drive reports CDS_NO_INFO (standard on USB optical drives and many modern SATA drives
            # where CDROM_DISC_STATUS is unpopulated by the bridge/firmware), the disc is loaded and OK.
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


def parse_duration_seconds(dur: Optional[str]) -> Optional[float]:
    """Parses a duration string (e.g. '04:12', '00:45:53.826', or seconds) into float seconds."""
    if not dur:
        return None
    try:
        clean = dur.strip()
        parts = clean.split(":")
        if len(parts) == 3:
            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600.0 + m * 60.0 + s
        if len(parts) == 2:
            m, s = float(parts[0]), float(parts[1])
            return m * 60.0 + s
        return float(clean)
    except Exception:
        return None


def format_time_span(seconds: float) -> str:
    """Formats seconds into human-readable duration like '45s', '4m 35s', or '1h 05m 20s'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _norm_drive_str(s: str) -> str:
    return re.sub(r"[\s_]+", " ", s or "").strip().lower()


def get_whipper_drive_offset(dev_path: str = "/dev/cdrom") -> Optional[int]:
    """Parses drive read offset from whipper.conf if present."""
    whipper_conf = get_whipper_conf()
    if not whipper_conf.exists():
        return None

    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        cp.read(str(whipper_conf), encoding="utf-8")
    except Exception:
        return None

    hw = _norm_drive_str(get_drive_hardware_id(dev_path))

    for sec in cp.sections():
        if not (sec.startswith("drive:") or sec == "drive"):
            continue
        if cp.has_option(sec, "read_offset"):
            try:
                val = int(cp.get(sec, "read_offset").strip())
            except ValueError:
                continue

            v = _norm_drive_str(cp.get(sec, "vendor", fallback=""))
            m = _norm_drive_str(cp.get(sec, "model", fallback=""))
            sec_name = _norm_drive_str(sec.split(":", 1)[1] if ":" in sec else sec)

            if (v and m and v in hw and m in hw) or (sec_name and (sec_name in hw or hw in sec_name)):
                return val

    # Allow fallback only when exactly one optical drive exists on the host and exactly one drive section exists
    optical_drives = find_optical_drives()
    drive_sections = [
        s for s in cp.sections()
        if (s.startswith("drive:") or s == "drive") and cp.has_option(s, "read_offset")
    ]
    if len(optical_drives) == 1 and len(drive_sections) == 1:
        try:
            return int(cp.get(drive_sections[0], "read_offset").strip())
        except ValueError:
            pass

    return None


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
    hw = get_drive_hardware_id(dev_path)
    parts = hw.split("_", 1)
    vendor = parts[0] if len(parts) > 0 else "Unknown"
    model = parts[1] if len(parts) > 1 else "Drive"
    return vendor, model, "1.00"


_whipper_conf_lock = threading.Lock()


@contextlib.contextmanager
def _locked_whipper_conf():
    with _whipper_conf_lock:
        conf_path = get_whipper_conf()
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = conf_path.with_suffix(".lock")
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield conf_path
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(fd)
        except OSError:
            yield conf_path


def _repair_whipper_configparser(cp: configparser.ConfigParser, dev_path: Optional[str] = None) -> bool:
    """Ensures all drive: sections in ConfigParser have vendor, model, and release so Whipper does not crash."""
    changed = False
    hw_target = get_drive_hardware_id(dev_path) if dev_path else ""

    for sec in list(cp.sections()):
        if sec.startswith("drive:"):
            needs_vendor = not cp.has_option(sec, "vendor") or not cp.get(sec, "vendor").strip()
            needs_model = not cp.has_option(sec, "model") or not cp.get(sec, "model").strip()
            needs_release = not cp.has_option(sec, "release") or not cp.get(sec, "release").strip()

            if needs_vendor or needs_model or needs_release:
                sec_name = sec[6:]
                if dev_path and hw_target and hw_target in sec_name:
                    v, m, r = get_drive_details(dev_path)
                else:
                    parts = sec_name.split("_", 1)
                    v = parts[0] if parts else "Unknown"
                    m = parts[1] if len(parts) > 1 else "Optical Drive"
                    r = "1.00"
                if needs_vendor:
                    cp.set(sec, "vendor", v)
                if needs_model:
                    cp.set(sec, "model", m)
                if needs_release:
                    cp.set(sec, "release", r)
                changed = True
    return changed


def repair_whipper_conf(dev_path: Optional[str] = None) -> None:
    """Ensures all drive: sections in whipper.conf have vendor, model, and release so Whipper does not crash."""
    with _locked_whipper_conf() as whipper_conf:
        if not whipper_conf.exists():
            return

        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        try:
            cp.read(str(whipper_conf), encoding="utf-8")
        except Exception:
            return

        if _repair_whipper_configparser(cp, dev_path):
            tmp_file = whipper_conf.with_suffix(f".tmp.{os.getpid()}_{threading.get_ident()}")
            with open(tmp_file, "w", encoding="utf-8") as f:
                cp.write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, whipper_conf)


def _update_whipper_drive_option(dev_path: str, option: str, value: str) -> None:
    with _locked_whipper_conf() as whipper_conf:
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        if whipper_conf.exists():
            try:
                cp.read(str(whipper_conf), encoding="utf-8")
            except Exception:
                pass

        hw_id = get_drive_hardware_id(dev_path)
        hw = _norm_drive_str(hw_id)
        vendor, model, release = get_drive_details(dev_path)

        target_sec = None
        for sec in cp.sections():
            if sec.startswith("drive:"):
                v = _norm_drive_str(cp.get(sec, "vendor", fallback=""))
                m = _norm_drive_str(cp.get(sec, "model", fallback=""))
                sec_name = _norm_drive_str(sec.split(":", 1)[1] if ":" in sec else sec)
                if (v and m and v in hw and m in hw) or (sec_name and (sec_name in hw or hw in sec_name)):
                    target_sec = sec
                    break

        if not target_sec:
            target_sec = f"drive:{hw_id}"

        if not cp.has_section(target_sec):
            cp.add_section(target_sec)

        for k, default_val in (("vendor", vendor), ("model", model), ("release", release)):
            if not cp.has_option(target_sec, k) or not cp.get(target_sec, k).strip():
                cp.set(target_sec, k, default_val)

        cp.set(target_sec, option, value)
        _repair_whipper_configparser(cp, dev_path)

        tmp_file = whipper_conf.with_suffix(f".tmp.{os.getpid()}_{threading.get_ident()}")
        with open(tmp_file, "w", encoding="utf-8") as f:
            cp.write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, whipper_conf)


def store_whipper_offset(dev_path: str, offset: int) -> None:
    """Updates or adds read_offset for this drive in whipper.conf."""
    _update_whipper_drive_option(dev_path, "read_offset", str(offset))


def get_whipper_defeats_cache(dev_path: str = "/dev/cdrom") -> Optional[bool]:
    """Queries defeats_cache setting for this drive from whipper.conf if present."""
    whipper_conf = get_whipper_conf()
    if not whipper_conf.exists():
        return None

    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        cp.read(str(whipper_conf), encoding="utf-8")
    except Exception:
        return None

    hw = _norm_drive_str(get_drive_hardware_id(dev_path))
    for sec in cp.sections():
        if not (sec.startswith("drive:") or sec == "drive"):
            continue
        if cp.has_option(sec, "defeats_cache"):
            v = _norm_drive_str(cp.get(sec, "vendor", fallback=""))
            m = _norm_drive_str(cp.get(sec, "model", fallback=""))
            sec_name = _norm_drive_str(sec.split(":", 1)[1] if ":" in sec else sec)
            if (v and m and v in hw and m in hw) or (sec_name and (sec_name in hw or hw in sec_name)):
                try:
                    return cp.getboolean(sec, "defeats_cache")
                except ValueError:
                    continue

    # Fallback when only a single drive section exists
    drive_sections = [
        s for s in cp.sections()
        if (s.startswith("drive:") or s == "drive") and cp.has_option(s, "defeats_cache")
    ]
    if len(drive_sections) == 1 and len(find_optical_drives()) <= 1:
        try:
            return cp.getboolean(drive_sections[0], "defeats_cache")
        except ValueError:
            pass
    return None


def store_whipper_defeats_cache(dev_path: str, defeats_cache: bool = False) -> None:
    """Stores or updates defeats_cache for this drive in whipper.conf."""
    _update_whipper_drive_option(dev_path, "defeats_cache", "True" if defeats_cache else "False")


def store_whipit_offset(dev_path: str, offset: int) -> None:
    """Stores drive offset in WhipIt's private config file."""
    hw_id = get_drive_hardware_id(dev_path)
    offsets_file = get_config_dir() / "offsets"
    offsets_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if offsets_file.exists():
        for line in offsets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, _ = line.split("=", 1)
                if k.strip() != hw_id:
                    lines.append(line)
    lines.append(f"{hw_id}={offset}")
    tmp_file = offsets_file.with_suffix(".tmp")
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    fd = os.open(tmp_file, flags, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp_file, offsets_file)


def get_stored_drive_offset(dev_path: str = "/dev/cdrom") -> Optional[int]:
    """Returns drive offset, checking WhipIt config first and falling back to whipper.conf."""
    hw_id = get_drive_hardware_id(dev_path)
    offsets_file = get_config_dir() / "offsets"
    if offsets_file.exists():
        for line in offsets_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                if key.strip() == hw_id:
                    try:
                        return int(val.strip())
                    except ValueError:
                        pass

    whipper_offset = get_whipper_drive_offset(dev_path)
    if whipper_offset is not None:
        try:
            store_whipit_offset(dev_path, whipper_offset)
        except Exception:
            pass
        return whipper_offset

    return None


def store_drive_offset(dev_path: str, offset: int) -> None:
    """Stores drive offset in both WhipIt configuration and whipper.conf."""
    store_whipit_offset(dev_path, offset)
    try:
        store_whipper_offset(dev_path, offset)
    except Exception:
        pass


def resolve_optical_drive(
    requested_device: Optional[str] = None,
    ui: Optional[UserInteractionPort] = None,
) -> Optional[str]:
    """Resolves and confirms an optical drive with an Audio CD present using the UI port."""
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
