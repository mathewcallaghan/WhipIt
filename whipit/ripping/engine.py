"""Optical disc extraction engine and recovery loop decoupled via UserInteractionPort."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from whipit.config import (
    drive_has_disc,
    eject_drive,
    get_drive_hardware_id,
    get_drives_with_hardware_ids,
    get_stored_drive_offset,
    release_drive_locks,
)
from whipit.inhibit import unmount_cdda_device
from whipit.ripping.calibration import calibrate_drive
from whipit.ripping.whipper import WhipperRunner
from whipit.ui.base import (
    DiscPromptAction,
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
    NullInteractionAdapter,
    UserInteractionPort,
)


@dataclass
class ExtractionResult:
    success: bool
    partial: bool
    device_path: str
    effective_offset: Optional[int]
    speed: Optional[int]


class RippingEngine:
    def __init__(
        self,
        cache_dir: Path,
        stage_dir: Path,
        device_path: str,
        speed: Optional[int] = None,
        offset: Optional[int] = None,
        release_id: Optional[str] = None,
        ui: Optional[UserInteractionPort] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.cache_dir = cache_dir
        self.stage_dir = stage_dir
        self.device_path = device_path
        self.speed = speed
        self.offset = offset
        self.release_id = release_id
        self.ui = ui or NullInteractionAdapter()
        self.cancel_event = cancel_event
        self.runner: Optional[WhipperRunner] = None

    def cancel(self) -> None:
        if self.runner:
            self.runner.cancel()
        release_drive_locks(self.device_path)

    def run_extraction(
        self,
        keep_going: bool = False,
        unknown_disc: bool = False,
    ) -> ExtractionResult:
        runner = WhipperRunner(
            self.cache_dir,
            self.device_path,
            self.speed,
            output_callback=self.ui.on_whipper_output,
        )
        self.runner = runner
        effective_offset = self.offset
        failed = False
        cdr_enabled = False

        try:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                if not drive_has_disc(self.device_path, wait_ready=True):
                    eject_drive(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        f"\nPlease insert an Audio CD into {self.device_path} and press [Enter] to continue...",
                    ):
                        self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    continue

                unmount_cdda_device(self.device_path)

                for item in self.stage_dir.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)

                self.ui.on_log(LogLevel.INFO, f"Starting extraction on {self.device_path}...")
                rip_res = runner.run_rip(
                    stage_dir=self.stage_dir,
                    offset=effective_offset,
                    release_id=self.release_id,
                    keep_going=keep_going,
                    unknown_disc=unknown_disc,
                    cdr=cdr_enabled,
                    cancel_event=self.cancel_event,
                )

                if rip_res.cancelled or (self.cancel_event and self.cancel_event.is_set()):
                    self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                if rip_res.returncode == 0:
                    self.ui.on_log(LogLevel.OK, "Extraction completed successfully!")
                    break

                if rip_res.no_cd:
                    drives_with_hw = get_drives_with_hardware_ids()
                    hw_id = get_drive_hardware_id(self.device_path)
                    prompt_res = self.ui.prompt_insert_disc(
                        self.device_path,
                        hw_id,
                        available_drives=drives_with_hw if len(drives_with_hw) > 1 else None,
                    )
                    if prompt_res.action == DiscPromptAction.ABORT:
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    if prompt_res.action == DiscPromptAction.SWITCH_DRIVE and prompt_res.selected_device:
                        self.device_path = prompt_res.selected_device
                        runner.target_device = self.device_path
                        effective_offset = get_stored_drive_offset(self.device_path)
                        continue

                    eject_drive(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        f"Please insert an Audio CD into {self.device_path} and press [Enter] to continue...",
                    ):
                        self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    continue

                if rip_res.offset_unconfigured:
                    self.ui.on_log(LogLevel.WARN, "Drive read offset is not configured in Whipper.")
                    new_offset = calibrate_drive(self.device_path, ui=self.ui)
                    if new_offset is not None:
                        effective_offset = new_offset
                    continue

                if rip_res.is_cdr and not cdr_enabled:
                    if self.ui.prompt_cdr_detected(self.device_path):
                        cdr_enabled = True
                        continue
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                if rip_res.metadata_missing and not unknown_disc:
                    if self.ui.prompt_unknown_disc():
                        unknown_disc = True
                        continue
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                drives_with_hw = get_drives_with_hardware_ids()
                err_choice = self.ui.prompt_extraction_error(self.device_path, drives_with_hw)
                if not err_choice or not getattr(err_choice, "action", None):
                    err_choice = ExtractionErrorChoice(ExtractionErrorAction.ABORT)

                if err_choice.action == ExtractionErrorAction.RETRY_SLOW_4X:
                    self.speed = 4
                    runner.speed = 4
                elif err_choice.action == ExtractionErrorAction.RETRY_MODERATE_8X:
                    self.speed = 8
                    runner.speed = 8
                elif err_choice.action == ExtractionErrorAction.EJECT_AND_RETRY:
                    eject_drive(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        "\nClean disc, reload tray, and press [Enter]...",
                    ):
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                elif err_choice.action == ExtractionErrorAction.SWITCH_DRIVE:
                    eject_drive(self.device_path)
                    if err_choice.selected_device:
                        self.device_path = err_choice.selected_device
                        runner.target_device = self.device_path
                        effective_offset = get_stored_drive_offset(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        f"\nInsert CD into {self.device_path} and press [Enter]...",
                    ):
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                elif err_choice.action == ExtractionErrorAction.KEEP_GOING:
                    keep_going = True
                elif err_choice.action == ExtractionErrorAction.ACCEPT_PARTIAL:
                    failed = True
                    break
                else:
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
        except KeyboardInterrupt:
            runner.cleanup_wrapper()
            release_drive_locks(self.device_path)
            raise
        finally:
            runner.cleanup_wrapper()
            if self.cancel_event and self.cancel_event.is_set():
                release_drive_locks(self.device_path)

        eject_drive(self.device_path)

        return ExtractionResult(
            success=True,
            partial=failed,
            device_path=self.device_path,
            effective_offset=effective_offset,
            speed=self.speed,
        )
