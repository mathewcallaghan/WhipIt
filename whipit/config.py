"""Runtime configuration, directory layout, and optical drive inspection."""

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator

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

DEFAULT_GITHUB_REPO = "mathewcallaghan/WhipIt"

# --- Linux CD-ROM ioctls & Status Constants (Bound early) ---
CDROM_DRIVE_STATUS: int = 0x5326
CDROM_DISC_STATUS: int = 0x5327

CDS_NO_INFO: int = 0
CDS_NO_DISC: int = 1
CDS_TRAY_OPEN: int = 2
CDS_DRIVE_NOT_READY: int = 3
CDS_DISC_OK: int = 4

CDS_AUDIO: int = 100
CDS_DATA_1: int = 101
CDS_DATA_2: int = 102
CDS_XA_2_1: int = 103
CDS_XA_2_2: int = 104
CDS_MIXED: int = 105


# --- Core Directory Layout (Defined early to eliminate circular imports) ---

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


# --- Re-export Drive Service Subsystem ---
from whipit.services.drive_service import (
    DriveService,
    _is_ripper_holding_device,
    _orig_subprocess_run,
    _proc_starttime,
    close_tray,
    disable_usb_autosuspend,
    drive_has_disc,
    eject_drive,
    find_optical_drives,
    get_disc_status,
    get_drive_details,
    get_drive_hardware_id,
    get_drive_max_speed,
    get_drives_with_hardware_ids,
    is_usb_drive,
    release_drive_locks,
    reset_optical_drive,
    resolve_optical_drive,
    unlock_drive,
    wait_for_disc_ready,
)


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

    optical_drives = find_optical_drives()
    drive_sections = [
        s for s in cp.sections()
        if (s.startswith("drive:") or s == "drive") and cp.has_option(s, "read_offset")
    ]
    if len(optical_drives) == 1 and len(drive_sections) == 1 and drive_sections[0] == "drive":
        v = cp.get(drive_sections[0], "vendor", fallback="").strip()
        m = cp.get(drive_sections[0], "model", fallback="").strip()
        if not v and not m:
            try:
                return int(cp.get(drive_sections[0], "read_offset").strip())
            except ValueError:
                pass

    return None


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
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield conf_path
            finally:
                try:
                    import fcntl
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

    drive_sections = [
        s for s in cp.sections()
        if (s.startswith("drive:") or s == "drive") and cp.has_option(s, "defeats_cache")
    ]
    if len(drive_sections) == 1 and len(find_optical_drives()) <= 1 and drive_sections[0] == "drive":
        v = cp.get(drive_sections[0], "vendor", fallback="").strip()
        m = cp.get(drive_sections[0], "model", fallback="").strip()
        if not v and not m:
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
