"""Drive calibration wizard to determine AccurateRip read offset via UI interaction port."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional
from whipit.config import eject_drive, get_stored_drive_offset, release_drive_locks, store_drive_offset
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

    if initial.action == CalibrationAction.MANUAL_OFFSET and initial.manual_offset is not None:
        store_drive_offset(dev, initial.manual_offset)
        ui.on_log(LogLevel.OK, f"Stored read offset {initial.manual_offset} for {dev}.")
        return CalibrationResult(CalibrationStatus.SUCCESS, offset=initial.manual_offset)

    if cancel_event and cancel_event.is_set():
        return CalibrationResult(CalibrationStatus.ABORTED)

    ui.on_log(LogLevel.INFO, "Analyzing drive cache capabilities with Whipper...")
    rc = _run_whipper_streaming(["whipper", "drive", "analyze", "-d", dev], ui, cancel_event, dev_path=dev)
    if rc != 0 and cancel_event and cancel_event.is_set():
        ui.on_log(LogLevel.WARN, "Drive analysis cancelled by user.")
        return CalibrationResult(CalibrationStatus.ABORTED)

    while True:
        if cancel_event and cancel_event.is_set():
            return CalibrationResult(CalibrationStatus.ABORTED)

        ui.on_log(LogLevel.INFO, "Searching for read offset in AccurateRip database...")
        rc = _run_whipper_streaming(["whipper", "offset", "find", "-d", dev], ui, cancel_event, dev_path=dev)
        if rc == 0:
            ui.on_log(LogLevel.OK, "Offset detected and saved by Whipper!")
            detected_offset = get_stored_drive_offset(dev)
            if detected_offset is not None:
                store_drive_offset(dev, detected_offset)
            return CalibrationResult(CalibrationStatus.SUCCESS, offset=detected_offset)

        if cancel_event and cancel_event.is_set():
            return CalibrationResult(CalibrationStatus.ABORTED)

        choice = ui.prompt_calibration_retry(dev)
        if choice.action == CalibrationAction.MANUAL_OFFSET and choice.manual_offset is not None:
            store_drive_offset(dev, choice.manual_offset)
            ui.on_log(LogLevel.OK, f"Stored read offset {choice.manual_offset} for {dev}.")
            return CalibrationResult(CalibrationStatus.SUCCESS, offset=choice.manual_offset)
        if choice.action == CalibrationAction.USE_ZERO:
            ui.on_log(LogLevel.WARN, "Using 0 as session fallback.")
            return CalibrationResult(CalibrationStatus.SUCCESS, offset=0)
        if choice.action == CalibrationAction.SWAP_DISC:
            eject_drive(dev)
            if not ui.wait_for_user_reload(dev, "\nInsert another commercial Audio CD and press [Enter]..."):
                return CalibrationResult(CalibrationStatus.ABORTED)
        elif choice.action == CalibrationAction.SKIP:
            return CalibrationResult(CalibrationStatus.SKIPPED)
        else:
            return CalibrationResult(CalibrationStatus.ABORTED)
