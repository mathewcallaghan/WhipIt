"""Inhibits OS media handling dialogues and automounting across GNOME, XFCE, MATE, and Cinnamon."""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
import threading
from whipit.config import find_optical_drives, get_log_dir
from whipit.models import InvariantViolationError
from whipit.ui.base import LogLevel, UserInteractionPort

logger = logging.getLogger(__name__)

THUNAR_PROPERTIES = [
    "/autoplay-audio-cds/enabled",
    "/autobrowse/enabled",
    "/automount-media/enabled",
    "/automount-drives/enabled",
    "/autoopen/enabled",
    "/autorun/enabled",
    "/autoplay-video-cds/enabled",
]

GSETTINGS_SCHEMAS = [
    "org.gnome.desktop.media-handling",
    "org.mate.media-handling",
    "org.cinnamon.desktop.media-handling",
]

_inhibitor_lock = threading.Lock()
_shared_inhibitor: Optional[MediaDialogInhibitor] = None
_inhibit_ref_count: int = 0


def restore_setting(
    command: List[str],
    description: str,
    timeout: float = 5.0,
) -> bool:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not restore %s: %s", description, exc)
        return False

    if result is not None and getattr(result, "returncode", 0) != 0:
        err = getattr(result, "stderr", "")
        err_msg = err[-1000:] if err else "unknown error"
        logger.warning(
            "Restoring %s failed (exit=%s): %s",
            description,
            getattr(result, "returncode", "unknown"),
            err_msg.strip(),
        )
        return False

    return True


