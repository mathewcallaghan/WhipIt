"""Qt signal-based implementation of UserInteractionPort with thread-safe modal prompt bridging."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from whipit.models import JobManifest, ReleaseOption, WorkflowState
from whipit.ui.base import (
    CalibrationChoice,
    ConflictChoice,
    DiscPromptResult,
    ExtractionErrorChoice,
    LogLevel,
    UserInteractionPort,
)


class GuiSignaler(QObject):
    log_emitted = pyqtSignal(object, str)
    state_changed = pyqtSignal(object, object)
    whipper_output = pyqtSignal(str)
    transcode_progress = pyqtSignal(int, int, str, bool)
    disc_spinup_poll = pyqtSignal(str, str, float, object)
    prompt_requested = pyqtSignal(str, object, object)


class GuiInteractionAdapter(UserInteractionPort):
    def __init__(self) -> None:
        self.signaler = GuiSignaler()
        self._cover_art_path: Optional[Path] = None
        self._art_lock = threading.Lock()

    def set_cover_art(self, path: Optional[Path]) -> None:
        with self._art_lock:
            self._cover_art_path = path

    def get_cover_art(self) -> Optional[Path]:
        with self._art_lock:
            return self._cover_art_path

    def is_interactive(self) -> bool:
        return True

    def _request_from_main_thread(self, prompt_type: str, payload: Any = None) -> Any:
        reply_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self.signaler.prompt_requested.emit(prompt_type, payload, reply_queue)
        return reply_queue.get()

    def on_log(self, level: LogLevel, message: str) -> None:
        self.signaler.log_emitted.emit(level, message)

    def on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        self.signaler.state_changed.emit(state, manifest)

    def on_whipper_output(self, line: str) -> None:
        self.signaler.whipper_output.emit(line)

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

    def prompt_calibration_retry(self, device: str) -> CalibrationChoice:
        return self._request_from_main_thread("prompt_calibration_retry", device)

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
    ) -> ConflictChoice:
        return self._request_from_main_thread(
            "prompt_conflict_resolution",
            (flac_dir, opus_dir),
        )
