"""Desktop notifications and audio alerts for rip completion and failures."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def play_alert(success: bool = True) -> None:
    """Plays an audible alert chime and triggers the terminal bell."""
    # 1. Sound the terminal bell immediately
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass

    sound_event = "complete" if success else "dialog-error"

    # 2. Try canberra-gtk-play (standard desktop theme sound)
    if shutil.which("canberra-gtk-play"):
        try:
            subprocess.Popen(
                ["canberra-gtk-play", "-i", sound_event],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass

    # 3. Fallback: try standard freedesktop sounds with pw-play / paplay / aplay
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
                    subprocess.Popen(
                        [player, str(target_sound)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
                except Exception:
                    pass


def send_notification(
    summary: str,
    body: str,
    urgency: str = "normal",
    icon: Optional[Path | str] = None,
) -> None:
    """Sends a freedesktop notification via notify-send."""
    if not shutil.which("notify-send"):
        return

    cmd = ["notify-send", "-a", "WhipIt", "-u", urgency]

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

    cmd.extend([summary, body])

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def notify_rip_success(
    artist: str,
    album: str,
    output_dir: Path,
    track_count: int,
    is_accurate: bool = True,
    cover_art: Optional[Path] = None,
) -> None:
    """Dispatches desktop notification and audio alert for a completed rip."""
    if is_accurate:
        summary = "CD Rip Complete (AccurateRip Verified)"
    else:
        summary = "CD Rip Complete (Unverified)"

    body = (
        f"{artist} - {album}\n"
        f"{track_count} track(s) (FLAC + Opus 192k)\n"
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
