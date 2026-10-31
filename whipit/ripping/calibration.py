"""Drive calibration wizard to determine AccurateRip read offset via UI interaction port."""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

from whipit.config import (
    CDS_DRIVE_NOT_READY,
    close_tray,
    disable_usb_autosuspend,
    drive_has_disc,
    eject_drive,
    get_disc_status,
    get_stored_drive_offset,
    get_whipper_defeats_cache,
    get_whipper_drive_offset,
    is_usb_drive,
    release_drive_locks,
    repair_whipper_conf,
    store_drive_offset,
    store_whipper_defeats_cache,
    wait_for_disc_ready,
)
from whipit.inhibit import unmount_cdda_device
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    LogLevel,
    NullInteractionAdapter,
    UserInteractionPort,
)


class CalibrationStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    ABORTED = "aborted"


@dataclass
class CalibrationResult:
    status: CalibrationStatus
    offset: Optional[int] = None

    @property
    def success(self) -> bool:
        return self.status == CalibrationStatus.SUCCESS


def _terminate_process_group(proc: subprocess.Popen, timeout: float = 1.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
    except (ProcessLookupError, OSError):
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait()
            except OSError:
                pass


def _run_whipper_streaming(
    cmd: List[str],
    ui: UserInteractionPort,
    cancel_event: Optional[threading.Event] = None,
    dev_path: Optional[str] = None,
) -> int:
    """Executes a whipper command in a new process group, streaming output asynchronously with non-blocking cancel."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )

    line_queue: queue.Queue[Optional[str]] = queue.Queue()

    def _reader() -> None:
        if not proc.stdout:
            line_queue.put(None)
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                line_queue.put(line)
        except Exception:
            pass
        finally:
            line_queue.put(None)
            try:
                proc.stdout.close()
            except Exception:
                pass

    threading.Thread(target=_reader, daemon=True).start()

    while True:
        if cancel_event and cancel_event.is_set():
            _terminate_process_group(proc)
            if dev_path:
                release_drive_locks(dev_path)
            return -1

        try:
            item = line_queue.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None:
                while not line_queue.empty():
                    leftover = line_queue.get_nowait()
                    if leftover:
                        ui.on_whipper_output(leftover)
                break
            continue

        if item is None:
            break

        ui.on_whipper_output(item)

    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc, timeout=0.5)

    return proc.returncode


def calibrate_drive(
    dev: str = "/dev/cdrom",
    ui: Optional[UserInteractionPort] = None,
    cancel_event: Optional[threading.Event] = None,
) -> CalibrationResult:
    """Runs drive calibration workflow to determine AccurateRip offset."""
    if ui is None:
        ui = NullInteractionAdapter()

    if cancel_event and cancel_event.is_set():
        return CalibrationResult(CalibrationStatus.ABORTED)

    initial = ui.prompt_calibration_start(dev)
    if initial is None or initial.action == CalibrationAction.ABORT:
        return CalibrationResult(CalibrationStatus.ABORTED)

    if initial.action == CalibrationAction.SKIP:
        return CalibrationResult(CalibrationStatus.SKIPPED)

    if initial.action == CalibrationAction.USE_ZERO:
        store_drive_offset(dev, 0)
        ui.on_log(LogLevel.WARN, f"Using 0 as read offset fallback for {dev}.")
        return CalibrationResult(CalibrationStatus.SUCCESS, offset=0)

    if initial.action == CalibrationAction.MANUAL_OFFSET and initial.manual_offset is not None:
        store_drive_offset(dev, initial.manual_offset)
        ui.on_log(LogLevel.OK, f"Stored read offset {initial.manual_offset} for {dev}.")
        return CalibrationResult(CalibrationStatus.SUCCESS, offset=initial.manual_offset)

    if cancel_event and cancel_event.is_set():
        return CalibrationResult(CalibrationStatus.ABORTED)

    real_dev = str(Path(dev).resolve()) if Path(dev).exists() else dev

    repair_whipper_conf(real_dev)
    release_drive_locks(real_dev)
    unmount_cdda_device(real_dev)
    if is_usb_drive(real_dev):
        store_whipper_defeats_cache(real_dev, False)
        disable_usb_autosuspend(real_dev)

    if ui.is_interactive() and not drive_has_disc(real_dev, wait_ready=False):
        drive_st, _ = get_disc_status(real_dev)
        if drive_st == CDS_DRIVE_NOT_READY:
            ui.on_log(LogLevel.INFO, f"Waiting for optical disc in {real_dev} to spin up...")
            wait_for_disc_ready(real_dev, timeout=6.0, ui=ui)

        if not drive_has_disc(real_dev, wait_ready=False):
            ui.on_log(LogLevel.INFO, f"No Audio CD detected in {real_dev}. Opening tray...")
            eject_drive(real_dev)
            if not ui.wait_for_user_reload(
                real_dev,
                f"Please insert a commercial Audio CD into {real_dev} and click Continue...",
            ):
                return CalibrationResult(CalibrationStatus.ABORTED)
            close_tray(real_dev)
            ui.on_log(LogLevel.INFO, f"Waiting for optical drive {real_dev} to spin up...")
            if not wait_for_disc_ready(real_dev, timeout=15.0, ui=ui):
                ui.on_log(LogLevel.WARN, f"No Audio CD detected in {real_dev} after reload.")
                return CalibrationResult(CalibrationStatus.ABORTED)

    toc_attempts = 0
    while True:
        if cancel_event and cancel_event.is_set():
            return CalibrationResult(CalibrationStatus.ABORTED)

        repair_whipper_conf(real_dev)
        drive_st, _ = get_disc_status(real_dev)
        if drive_st == CDS_DRIVE_NOT_READY:
            ui.on_log(LogLevel.INFO, f"Waiting for drive {real_dev} to finish spinning up...")
            wait_for_disc_ready(real_dev, timeout=6.0, ui=ui)

        ui.on_log(LogLevel.INFO, "Searching for read offset in AccurateRip database...")
        detected_offset_from_log: Optional[int] = None
        whipper_error_reason: Optional[str] = None
        toc_read_error: bool = False
        is_404: bool = False
        no_match: bool = False
        cdparanoia_err: bool = False

        class _OffsetSniffer:
            def __init__(self, target_ui: UserInteractionPort):
                self._ui = target_ui

            def is_interactive(self) -> bool:
                return self._ui.is_interactive()

            def on_log(self, level: LogLevel, message: str) -> None:
                self._ui.on_log(level, message)

            def on_state_changed(self, state, manifest) -> None:
                self._ui.on_state_changed(state, manifest)

            def on_whipper_output(self, line: str) -> None:
                nonlocal detected_offset_from_log, whipper_error_reason, toc_read_error, is_404, no_match, cdparanoia_err
                m = re.search(r"Read offset of device is:\s*([+-]?\d+)", line)
                if not m:
                    m = re.search(r"offset of device is likely\s*([+-]?\d+)", line)
                if m:
                    try:
                        detected_offset_from_log = int(m.group(1))
                    except ValueError:
                        pass

                clean_line = line.strip()
                if re.search(r"cdrdao\.read-toc.*FileNotFoundError|FileNotFoundError.*cdrdao\.read-toc|Cannot read disk toc", clean_line, re.I):
                    toc_read_error = True
                    whipper_error_reason = "Optical drive was spinning down when reading Table of Contents."
                elif re.search(r"error retrieving AccurateRip entry:\s*404|not in AccurateRip database", clean_line, re.I):
                    is_404 = True
                    whipper_error_reason = "Disc not found in AccurateRip database (404 Not Found)."
                elif re.search(r"no matching offset found", clean_line, re.I):
                    no_match = True
                    whipper_error_reason = "Disc found in AccurateRip, but no tested read offsets matched the track checksums."
                elif re.search(r"cdparanoia.*(?:file size 0|couldn't read any frames)", clean_line, re.I):
                    cdparanoia_err = True
                    whipper_error_reason = "cd-paranoia read error: unable to read audio frames cleanly."
                elif re.search(r"^ERROR:|^CRITICAL:|^Traceback", clean_line) and not whipper_error_reason:
                    whipper_error_reason = clean_line[:120]

                self._ui.on_whipper_output(line)

            def __getattr__(self, name: str):
                return getattr(self._ui, name)

        sniffer = _OffsetSniffer(ui)
        rc = _run_whipper_streaming(["whipper", "offset", "find", "-d", real_dev], sniffer, cancel_event, dev_path=real_dev)

        if toc_read_error and toc_attempts < 2:
            toc_attempts += 1
            ui.on_log(LogLevel.WARN, "Optical drive was still spinning up when reading TOC; settling and retrying...")
            release_drive_locks(real_dev)
            time.sleep(2.0)
            wait_for_disc_ready(real_dev, timeout=15.0, ui=ui)
            continue

        if rc == 0 or detected_offset_from_log is not None:
            detected_offset = detected_offset_from_log
            if detected_offset is None:
                detected_offset = get_whipper_drive_offset(real_dev)
            if detected_offset is None:
                detected_offset = get_stored_drive_offset(real_dev)

            if detected_offset is not None:
                store_drive_offset(real_dev, detected_offset)
                ui.on_log(LogLevel.OK, f"Offset {detected_offset:+d} detected and saved successfully!")
                return CalibrationResult(CalibrationStatus.SUCCESS, offset=detected_offset)
            else:
                ui.on_log(LogLevel.OK, "Offset detected successfully!")
                return CalibrationResult(CalibrationStatus.SUCCESS, offset=None)

        if cancel_event and cancel_event.is_set():
            return CalibrationResult(CalibrationStatus.ABORTED)

        try:
            choice = ui.prompt_calibration_retry(real_dev, error_reason=whipper_error_reason)
        except TypeError:
            choice = ui.prompt_calibration_retry(real_dev)
        if choice.action == CalibrationAction.MANUAL_OFFSET and choice.manual_offset is not None:
            store_drive_offset(real_dev, choice.manual_offset)
            ui.on_log(LogLevel.OK, f"Stored read offset {choice.manual_offset} for {real_dev}.")
            return CalibrationResult(CalibrationStatus.SUCCESS, offset=choice.manual_offset)
        if choice.action == CalibrationAction.USE_ZERO:
            store_drive_offset(real_dev, 0)
            ui.on_log(LogLevel.WARN, f"Using 0 as session fallback for {real_dev}.")
            return CalibrationResult(CalibrationStatus.SUCCESS, offset=0)
        if choice.action == CalibrationAction.SWAP_DISC:
            eject_drive(real_dev)
            if not ui.wait_for_user_reload(real_dev, f"\nInsert another commercial Audio CD into {real_dev} and click Continue..."):
                return CalibrationResult(CalibrationStatus.ABORTED)
            close_tray(real_dev)
            ui.on_log(LogLevel.INFO, f"Waiting for optical drive {real_dev} to spin up...")
            time.sleep(2.0)
            release_drive_locks(real_dev)
            unmount_cdda_device(real_dev)
            wait_for_disc_ready(real_dev, timeout=20.0, ui=ui)
            time.sleep(1.0)
            unmount_cdda_device(real_dev)
        elif choice.action == CalibrationAction.SKIP:
            return CalibrationResult(CalibrationStatus.SKIPPED)
        else:
            return CalibrationResult(CalibrationStatus.ABORTED)
