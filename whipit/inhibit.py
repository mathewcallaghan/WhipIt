"""Inhibits OS media handling dialogues and automounting across GNOME, XFCE, MATE, and Cinnamon."""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Generator, List, Optional
from whipit.config import find_optical_drives

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
    # Dismiss any active dialog already created by thunar-volman
    subprocess.run(["pkill", "-f", "thunar-volman"], capture_output=True, check=False)

    devices = [dev_path] if dev_path else find_optical_drives()
    for d in devices:
        if not d:
            continue
        block_name = Path(d).name
        # Unmount GVfs virtual cdda volume
        for target in (d, f"cdda://{block_name}/", f"cdda://{block_name}"):
            subprocess.run(["gio", "mount", "-u", target], capture_output=True, check=False)

        # Unmount any ISO-9660 or data partition via udisksctl
        subprocess.run(["udisksctl", "unmount", "-b", d], capture_output=True, check=False)


class MediaDialogInhibitor:
    def __init__(self, device: Optional[str] = None):
        self.device = device
        self.cookie: Optional[int] = None
        self._orig_gsettings: Dict[str, Dict[str, str]] = {}
        self._orig_xfconf: Dict[str, str] = {}
        self._has_gsettings = bool(shutil.which("gsettings"))
        self._has_xfconf = bool(shutil.which("xfconf-query"))

    def start(self) -> None:
        # 1. Unmount existing partitions/mounts and dismiss any lingering dialogs
        unmount_cdda_device(self.device)

        # 2. XFCE (thunar-volman) Suppression
        if self._has_xfconf:
            for prop in THUNAR_PROPERTIES:
                # Query current value
                res = subprocess.run(
                    ["xfconf-query", "-c", "thunar-volman", "-p", prop],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0:
                    val = res.stdout.strip()
                    self._orig_xfconf[prop] = val
                    # Temporarily disable
                    subprocess.run(
                        ["xfconf-query", "-c", "thunar-volman", "-p", prop, "-s", "false"],
                        capture_output=True,
                        check=False,
                    )

        # 3. GNOME / MATE / Cinnamon GSettings Suppression
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

        # 4. GNOME Session D-Bus Inhibit (Flag 16 = INHIBIT_AUTOMOUNT)
        if shutil.which("gdbus"):
            try:
                res = subprocess.run(
                    [
                        "gdbus", "call", "--session",
                        "--dest", "org.gnome.SessionManager",
                        "--object-path", "/org/gnome/SessionManager",
                        "--method", "org.gnome.SessionManager.Inhibit",
                        "whipit", "0", "Archival CD ripping in progress", "16"
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                m = re.search(r"\(uint32\s+(\d+),\)", res.stdout)
                if m:
                    self.cookie = int(m.group(1))
            except Exception:
                pass

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

        # 3. Release GNOME D-Bus inhibitor
        if self.cookie is not None and shutil.which("gdbus"):
            try:
                subprocess.run(
                    [
                        "gdbus", "call", "--session",
                        "--dest", "org.gnome.SessionManager",
                        "--object-path", "/org/gnome/SessionManager",
                        "--method", "org.gnome.SessionManager.Uninhibit",
                        str(self.cookie)
                    ],
                    capture_output=True,
                    check=False,
                )
                self.cookie = None
            except Exception:
                pass


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
