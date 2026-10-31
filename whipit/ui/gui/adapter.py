"""Qt signal-based implementation of UserInteractionPort with thread-safe modal prompt bridging."""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from whipit.models import JobManifest, ReleaseOption, WorkflowState
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
    UserInteractionPort,
)

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_TIMEOUT = 120.0


class PromptRequest:
    def __init__(self, prompt_type: str, payload: Any = None):
        self.prompt_type = prompt_type
        self.payload = payload
        self.reply_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._resolved = threading.Event()
        self._lock = threading.Lock()

    def resolve(self, result: Any) -> bool:
        with self._lock:
            if self._resolved.is_set():
                return False
            self._resolved.set()
            try:
                self.reply_queue.put_nowait(result)
            except (queue.Full, Exception):
                pass
            return True

    @property
    def is_resolved(self) -> bool:
        return self._resolved.is_set()


class GuiSignaler(QObject):
    log_emitted = pyqtSignal(object, str)
    state_changed = pyqtSignal(object, object)
    whipper_output = pyqtSignal(str)
    whipper_event = pyqtSignal(object)
    transcode_progress = pyqtSignal(int, int, str, bool)
    disc_spinup_poll = pyqtSignal(str, str, float, object)
    prompt_requested = pyqtSignal(object)


class GuiInteractionAdapter(UserInteractionPort):
    def __init__(self) -> None:
        self.signaler = GuiSignaler()
        self._cover_art_path: Optional[Path] = None
        self._art_lock = threading.Lock()
        self._shutdown_evt = threading.Event()
        self._active_requests: List[PromptRequest] = []
        self._requests_lock = threading.Lock()

    def set_cover_art(self, path: Optional[Path]) -> None:
        with self._art_lock:
            self._cover_art_path = path

    def get_cover_art(self) -> Optional[Path]:
        with self._art_lock:
            return self._cover_art_path

    def is_interactive(self) -> bool:
        return True

    def shutdown(self) -> None:
        """Unblocks all worker threads waiting on UI modal prompts immediately with safe defaults."""
        self._shutdown_evt.set()
        with self._requests_lock:
            for req in self._active_requests:
                default_cancel = self._default_for_prompt(req.prompt_type)
                req.resolve(default_cancel)
            self._active_requests.clear()

    def _request_from_main_thread(
        self,
        prompt_type: str,
        payload: Any = None,
        timeout: float = DEFAULT_PROMPT_TIMEOUT,
    ) -> Any:
        default_cancel = self._default_for_prompt(prompt_type)
        if self._shutdown_evt.is_set():
            return default_cancel

        request = PromptRequest(prompt_type, payload)
        with self._requests_lock:
            self._active_requests.append(request)

        try:
            self.signaler.prompt_requested.emit(request)
            start_time = time.monotonic()
            while not self._shutdown_evt.is_set():
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    logger.warning("GUI prompt %s timed out after %.1fs", prompt_type, timeout)
                    request.resolve(default_cancel)
                    return default_cancel
                try:
                    res = request.reply_queue.get(timeout=min(0.15, max(0.01, remaining)))
                    return res if res is not None else default_cancel
                except queue.Empty:
                    continue
            request.resolve(default_cancel)
            return default_cancel
        finally:
            with self._requests_lock:
                if request in self._active_requests:
                    self._active_requests.remove(request)

    @staticmethod
    def _default_for_prompt(prompt_type: str) -> Any:
        if prompt_type in ("prompt_insert_disc", "prompt_empty_multi_drives"):
            return DiscPromptResult(DiscPromptAction.ABORT)
        if prompt_type in ("prompt_calibration_start", "prompt_calibration_retry"):
            return CalibrationChoice(CalibrationAction.ABORT)
        if prompt_type == "prompt_extraction_error":
            return ExtractionErrorChoice(ExtractionErrorAction.ABORT)
        if prompt_type == "prompt_conflict_resolution":
            return ConflictChoice.ABORT
        if prompt_type in (
            "confirm_install_dependencies",
            "wait_for_user_reload",
            "prompt_cdr_detected",
            "prompt_unknown_disc",
            "confirm_purge_stage",
        ):
            return False
        return None

    def on_log(self, level: LogLevel, message: str) -> None:
        self.signaler.log_emitted.emit(level, message)

    def on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        self.signaler.state_changed.emit(state, manifest)

    def on_whipper_output(self, line: str) -> None:
        self.signaler.whipper_output.emit(line)

    def on_whipper_event(self, event: Any) -> None:
        self.signaler.whipper_event.emit(event)

    def on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        self.signaler.transcode_progress.emit(completed, total, track_name, success)

    def on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        self.signaler.disc_spinup_poll.emit(device, hardware_id, elapsed, ready)

    def confirm_install_dependencies(self, missing_tools: List[str], install_cmd: str, manager: str) -> bool:
        return bool(
            self._request_from_main_thread(
                "confirm_install_dependencies",
                (missing_tools, install_cmd, manager),
            )
        )

    def select_loaded_drive(self, drives_with_hw: List[Tuple[str, str]]) -> Optional[str]:
        return self._request_from_main_thread("select_loaded_drive", drives_with_hw)

    def prompt_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        return self._request_from_main_thread("prompt_empty_multi_drives", drives_with_hw)

    def prompt_insert_disc(
        self,
        device: str,
        hardware_id: str,
        available_drives: Optional[List[Tuple[str, str]]] = None,
        allow_main_menu: bool = False,
    ) -> DiscPromptResult:
        return self._request_from_main_thread(
            "prompt_insert_disc",
            (device, hardware_id, available_drives, allow_main_menu),
        )

    def wait_for_user_reload(self, device: str, message: str) -> bool:
        return bool(self._request_from_main_thread("wait_for_user_reload", (device, message)))

    def prompt_extraction_speed(self) -> Optional[int]:
        return self._request_from_main_thread("prompt_extraction_speed", None)

    def prompt_select_release(self, releases: List[ReleaseOption]) -> Optional[str]:
        return self._request_from_main_thread("prompt_select_release", releases)

    def prompt_calibration_start(self, device: str) -> Optional[CalibrationChoice]:
        return self._request_from_main_thread("prompt_calibration_start", device)

    def prompt_calibration_retry(self, device: str, error_reason: Optional[str] = None) -> CalibrationChoice:
        return self._request_from_main_thread("prompt_calibration_retry", (device, error_reason))

    def prompt_cdr_detected(self, device: str) -> bool:
        return bool(self._request_from_main_thread("prompt_cdr_detected", device))

    def prompt_unknown_disc(self) -> bool:
        return bool(self._request_from_main_thread("prompt_unknown_disc", None))

    def prompt_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        return self._request_from_main_thread(
            "prompt_extraction_error",
            (device, available_drives),
        )

    def prompt_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
        suggested_suffix: Optional[str] = None,
    ) -> ConflictChoice:
        return self._request_from_main_thread(
            "prompt_conflict_resolution",
            (flac_dir, opus_dir, warning, suggested_suffix),
        )

    def confirm_purge_stage(self, stage_dir: Path, flac_count: int) -> bool:
        return bool(self._request_from_main_thread("confirm_purge_stage", (stage_dir, flac_count)))
