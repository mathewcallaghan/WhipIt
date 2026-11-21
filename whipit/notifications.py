"""Desktop notifications and audio alerts for rip completion and failures."""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _spawn_detached(cmd: List[str]) -> None:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if proc is not None:
        threading.Thread(target=proc.wait, daemon=True).start()


def play_alert(success: bool = True) -> None:
    """Plays an audible alert chime and triggers the terminal bell."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception as exc:
        logger.debug("Terminal bell alert failed: %s", exc)

    sound_event = "complete" if success else "dialog-error"

    if shutil.which("canberra-gtk-play"):
        try:
            _spawn_detached(["canberra-gtk-play", "-i", sound_event])
            return
        except Exception as exc:
            logger.debug("canberra-gtk-play alert failed: %s", exc)

    sound_files = [
        Path(f"/usr/share/sounds/freedesktop/stereo/{sound_event}.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/message.oga"),
        Path("/usr/share/sounds/sound-icons/prompt.wav"),
    ]
    target_sound = next((p for p in sound_files if p.exists()), None)

    if target_sound:
        for player in ("pw-play", "paplay", "aplay"):
            if shutil.which(player):
                try:
                    _spawn_detached([player, str(target_sound)])
                    return
                except Exception as exc:
                    logger.debug("Sound player %s failed: %s", player, exc)


def send_notification(
    summary: str,
    body: str,
    urgency: str = "normal",
    icon: Optional[Path | str] = None,
    timeout_ms: Optional[int] = None,
) -> None:
    """Sends a freedesktop notification via notify-send."""
    if not shutil.which("notify-send"):
        return

    if os.environ.get("PYTEST_CURRENT_TEST"):
        urgency = "low" if urgency == "critical" else urgency
        timeout_ms = timeout_ms if timeout_ms is not None else 1000

    cmd = ["notify-send", "-a", "WhipIt", "-u", urgency]
    if timeout_ms is not None:
        cmd.extend(["-t", str(timeout_ms)])

    from whipit.launcher import get_icon_path
    custom_icon = get_icon_path()

    icon_str = None
    if icon:
        if isinstance(icon, Path) and icon.exists():
            icon_str = str(icon.resolve())
        elif isinstance(icon, str):
            icon_str = icon

    if icon_str:
        cmd.extend(["-i", icon_str])
    elif custom_icon.exists():
        cmd.extend(["-i", str(custom_icon)])
    else:
        cmd.extend(["-i", "media-optical"])

    cmd.extend(["--", html.escape(summary), html.escape(body)])

    try:
        _spawn_detached(cmd)
    except Exception as exc:
        logger.debug("Desktop notification dispatch failed: %s", exc)


def notify_rip_success(
    artist: str,
    album: str,
    output_dir: Path,
    track_count: int,
    is_accurate: bool = True,
    cover_art: Optional[Path] = None,
    is_partial: bool = False,
    generate_opus: bool = True,
    opus_bitrate: int = 192,
) -> None:
    """Dispatches desktop notification and audio alert for a completed rip."""
    if is_partial:
        summary = "CD Rip Complete (Partial Archive Saved)"
    elif is_accurate:
        summary = "CD Rip Complete (AccurateRip Verified)"
    else:
        summary = "CD Rip Complete (Unverified)"

    fmt_label = f"FLAC + Opus {opus_bitrate}k" if generate_opus else "FLAC"
    body = (
        f"{artist} - {album}\n"
        f"{track_count} track(s) ({fmt_label})\n"
        f"Saved to: {output_dir.name}"
    )

    send_notification(summary, body, urgency="normal", icon=cover_art)
    play_alert(success=True)


def notify_rip_failed(
    artist: str,
    album: str,
    reason: str,
    cover_art: Optional[Path] = None,
) -> None:
    """Dispatches desktop notification and audio alert for a failed rip."""
    summary = "CD Rip Failed"
    body = f"{artist} - {album}\n{reason}"

    send_notification(summary, body, urgency="critical", icon=cover_art or "dialog-error")
    play_alert(success=False)