def _sync_dir(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _write_state_file(sf: Path, payload: Dict[str, Any]) -> bool:
    """Atomically and durably writes recovery state to disk with full file and directory sync."""
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = sf.with_suffix(f".tmp.{os.getpid()}")
    try:
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, sf)
        _sync_dir(sf.parent)
        return True
    except OSError as exc:
        logger.warning("Failed persisting desktop settings recovery state to %s: %s", sf, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def unmount_cdda_device(dev_path: Optional[str] = None) -> None:
    """Silently unmounts any data filesystems or GVfs cdda:// virtual mounts."""
    subprocess.run(["pkill", "-x", "thunar-volman"], capture_output=True, check=False)

    devices = [dev_path] if dev_path else find_optical_drives()
    for d in devices:
        if not d:
            continue
        block_name = Path(d).name
        for target in (d, f"cdda://{block_name}/", f"cdda://{block_name}"):
            subprocess.run(["gio", "mount", "-u", target], capture_output=True, check=False)
        subprocess.run(["udisksctl", "unmount", "-b", d], capture_output=True, check=False)


class MediaDialogInhibitor:
    def __init__(self, device: Optional[str] = None):
        self.device = device
        self.gnome_cookie: Optional[int] = None
        self.pm_cookie: Optional[int] = None
        self.ss_cookie: Optional[int] = None
        self._dbus_proc: Optional[subprocess.Popen] = None
        self._orig_gsettings: Dict[str, Dict[str, str]] = {}
        self._orig_xfconf: Dict[str, str] = {}
        self._has_gsettings = bool(shutil.which("gsettings"))
        self._has_xfconf = bool(shutil.which("xfconf-query"))
        self.pending_restoration: bool = False
        self.pending_restoration_details: Dict[str, Any] = {}

    @staticmethod
    def state_file() -> Path:
        return get_log_dir() / "inhibit_state.json"

    @classmethod
    def has_pending_state(cls) -> bool:
        return cls.state_file().exists()

    @classmethod
    def _load_pending_state(cls) -> Dict[str, Any]:
        """Reads existing on-disk state without clearing it."""
        sf = cls.state_file()
        if not sf.exists():
            return {}
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.warning("Failed reading state file %s: %s", sf, exc)
        return {}

    @classmethod
    def restore_persisted_state(cls) -> bool:
        """Restores desktop settings from disk if a previous run crashed unexpectedly."""
        sf = cls.state_file()
        if not sf.exists():
            return False

        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed parsing inhibit state file %s: %s", sf, exc)
            sf.unlink(missing_ok=True)
            return False

        if not isinstance(data, dict):
            sf.unlink(missing_ok=True)
            return False

        restored_any = False
        allowed_keys = {"autorun-never", "automount-open", "automount"}
        allowed_booleans = {"true", "false"}

        remaining_xfconf: Dict[str, str] = {}
        has_xf = bool(shutil.which("xfconf-query"))
        if isinstance(data.get("xfconf"), dict):
            for prop, val in data["xfconf"].items():
                clean_val = str(val).strip().strip("'\"").lower()
                if prop in THUNAR_PROPERTIES and clean_val in allowed_booleans:
                    if has_xf:
                        cmd = ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", clean_val]
                        if restore_setting(cmd, f"xfconf prop {prop}={clean_val}"):
                            restored_any = True
                        else:
                            remaining_xfconf[prop] = clean_val
                    else:
                        remaining_xfconf[prop] = clean_val

        remaining_gsettings: Dict[str, Dict[str, str]] = {}
        has_gset = bool(shutil.which("gsettings"))
        if isinstance(data.get("gsettings"), dict):
            for schema, key_vals in data["gsettings"].items():
                if schema not in GSETTINGS_SCHEMAS or not isinstance(key_vals, dict):
                    continue
                for key, val in key_vals.items():
                    clean_val = str(val).strip().strip("'\"").lower()
                    if key in allowed_keys and clean_val in allowed_booleans:
                        if has_gset:
                            cmd = ["gsettings", "set", schema, key, clean_val]
                            if restore_setting(cmd, f"gsettings {schema} {key}={clean_val}"):
                                restored_any = True
                            else:
                                remaining_gsettings.setdefault(schema, {})[key] = clean_val
                        else:
                            remaining_gsettings.setdefault(schema, {})[key] = clean_val

        if remaining_xfconf or remaining_gsettings:
            updated_payload = {"xfconf": remaining_xfconf, "gsettings": remaining_gsettings}
            if not _write_state_file(sf, updated_payload):
                logger.warning("Failed updating remaining inhibit state %s", sf)
            logger.warning(
                "Desktop settings partial restoration: %d setting(s) could not be restored and remain pending: %s",
                len(remaining_xfconf) + sum(len(v) for v in remaining_gsettings.values()),
                updated_payload,
            )
        else:
            sf.unlink(missing_ok=True)
            _sync_dir(sf.parent)

        return restored_any

    def _merge_pending_state(self) -> None:
        """Merges any unresolved settings from disk, ensuring historical original values are never overwritten."""
        pending = self._load_pending_state()
        if not pending:
            return

        pending_xf = pending.get("xfconf")
        if isinstance(pending_xf, dict):
            for prop, val in pending_xf.items():
                clean_val = str(val).strip().strip("'\"").lower()
                if prop in THUNAR_PROPERTIES and clean_val in ("true", "false"):
                    self._orig_xfconf[prop] = clean_val

        pending_gset = pending.get("gsettings")
        if isinstance(pending_gset, dict):
            for schema, key_vals in pending_gset.items():
                if schema in GSETTINGS_SCHEMAS and isinstance(key_vals, dict):
                    for key, val in key_vals.items():
                        clean_val = str(val).strip().strip("'\"").lower()
                        if key in ("autorun-never", "automount-open", "automount") and clean_val in ("true", "false"):
                            self._orig_gsettings.setdefault(schema, {})[key] = clean_val

    def _persist_state(self) -> bool:
        """Atomically saves the captured and merged original settings to disk with directory sync."""
        if not self._orig_xfconf and not self._orig_gsettings:
            return True

        payload = {
            "xfconf": self._orig_xfconf,
            "gsettings": self._orig_gsettings,
        }
        return _write_state_file(self.state_file(), payload)

    def _start_gnome_session_inhibit(self) -> None:
        """
        Takes an inhibitor tied to a persistent session bus connection.
        Prefers QtDBus (in-process) or a persistent background bus bridge.
        """
        try:
            from PyQt6.QtDBus import QDBusConnection, QDBusInterface
            bus = QDBusConnection.sessionBus()
            if bus.isConnected():
                iface = QDBusInterface(
                    "org.gnome.SessionManager",
                    "/org/gnome/SessionManager",
                    "org.gnome.SessionManager",
                    bus,
                )
                if iface.isValid():
                    # GNOME Inhibit flags: 4 = Inhibit Suspend, 8 = Inhibit Idle (4 | 8 = 12)
                    reply = iface.call("Inhibit", "whipit", 0, "Archival CD ripping in progress", 12)
                    if reply and reply.arguments():
                        self.gnome_cookie = int(reply.arguments()[0])

                # Universal FreeDesktop power management and screensaver inhibition
                iface_pm = QDBusInterface(
                    "org.freedesktop.PowerManagement.Inhibit",
                    "/org/freedesktop/PowerManagement/Inhibit",
                    "org.freedesktop.PowerManagement.Inhibit",
                    bus,
                )
                if iface_pm.isValid():
                    reply_pm = iface_pm.call("Inhibit", "whipit", "Archival CD ripping in progress")
                    if reply_pm and reply_pm.arguments():
                        self.pm_cookie = int(reply_pm.arguments()[0])

                iface_ss = QDBusInterface(
                    "org.freedesktop.ScreenSaver",
                    "/org/freedesktop/ScreenSaver",
                    "org.freedesktop.ScreenSaver",
                    bus,
                )
                if iface_ss.isValid():
                    reply_ss = iface_ss.call("Inhibit", "whipit", "Archival CD ripping in progress")
                    if reply_ss and reply_ss.arguments():
                        self.ss_cookie = int(reply_ss.arguments()[0])
                        return
        except Exception as exc:
            logger.debug("QtDBus session inhibit not available: %s", exc)

        if shutil.which("gdbus"):
            script = """
import sys, subprocess, re
proc = subprocess.Popen(
    ["gdbus", "call", "--session", "--dest", "org.gnome.SessionManager",
     "--object-path", "/org/gnome/SessionManager",
     "--method", "org.gnome.SessionManager.Inhibit",
     "whipit", "0", "Archival CD ripping in progress", "16"],
    stdout=subprocess.PIPE, text=True
)
out, _ = proc.communicate()
sys.stdout.write(out + "\\n")
sys.stdout.flush()
sys.stdin.read()
"""
            try:
                p = subprocess.Popen(
                    [sys.executable, "-c", script],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                if p.stdout:
                    line = p.stdout.readline()
                    m = re.search(r"\(uint32\s+(\d+),\)", line)
                    if m:
                        self.gnome_cookie = int(m.group(1))
                        self._dbus_proc = p
            except Exception as exc:
                logger.debug("Failed spawning gdbus session bridge: %s", exc)

    def _capture_original_settings(self) -> None:
        """Captures existing desktop settings without modifying them."""
        self._orig_xfconf.clear()
        self._orig_gsettings.clear()

        if self._has_xfconf:
            for prop in THUNAR_PROPERTIES:
                res = subprocess.run(
                    ["xfconf-query", "-c", "thunar-volman", "-p", prop],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    val = res.stdout.strip().strip("'\"")
                    if val.lower() != "false":
                        self._orig_xfconf[prop] = val

        if self._has_gsettings:
            for schema in GSETTINGS_SCHEMAS:
                chk = subprocess.run(["gsettings", "list-keys", schema], capture_output=True, text=True, check=False)
                if chk.returncode != 0:
                    continue

                keys = chk.stdout.splitlines()
                for key in ("autorun-never", "automount-open", "automount"):
                    if key in keys:
                        r = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, check=False)
                        if r.returncode == 0:
                            val = r.stdout.strip().strip("'\"")
                            target_val = "true" if key == "autorun-never" else "false"
                            if val.lower() != target_val.lower():
                                self._orig_gsettings.setdefault(schema, {})[key] = val

    def _apply_desktop_setting_changes(self) -> None:
        """Applies automount suppression settings only after originals are persisted."""
        if self._has_xfconf and self._orig_xfconf:
            for prop in self._orig_xfconf:
                subprocess.run(
                    ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", "false"],
                    capture_output=True,
                    text=True,
                    check=False,
                )

        if self._has_gsettings and self._orig_gsettings:
            for schema, key_vals in self._orig_gsettings.items():
                for key in key_vals:
                    target_val = "true" if key == "autorun-never" else "false"
                    subprocess.run(
                        ["gsettings", "set", schema, key, target_val],
                        capture_output=True,
                        text=True,
                        check=False,
                    )

    def start(self, ui: Optional[UserInteractionPort] = None) -> None:
        self.restore_persisted_state()
        unmount_cdda_device(self.device)

        self._capture_original_settings()
        self._merge_pending_state()

        pending = self._load_pending_state()
        if pending and (pending.get("xfconf") or pending.get("gsettings")):
            self.pending_restoration = True
            self.pending_restoration_details = pending
            warn_msg = (
                "Desktop media automount settings from a previous session could not be fully restored; "
                "original values remain preserved and restoration will be retried on exit."
            )
            logger.warning("%s (pending: %s)", warn_msg, pending)
            if ui:
                ui.on_log(LogLevel.WARN, warn_msg)
        else:
            self.pending_restoration = False
            self.pending_restoration_details = {}

        if not self._persist_state():
            raise InvariantViolationError(
                "Cannot safely suppress desktop automount: "
                "recovery state could not be persisted."
            )
        self._apply_desktop_setting_changes()
        self._start_gnome_session_inhibit()

    def stop(self) -> bool:
        xfconf_failed = {}
        if self._has_xfconf and self._orig_xfconf:
            if shutil.which("xfconf-query"):
                for prop, val in list(self._orig_xfconf.items()):
                    cmd = ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", val]
                    if restore_setting(cmd, f"xfconf prop {prop}={val}"):
                        self._orig_xfconf.pop(prop, None)
                    else:
                        xfconf_failed[prop] = val
            else:
                xfconf_failed = dict(self._orig_xfconf)
        elif self._orig_xfconf:
            xfconf_failed = dict(self._orig_xfconf)

        gsettings_failed = {}
        if self._has_gsettings and self._orig_gsettings:
            if shutil.which("gsettings"):
                for schema, key_vals in list(self._orig_gsettings.items()):
                    for key, val in list(key_vals.items()):
                        cmd = ["gsettings", "set", schema, key, val]
                        if restore_setting(cmd, f"gsettings {schema} {key}={val}"):
                            key_vals.pop(key, None)
                        else:
                            gsettings_failed.setdefault(schema, {})[key] = val
                    if not key_vals:
                        self._orig_gsettings.pop(schema, None)
            else:
                gsettings_failed = {s: dict(kv) for s, kv in self._orig_gsettings.items()}
        elif self._orig_gsettings:
            gsettings_failed = {s: dict(kv) for s, kv in self._orig_gsettings.items()}

        sf = self.state_file()
        if xfconf_failed or gsettings_failed:
            self._orig_xfconf = xfconf_failed
            self._orig_gsettings = gsettings_failed
            self._persist_state()
            self.pending_restoration = True
            self.pending_restoration_details = {
                "xfconf": xfconf_failed,
                "gsettings": gsettings_failed,
            }
            logger.warning(
                "Desktop settings restoration incomplete on exit; %d setting(s) remain un-restored and saved in %s",
                len(xfconf_failed) + sum(len(v) for v in gsettings_failed.values()),
                sf,
            )
            all_succeeded = False
        else:
            if sf.exists():
                sf.unlink(missing_ok=True)
                _sync_dir(sf.parent)
            self.pending_restoration = False
            self.pending_restoration_details = {}
            all_succeeded = True

        if self.gnome_cookie is not None:
            try:
                from PyQt6.QtDBus import QDBusConnection, QDBusInterface
                bus = QDBusConnection.sessionBus()
                if bus.isConnected():
                    iface = QDBusInterface(
                        "org.gnome.SessionManager",
                        "/org/gnome/SessionManager",
                        "org.gnome.SessionManager",
                        bus,
                    )
                    if iface.isValid():
                        iface.call("Uninhibit", self.gnome_cookie)
            except Exception as exc:
                logger.debug("Failed releasing GNOME session inhibitor via QtDBus: %s", exc)
            self.gnome_cookie = None

        if (self.pm_cookie is not None or self.ss_cookie is not None) and "QDBusConnection" in sys.modules:
            try:
                from PyQt6.QtDBus import QDBusConnection, QDBusInterface
                bus = QDBusConnection.sessionBus()
                if bus.isConnected():
                    if self.pm_cookie is not None:
                        QDBusInterface("org.freedesktop.PowerManagement.Inhibit", "/org/freedesktop/PowerManagement/Inhibit", "org.freedesktop.PowerManagement.Inhibit", bus).call("UnInhibit", self.pm_cookie)
                    if self.ss_cookie is not None:
                        QDBusInterface("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver", "org.freedesktop.ScreenSaver", bus).call("UnInhibit", self.ss_cookie)
            except Exception as exc:
                logger.debug("Failed releasing freedesktop power inhibitor: %s", exc)
            self.pm_cookie = None
            self.ss_cookie = None

        if self._dbus_proc:
            try:
                if self._dbus_proc.stdin:
                    self._dbus_proc.stdin.close()
                self._dbus_proc.terminate()
                self._dbus_proc.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.debug("Terminating D-Bus bridge process failed: %s", exc)
            self._dbus_proc = None

        return all_succeeded


@contextlib.contextmanager
def suppress_media_dialogs(
    device: Optional[str] = None,
    ui: Optional[UserInteractionPort] = None,
) -> Generator[MediaDialogInhibitor, None, None]:
    """Thread-safe reference-counted context manager ensuring automount suppression remains active during concurrent rips."""
    global _shared_inhibitor, _inhibit_ref_count
    with _inhibitor_lock:
        if _inhibit_ref_count == 0 or _shared_inhibitor is None:
            _shared_inhibitor = MediaDialogInhibitor(device)
            _shared_inhibitor.start(ui=ui)
            atexit.register(_shared_inhibitor.stop)
        _inhibit_ref_count += 1
        active = _shared_inhibitor

    if device:
        unmount_cdda_device(device)

    try:
        yield active
    finally:
        with _inhibitor_lock:
            _inhibit_ref_count = max(0, _inhibit_ref_count - 1)
            if _inhibit_ref_count == 0 and _shared_inhibitor is not None:
                atexit.unregister(_shared_inhibitor.stop)
                _shared_inhibitor.stop()
                _shared_inhibitor = None
