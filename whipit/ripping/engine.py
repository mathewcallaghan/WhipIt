"""Optical disc extraction engine and recovery loop decoupled via UserInteractionPort."""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from whipit.config import (
    close_tray,
    disable_usb_autosuspend,
    drive_has_disc,
    eject_drive,
    format_time_span,
    get_drive_hardware_id,
    get_drives_with_hardware_ids,
    get_drive_max_speed,
    get_stored_drive_offset,
    is_usb_drive,
    release_drive_locks,
    reset_optical_drive,
    store_whipper_defeats_cache,
    unlock_drive,
)
from whipit.inhibit import unmount_cdda_device
from whipit.ripping.calibration import CalibrationResult, CalibrationStatus, calibrate_drive
from whipit.ripping.whipper import WhipperRunner, WhipperStreamParser
from whipit.ui.base import (
    DiscPromptAction,
    DiscPromptResult,
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
    cancelled: bool = False
    elapsed_seconds: Optional[float] = None
    average_speed_x: Optional[float] = None


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
        full_log_path: Optional[Path] = None,
    ):
        self.cache_dir = cache_dir
        self.stage_dir = stage_dir
        self.device_path = device_path
        self.speed = speed
        self.offset = offset
        self.release_id = release_id
        self.ui = ui or NullInteractionAdapter()
        self.cancel_event = cancel_event
        self.full_log_path = full_log_path
        self.runner: Optional[WhipperRunner] = None

    def cancel(self) -> None:
        if self.runner:
            self.runner.cancel()
        release_drive_locks(self.device_path)

    def _handle_insert_disc_prompt(
        self, runner: WhipperRunner, effective_offset: Optional[int]
    ) -> Tuple[bool, Optional[int]]:
        """
        Presents the prompt_insert_disc dialog and executes the chosen user action distinctly.
        Returns: (should_continue, effective_offset)
        """
        drives_with_hw = get_drives_with_hardware_ids()
        hw_id = get_drive_hardware_id(self.device_path)
        prompt_res = self.ui.prompt_insert_disc(
            self.device_path,
            hw_id,
            available_drives=drives_with_hw if len(drives_with_hw) > 1 else None,
        )

        if not prompt_res or not getattr(prompt_res, "action", None):
            prompt_res = DiscPromptResult(DiscPromptAction.ABORT)

        if prompt_res.action in (DiscPromptAction.ABORT, DiscPromptAction.MAIN_MENU):
            self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
            return False, effective_offset

        if prompt_res.action == DiscPromptAction.SWITCH_DRIVE and prompt_res.selected_device:
            self.device_path = prompt_res.selected_device
            runner.target_device = self.device_path
            effective_offset = get_stored_drive_offset(self.device_path)
            return True, effective_offset

        if prompt_res.action == DiscPromptAction.RETRY:
            close_tray(self.device_path)
            return True, effective_offset

        if prompt_res.action == DiscPromptAction.EJECT:
            eject_drive(self.device_path)
            if not self.ui.wait_for_user_reload(
                self.device_path,
                f"Please insert an Audio CD into {self.device_path} and press [Enter] to continue...",
            ):
                self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
                return False, effective_offset
            close_tray(self.device_path)
            return True, effective_offset

        return False, effective_offset

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
            event_callback=self.ui.on_whipper_event,
            full_log_path=self.full_log_path,
        )
        self.runner = runner
        effective_offset = self.offset
        failed = False
        cdr_enabled = False
        rip_start_time = time.monotonic()

        try:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                runner.parser = WhipperStreamParser()

                if not drive_has_disc(self.device_path, wait_ready=True):
                    should_continue, effective_offset = self._handle_insert_disc_prompt(runner, effective_offset)
                    if not should_continue:
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    continue

                unmount_cdda_device(self.device_path)

                if is_usb_drive(self.device_path):
                    # Pre-emptively disable cache defeat on USB drives to prevent firmware lockup
                    store_whipper_defeats_cache(self.device_path, False)
                    disable_usb_autosuspend(self.device_path)

                if not keep_going and not failed:
                    for item in self.stage_dir.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                else:
                    for wav in self.stage_dir.glob("*.wav"):
                        wav.unlink(missing_ok=True)

                max_hw = get_drive_max_speed(self.device_path)
                max_hw_str = f" [Drive Max: {max_hw}x]" if max_hw else ""
                spd_str = f"capped at {self.speed}x" if self.speed else "maximum drive speed"
                self.ui.on_log(LogLevel.INFO, f"Starting extraction on {self.device_path}{max_hw_str} ({spd_str})...")
                if self.release_id:
                    self.ui.on_log(LogLevel.INFO, f"Locked to MusicBrainz release ID: {self.release_id}")
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
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                if rip_res.returncode == 0:
                    elapsed = time.monotonic() - rip_start_time
                    self.ui.on_log(LogLevel.OK, f"Extraction completed successfully in {format_time_span(elapsed)}!")
                    break

                if rip_res.no_cd:
                    should_continue, effective_offset = self._handle_insert_disc_prompt(runner, effective_offset)
                    if not should_continue:
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    continue

                if rip_res.offset_unconfigured:
                    self.ui.on_log(LogLevel.WARN, "Drive read offset is not configured in Whipper.")
                    cal_res = calibrate_drive(self.device_path, ui=self.ui, cancel_event=self.cancel_event)
                    if cal_res.status == CalibrationStatus.ABORTED:
                        self.ui.on_log(LogLevel.WARN, "Drive calibration aborted.")
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                    if cal_res.status == CalibrationStatus.SKIPPED:
                        self.ui.on_log(LogLevel.WARN, "Calibration skipped. Extraction cannot proceed without a drive read offset.")
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                    if cal_res.offset is not None:
                        effective_offset = cal_res.offset
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

                if rip_res.stalled:
                    self.ui.on_log(LogLevel.WARN, "Optical drive firmware deadlocked; resetting drive bus...")
                    store_whipper_defeats_cache(self.device_path, False)
                    release_drive_locks(self.device_path)
                    reset_optical_drive(self.device_path)
                    self.speed = 4
                    runner.speed = 4
                    time.sleep(1.5)

                drives_with_hw = get_drives_with_hardware_ids()
                err_choice = self.ui.prompt_extraction_error(self.device_path, drives_with_hw)
                if not err_choice or not getattr(err_choice, "action", None):
                    err_choice = ExtractionErrorChoice(ExtractionErrorAction.ABORT)

                if err_choice.action == ExtractionErrorAction.RETRY_SLOW_4X:
                    self.ui.on_log(LogLevel.HEADER, "Retrying album extraction at slow speed (4x)...")
                    self.speed = 4
                    runner.speed = 4
                    release_drive_locks(self.device_path)
                    unlock_drive(self.device_path)
                    time.sleep(1.0)
                elif err_choice.action == ExtractionErrorAction.RETRY_MODERATE_8X:
                    self.ui.on_log(LogLevel.HEADER, "Retrying album extraction at moderate speed (8x)...")
                    self.speed = 8
                    runner.speed = 8
                    release_drive_locks(self.device_path)
                    unlock_drive(self.device_path)
                    time.sleep(1.0)
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
                    self.ui.on_log(LogLevel.INFO, "Resuming with --keep-going to rescue remaining readable tracks...")
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
            elapsed_seconds=time.monotonic() - rip_start_time,
        )
