"""Drive calibration wizard to determine AccurateRip read offset via UI interaction port."""

from __future__ import annotations

import subprocess
from typing import Optional
from whipit.config import eject_drive, store_drive_offset
from whipit.ui.base import CalibrationAction, LogLevel, UserInteractionPort
from whipit.ui.base import CalibrationAction, LogLevel, NullInteractionAdapter, UserInteractionPort


def calibrate_drive(
    dev: str = "/dev/cdrom",
    ui: Optional[UserInteractionPort] = None,
) -> Optional[int]:
    """Runs drive calibration workflow to determine AccurateRip offset."""
    if ui is None:
        ui = NullInteractionAdapter()

    initial = ui.prompt_calibration_start(dev)
    if initial is None or initial.action == CalibrationAction.ABORT:
        return None

    ui.on_log(LogLevel.INFO, "Analyzing drive cache capabilities with Whipper...")
    subprocess.run(["whipper", "drive", "analyze", "-d", dev], check=False)

    if initial.action == CalibrationAction.MANUAL_OFFSET and initial.manual_offset is not None:
        store_drive_offset(dev, initial.manual_offset)
        ui.on_log(LogLevel.OK, f"Stored read offset {initial.manual_offset} for {dev}.")
        return initial.manual_offset

    while True:
        ui.on_log(LogLevel.INFO, "Searching for read offset in AccurateRip database...")
        res = subprocess.run(["whipper", "offset", "find", "-d", dev], check=False)
        if res.returncode == 0:
            ui.on_log(LogLevel.OK, "Offset detected and saved by Whipper!")
            return None

        choice = ui.prompt_calibration_retry(dev)
        if choice.action == CalibrationAction.MANUAL_OFFSET and choice.manual_offset is not None:
            store_drive_offset(dev, choice.manual_offset)
            ui.on_log(LogLevel.OK, f"Stored read offset {choice.manual_offset} for {dev}.")
            return choice.manual_offset
        if choice.action == CalibrationAction.SWAP_DISC:
            eject_drive(dev)
            if not ui.wait_for_user_reload(dev, "\nInsert another commercial Audio CD and press [Enter]..."):
                return None
        elif choice.action == CalibrationAction.USE_ZERO:
            ui.on_log(LogLevel.WARN, "Using 0 as session fallback.")
            return 0
        else:
            return None
