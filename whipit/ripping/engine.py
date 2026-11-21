"""Optical disc extraction engine and recovery loop decoupled via UserInteractionPort."""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from whipit.config import (
    close_tray,
    disable_usb_autosuspend,
    drive_has_disc,
    eject_drive,
    format_time_span,
    get_drive_hardware_id,
    get_drive_max_speed,
    get_drives_with_hardware_ids,
    get_stored_drive_offset,
    is_usb_drive,
    release_drive_locks,
    repair_whipper_conf,
    reset_optical_drive,
    store_whipper_defeats_cache,
    unlock_drive,
    wait_for_disc_ready,
)
from whipit.inhibit import unmount_cdda_device
from whipit.ripping.calibration import CalibrationStatus, calibrate_drive
from whipit.ripping.raw import RawSectorExtractor
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
    raw_fallback: bool = False


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
        auto_eject: bool = True,
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
        self.auto_eject = auto_eject
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
            old_device = self.device_path
            if prompt_res.selected_device != old_device:
                self.device_path = prompt_res.selected_device
                runner.target_device = self.device_path
                effective_offset = get_stored_drive_offset(self.device_path)
                self.ui.on_active_device_changed(old_device, self.device_path)
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
            time.sleep(0.5)
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
        initial_speed = self.speed
        initial_keep_going = keep_going
        effective_offset = self.offset
        failed = False
        raw_fallback_used = False
        cdr_enabled = False
        cdr_declined = False
        unknown_declined = False
        toc_attempts = 0
        offset_attempts = 0
        skip_tracks: Set[int] = set()
        stalled_devices: Set[str] = set()
        last_stalled_track: Optional[int] = None
        rip_start_time = time.monotonic()

        try:
            while True:
                if self.cancel_event and self.cancel_event.is_set():
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                runner.parser = WhipperStreamParser(max_tries=getattr(runner, "max_retries", 10))

                if not drive_has_disc(self.device_path, wait_ready=True):
                    prev_device = self.device_path
                    should_continue, effective_offset = self._handle_insert_disc_prompt(runner, effective_offset)
                    if not should_continue:
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)
                    if self.device_path != prev_device:
                        self.speed = initial_speed
                        runner.speed = initial_speed
                        toc_attempts = 0
                        offset_attempts = 0
                        skip_tracks.clear()
                        last_stalled_track = None
                        cdr_enabled = False
                        cdr_declined = False
                        unknown_declined = False
                        keep_going = False
                        failed = False
                    continue

                unmount_cdda_device(self.device_path)

                if keep_going:
                    capped_speed = min(self.speed, 4) if self.speed else 4
                    if self.speed != capped_speed:
                        self.ui.on_log(
                            LogLevel.INFO,
                            f"Defensive --keep-going mode: capping read speed to {capped_speed}x on {self.device_path}.",
                        )
                    self.speed = capped_speed
                    runner.speed = capped_speed

                if keep_going or is_usb_drive(self.device_path) or self.device_path in stalled_devices:
                    # Disable cache defeat on keep-going, USB, and stalled drives to prevent firmware lockup
                    store_whipper_defeats_cache(self.device_path, False)
                    disable_usb_autosuspend(self.device_path)

                self.stage_dir.mkdir(parents=True, exist_ok=True)
                if not keep_going and not failed:
                    for item in self.stage_dir.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                else:
                    for wav in self.stage_dir.rglob("*.wav"):
                        wav.unlink(missing_ok=True)
                    for flac in self.stage_dir.rglob("*.flac"):
                        try:
                            if flac.stat().st_size == 0:
                                flac.unlink(missing_ok=True)
                        except OSError:
                            pass

                max_hw = get_drive_max_speed(self.device_path)
                max_hw_str = f" [Drive Max: {max_hw}x]" if max_hw else ""
                spd_str = f"capped at {self.speed}x" if self.speed else "maximum drive speed"
                self.ui.on_log(LogLevel.INFO, f"Starting extraction on {self.device_path}{max_hw_str} ({spd_str})...")
                if self.release_id:
                    self.ui.on_log(LogLevel.INFO, f"Locked to MusicBrainz release ID: {self.release_id}")
                repair_whipper_conf(self.device_path)
                rip_kwargs: Dict[str, Any] = {}
                if skip_tracks:
                    rip_kwargs["skip_tracks"] = set(skip_tracks)
                rip_res = runner.run_rip(
                    stage_dir=self.stage_dir,
                    offset=effective_offset,
                    release_id=self.release_id,
                    keep_going=keep_going,
                    unknown_disc=unknown_disc,
                    cdr=cdr_enabled,
                    cancel_event=self.cancel_event,
                    **rip_kwargs,
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

                if rip_res.toc_read_error and toc_attempts < 2:
                    toc_attempts += 1
                    self.ui.on_log(LogLevel.WARN, "Optical drive was still spinning up when reading TOC; settling and retrying...")
                    release_drive_locks(self.device_path)
                    time.sleep(2.0)
                    wait_for_disc_ready(self.device_path, timeout=15.0, ui=self.ui)
                    time.sleep(1.0)
                    continue

                if rip_res.offset_unconfigured and offset_attempts < 2:
                    offset_attempts += 1
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

                if rip_res.is_cdr and not cdr_enabled and not cdr_declined:
                    if self.ui.prompt_cdr_detected(self.device_path):
                        cdr_enabled = True
                        continue
                    cdr_declined = True
                    self.ui.on_log(
                        LogLevel.WARN,
                        "User indicated disc is not a CD-R; opening error recovery options...",
                    )

                if rip_res.metadata_missing and not unknown_disc and not unknown_declined:
                    if self.ui.prompt_unknown_disc():
                        unknown_disc = True
                        continue
                    unknown_declined = True

                if rip_res.stalled:
                    self.ui.on_log(
                        LogLevel.WARN,
                        "Optical drive firmware deadlocked; disabling cache defeat and resetting drive bus...",
                    )
                    stalled_devices.add(self.device_path)
                    stalled_tr = getattr(rip_res, "stalled_track", None) or 1
                    last_stalled_track = stalled_tr
                    store_whipper_defeats_cache(self.device_path, False)
                    disable_usb_autosuspend(self.device_path)
                    release_drive_locks(self.device_path)
                    reset_optical_drive(self.device_path)
                    capped_speed = min(self.speed, 4) if self.speed else 4
                    if self.speed != capped_speed:
                        self.ui.on_log(
                            LogLevel.INFO,
                            f"Capping read speed to {capped_speed}x on {self.device_path} to prevent repeat lockups.",
                        )
                    self.speed = capped_speed
                    runner.speed = capped_speed
                    time.sleep(1.5)

                    if self.cancel_event and self.cancel_event.is_set():
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                    if initial_keep_going and stalled_tr not in skip_tracks:
                        skip_tracks.add(stalled_tr)
                        self.ui.on_log(
                            LogLevel.WARN,
                            f"Blacklisting stalled track {stalled_tr} and resuming with --keep-going...",
                        )
                        last_stalled_track = None
                        continue
                else:
                    last_stalled_track = None

                if self.cancel_event and self.cancel_event.is_set():
                    runner.cleanup_wrapper()
                    release_drive_locks(self.device_path)
                    return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)

                has_partial_tracks = bool(self.stage_dir.exists() and any(self.stage_dir.rglob("*.flac")))
                drives_with_hw = get_drives_with_hardware_ids()
                err_reason = getattr(rip_res, "error_reason", None)
                is_stall = bool(getattr(rip_res, "stalled", False))
                stalled_tr_arg = last_stalled_track if is_stall else None
                try:
                    err_choice = self.ui.prompt_extraction_error(
                        self.device_path,
                        drives_with_hw,
                        error_reason=err_reason,
                        has_partial_tracks=has_partial_tracks,
                        is_hardware_stall=is_stall,
                        stalled_track=stalled_tr_arg,
                    )
                except TypeError:
                    try:
                        err_choice = self.ui.prompt_extraction_error(
                            self.device_path,
                            drives_with_hw,
                            error_reason=err_reason,
                            has_partial_tracks=has_partial_tracks,
                            is_hardware_stall=is_stall,
                        )
                    except TypeError:
                        try:
                            err_choice = self.ui.prompt_extraction_error(
                                self.device_path,
                                drives_with_hw,
                                error_reason=err_reason,
                                has_partial_tracks=has_partial_tracks,
                            )
                        except TypeError:
                            err_choice = self.ui.prompt_extraction_error(self.device_path, drives_with_hw)
                if not err_choice or not getattr(err_choice, "action", None):
                    err_choice = ExtractionErrorChoice(ExtractionErrorAction.ABORT)

                if err_choice.action == ExtractionErrorAction.RETRY_SLOW_4X:
                    last_stalled_track = None
                    self.ui.on_log(LogLevel.HEADER, "Retrying album extraction at slow speed (4x)...")
                    self.speed = 4
                    runner.speed = 4
                    release_drive_locks(self.device_path)
                    unlock_drive(self.device_path)
                    close_tray(self.device_path)
                    time.sleep(0.5)
                elif err_choice.action == ExtractionErrorAction.RETRY_MODERATE_8X:
                    last_stalled_track = None
                    self.ui.on_log(LogLevel.HEADER, "Retrying album extraction at moderate speed (8x)...")
                    self.speed = 8
                    runner.speed = 8
                    release_drive_locks(self.device_path)
                    unlock_drive(self.device_path)
                    close_tray(self.device_path)
                    time.sleep(0.5)
                elif err_choice.action == ExtractionErrorAction.EJECT_AND_RETRY:
                    last_stalled_track = None
                    toc_attempts = 0
                    cdr_declined = False
                    eject_drive(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        f"\nClean disc, reload tray for {self.device_path}, and press [Enter] / Continue...",
                    ):
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)
                    close_tray(self.device_path)
                    wait_for_disc_ready(self.device_path, timeout=15.0, ui=self.ui)
                    release_drive_locks(self.device_path)
                    unmount_cdda_device(self.device_path)
                    time.sleep(1.5)
                elif err_choice.action == ExtractionErrorAction.SWITCH_DRIVE:
                    target_dev = err_choice.selected_device
                    if not target_dev or target_dev == self.device_path:
                        self.ui.on_log(
                            LogLevel.WARN,
                            f"Cannot switch to the same optical drive ({self.device_path}); aborting switch.",
                        )
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed)

                    old_device = self.device_path
                    self.device_path = target_dev
                    runner.target_device = self.device_path
                    self.speed = initial_speed
                    runner.speed = initial_speed
                    effective_offset = get_stored_drive_offset(self.device_path)
                    toc_attempts = 0
                    offset_attempts = 0
                    skip_tracks.clear()
                    last_stalled_track = None
                    cdr_enabled = False
                    cdr_declined = False
                    unknown_declined = False
                    keep_going = False
                    initial_keep_going = False
                    failed = False
                    if self.stage_dir.exists():
                        for item in self.stage_dir.iterdir():
                            if item.name == "whipper_exec.log":
                                continue
                            if item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                            else:
                                item.unlink(missing_ok=True)
                    self.ui.on_active_device_changed(old_device, self.device_path)
                    self.ui.on_log(LogLevel.HEADER, f"Switching optical drive from {old_device} to {self.device_path}...")

                    eject_drive(old_device)
                    eject_drive(self.device_path)
                    if not self.ui.wait_for_user_reload(
                        self.device_path,
                        f"\nInsert CD into {self.device_path} and press [Enter] / Continue...",
                    ):
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(False, False, self.device_path, effective_offset, self.speed, cancelled=True)
                    close_tray(self.device_path)
                    ready = wait_for_disc_ready(self.device_path, timeout=15.0, ui=self.ui)
                    if not ready:
                        self.ui.on_log(
                            LogLevel.WARN,
                            f"Optical drive {self.device_path} is not ready yet; verifying disc...",
                        )
                    release_drive_locks(self.device_path)
                    unmount_cdda_device(self.device_path)
                    repair_whipper_conf(self.device_path)
                    time.sleep(1.5)
                elif err_choice.action == ExtractionErrorAction.KEEP_GOING:
                    if last_stalled_track is not None:
                        skip_tracks.add(last_stalled_track)
                        self.ui.on_log(
                            LogLevel.WARN,
                            f"Blacklisting stalled track {last_stalled_track} and resuming with --keep-going...",
                        )
                        last_stalled_track = None
                    elif rip_res.stalled:
                        self.ui.on_log(
                            LogLevel.WARN,
                            "Drive stalled before track extraction began; resuming with --keep-going...",
                        )
                    else:
                        self.ui.on_log(LogLevel.INFO, "Resuming with --keep-going to rescue remaining readable tracks...")
                    keep_going = True
                elif err_choice.action == ExtractionErrorAction.RAW_SECTOR_FALLBACK:
                    self.ui.on_log(
                        LogLevel.HEADER,
                        f"Starting emergency raw sector extraction on {self.device_path} (cdrdao --paranoia-mode 0)...",
                    )
                    if self.stage_dir.exists():
                        for item in self.stage_dir.iterdir():
                            if item.name == "whipper_exec.log":
                                continue
                            if item.is_dir():
                                shutil.rmtree(item, ignore_errors=True)
                            else:
                                item.unlink(missing_ok=True)
                    unmount_cdda_device(self.device_path)
                    raw_extractor = RawSectorExtractor(
                        device_path=self.device_path,
                        speed=self.speed,
                        output_callback=self.ui.on_whipper_output,
                    )
                    raw_res = raw_extractor.run_raw_extraction(
                        stage_dir=self.stage_dir,
                        cancel_event=self.cancel_event,
                    )
                    if raw_res.cancelled or (self.cancel_event and self.cancel_event.is_set()):
                        runner.cleanup_wrapper()
                        release_drive_locks(self.device_path)
                        return ExtractionResult(
                            False, False, self.device_path, effective_offset, self.speed, cancelled=True
                        )
                    if raw_res.success:
                        self.ui.on_log(
                            LogLevel.WARN,
                            "Raw sector recovery completed (unverified; tracks tagged with WARNING).",
                        )
                        raw_fallback_used = True
                        failed = False
                        break
                    self.ui.on_log(
                        LogLevel.ERROR,
                        raw_res.error_message or "Emergency raw sector fallback failed.",
                    )
                    if raw_res.stalled:
                        stalled_devices.add(self.device_path)
                        store_whipper_defeats_cache(self.device_path, False)
                        release_drive_locks(self.device_path)
                        reset_optical_drive(self.device_path)
                        time.sleep(1.5)
                    rip_res.stalled = raw_res.stalled
                    rip_res.error_reason = raw_res.error_message
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

        if self.auto_eject:
            eject_drive(self.device_path)

        return ExtractionResult(
            success=True,
            partial=failed,
            device_path=self.device_path,
            effective_offset=effective_offset,
            speed=self.speed,
            elapsed_seconds=time.monotonic() - rip_start_time,
            raw_fallback=raw_fallback_used,
        )
