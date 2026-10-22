"""Inhibits OS media handling dialogues and automounting across GNOME, XFCE, MATE, and Cinnamon."""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Generator, List, Optional
from whipit.config import find_optical_drives, get_log_dir

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
        self._dbus_proc: Optional[subprocess.Popen] = None
        self._orig_gsettings: Dict[str, Dict[str, str]] = {}
        self._orig_xfconf: Dict[str, str] = {}
        self._has_gsettings = bool(shutil.which("gsettings"))
        self._has_xfconf = bool(shutil.which("xfconf-query"))

    @staticmethod
    def state_file() -> Path:
        return get_log_dir() / "inhibit_state.json"

    @classmethod
    def restore_persisted_state(cls) -> bool:
        """Restores desktop settings from disk if a previous run crashed unexpectedly."""
        sf = cls.state_file()
        if not sf.exists():
            return False

        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            sf.unlink(missing_ok=True)
            return False

        if not isinstance(data, dict):
            sf.unlink(missing_ok=True)
            return False

        restored_any = False
        allowed_keys = {"autorun-never", "automount-open", "automount"}
        allowed_booleans = {"true", "false"}

        if shutil.which("xfconf-query") and isinstance(data.get("xfconf"), dict):
            for prop, val in data["xfconf"].items():
                clean_val = str(val).strip().lower()
                if prop in THUNAR_PROPERTIES and clean_val in allowed_booleans:
                    subprocess.run(
                        ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", clean_val],
                        capture_output=True,
                        check=False,
                    )
                    restored_any = True

        if shutil.which("gsettings") and isinstance(data.get("gsettings"), dict):
            for schema, key_vals in data["gsettings"].items():
                if schema not in GSETTINGS_SCHEMAS or not isinstance(key_vals, dict):
                    continue
                for key, val in key_vals.items():
                    clean_val = str(val).strip().lower()
                    if key in allowed_keys and clean_val in allowed_booleans:
                        subprocess.run(["gsettings", "set", schema, key, clean_val], capture_output=True, check=False)
                        restored_any = True

        sf.unlink(missing_ok=True)
        return restored_any

    def _persist_state(self) -> None:
        """Atomically saves the captured original settings to disk."""
        payload = {
            "xfconf": self._orig_xfconf,
            "gsettings": self._orig_gsettings,
        }
        sf = self.state_file()
        tmp = sf.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, sf)
        except OSError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def _start_gnome_session_inhibit(self) -> None:
        """
        Takes an inhibitor tied to a persistent session bus connection.
        Prefers QtDBus (in-process) or a persistent background bus bridge.
        """
        # Option A: In-process persistent connection via PyQt6.QtDBus
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
                    reply = iface.call("Inhibit", "whipit", 0, "Archival CD ripping in progress", 16)
                    if reply and reply.arguments():
                        self.gnome_cookie = int(reply.arguments()[0])
                        return
        except Exception:
            pass

        # Option B: Persistent background bridge holding the connection open
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
# Keep bus connection active until parent closes stdin pipe
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
            except Exception:
                pass

    def start(self) -> None:
        # 1. Recover any leftover settings from a prior crashed process
        self.restore_persisted_state()

        # 2. Unmount existing partitions/mounts and dismiss lingering dialogs
        unmount_cdda_device(self.device)

        # 3. Read current XFCE thunar-volman properties
        if self._has_xfconf:
            for prop in THUNAR_PROPERTIES:
                res = subprocess.run(
                    ["xfconf-query", "-c", "thunar-volman", "-p", prop],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    self._orig_xfconf[prop] = res.stdout.strip()
                    subprocess.run(
                        ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", "false"],
                        capture_output=True,
                        check=False,
                    )

        # 4. Read current GNOME / MATE / Cinnamon GSettings
        if self._has_gsettings:
            for schema in GSETTINGS_SCHEMAS:
                chk = subprocess.run(["gsettings", "list-keys", schema], capture_output=True, text=True, check=False)
                if chk.returncode != 0:
                    continue

                self._orig_gsettings[schema] = {}
                keys = chk.stdout.splitlines()

                for key in ("autorun-never", "automount-open", "automount"):
                    if key in keys:
                        r = subprocess.run(["gsettings", "get", schema, key], capture_output=True, text=True, check=False)
                        if r.returncode == 0:
                            self._orig_gsettings[schema][key] = r.stdout.strip()
                            target_val = "true" if key == "autorun-never" else "false"
                            subprocess.run(["gsettings", "set", schema, key, target_val], capture_output=True, check=False)

        # 5. Persist original settings to disk for crash safety
        self._persist_state()

        # 6. Take GNOME Session D-Bus Inhibit (Flag 16 = INHIBIT_AUTOMOUNT)
        self._start_gnome_session_inhibit()

    def stop(self) -> None:
        # 1. Restore XFCE thunar-volman properties
        if self._has_xfconf and self._orig_xfconf:
            for prop, val in self._orig_xfconf.items():
                subprocess.run(
                    ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", val],
                    capture_output=True,
                    check=False,
                )
            self._orig_xfconf.clear()

        # 2. Restore GSettings properties
        if self._has_gsettings and self._orig_gsettings:
            for schema, key_vals in self._orig_gsettings.items():
                for key, val in key_vals.items():
                    subprocess.run(["gsettings", "set", schema, key, val], capture_output=True, check=False)
            self._orig_gsettings.clear()

        # 3. Clean up on-disk state file
        sf = self.state_file()
        if sf.exists():
            sf.unlink(missing_ok=True)

        # 4. Release GNOME D-Bus inhibitor
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
            except Exception:
                pass
            self.gnome_cookie = None

        if self._dbus_proc:
            try:
                if self._dbus_proc.stdin:
                    self._dbus_proc.stdin.close()
                self._dbus_proc.terminate()
                self._dbus_proc.wait(timeout=1.0)
            except Exception:
                pass
            self._dbus_proc = None


@contextlib.contextmanager
def suppress_media_dialogs(device: Optional[str] = None) -> Generator[None, None, None]:
    """Context manager ensuring OS media dialogues are suppressed and safely restored on exit."""
    inhibitor = MediaDialogInhibitor(device)
    inhibitor.start()
    atexit.register(inhibitor.stop)
    try:
        yield
    finally:
        atexit.unregister(inhibitor.stop)
        inhibitor.stop()
