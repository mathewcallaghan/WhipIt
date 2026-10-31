"""Abstract interaction port and structured decision models for CLI and GUI frontends."""

from __future__ import annotations

from abc import ABC, abstractmethod
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple
from whipit.models import JobManifest, ReleaseOption, WorkflowState


class LogLevel(str, Enum):
    INFO = "INFO"
    OK = "OK"
    WARN = "WARN"
    ERROR = "ERROR"
    HEADER = "HEADER"


class DiscPromptAction(str, Enum):
    RETRY = "retry"
    EJECT = "eject"
    SWITCH_DRIVE = "switch_drive"
    MAIN_MENU = "main_menu"
    ABORT = "abort"


@dataclass
class DiscPromptResult:
    action: DiscPromptAction
    selected_device: Optional[str] = None


class CalibrationAction(str, Enum):
    AUTO_DETECT = "auto_detect"
    MANUAL_OFFSET = "manual_offset"
    SWAP_DISC = "swap_disc"
    USE_ZERO = "use_zero"
    SKIP = "skip"
    ABORT = "abort"


@dataclass
class CalibrationChoice:
    action: CalibrationAction
    manual_offset: Optional[int] = None


class ExtractionErrorAction(str, Enum):
    RETRY_SLOW_4X = "retry_4x"
    RETRY_MODERATE_8X = "retry_8x"
    EJECT_AND_RETRY = "eject_retry"
    SWITCH_DRIVE = "switch_drive"
    KEEP_GOING = "keep_going"
    ACCEPT_PARTIAL = "accept_partial"
    ABORT = "abort"


@dataclass
class ExtractionErrorChoice:
    action: ExtractionErrorAction
    selected_device: Optional[str] = None


class ConflictChoice(str, Enum):
    OVERWRITE = "overwrite"
    TIMESTAMP_SUFFIX = "timestamp_suffix"
    ABORT = "abort"


class UserInteractionPort(ABC):
    """
    Interface decoupling core CD ripping, calibration, and transcode workflows
    from any specific user interface (desktop GUI, headless worker, or unit tests).
    """

    @abstractmethod
    def is_interactive(self) -> bool:
        """Returns True if the frontend can respond to interactive user prompts."""

    @abstractmethod
    def on_log(self, level: LogLevel, message: str) -> None:
        """Emits a structured status message to the frontend."""

    @abstractmethod
    def on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        """Notifies the frontend whenever the job state machine transitions."""

    @abstractmethod
    def on_whipper_output(self, line: str) -> None:
        """Streams raw output lines from Whipper during extraction/calibration."""

    def on_whipper_event(self, event: Any) -> None:
        """Receives structured events parsed from Whipper execution."""
        pass

    @abstractmethod
    def on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        """Reports per-track Opus encoding completion."""

    @abstractmethod
    def on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        """Reports optical drive spin-up polling progress."""

    @abstractmethod
    def confirm_install_dependencies(self, missing_tools: List[str], install_cmd: str, manager: str) -> bool:
        """Asks the user whether to install missing system dependencies."""

    @abstractmethod
    def select_loaded_drive(self, drives_with_hw: List[Tuple[str, str]]) -> Optional[str]:
        """Asks the user to choose among multiple optical drives that have discs loaded."""

    @abstractmethod
    def prompt_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        """Asks the user what to do when multiple optical drives exist but none have a disc."""

    @abstractmethod
    def prompt_insert_disc(
        self,
        device: str,
        hardware_id: str,
        available_drives: Optional[List[Tuple[str, str]]] = None,
        allow_main_menu: bool = False,
    ) -> DiscPromptResult:
        """Prompts the user when no disc is detected in the target drive."""

    @abstractmethod
    def wait_for_user_reload(self, device: str, message: str) -> bool:
        """Pauses after ejecting a tray until the user reloads the disc."""

    @abstractmethod
    def prompt_extraction_speed(self) -> Optional[int]:
        """Prompts the user to choose an extraction speed."""

    @abstractmethod
    def prompt_select_release(self, releases: List[ReleaseOption]) -> Optional[str]:
        """Asks the user to choose among multiple matching MusicBrainz releases (returns MBID)."""

    @abstractmethod
    def prompt_calibration_start(self, device: str) -> Optional[CalibrationChoice]:
        """Prompts the user at the start of the drive calibration wizard."""

    @abstractmethod
    def prompt_calibration_retry(self, device: str, error_reason: Optional[str] = None) -> CalibrationChoice:
        """Prompts the user when automatic AccurateRip offset detection fails."""

    @abstractmethod
    def prompt_cdr_detected(self, device: str) -> bool:
        """Asks whether to continue ripping a detected CD-R."""

    @abstractmethod
    def prompt_unknown_disc(self) -> bool:
        """Asks whether to rip an uncatalogued disc as Unknown (-U)."""

    @abstractmethod
    def prompt_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        """Prompts the user for recovery action when read errors halt extraction."""

    @abstractmethod
    def prompt_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
        suggested_suffix: Optional[str] = None,
    ) -> ConflictChoice:
        """Asks how to resolve a destination directory collision."""

    @abstractmethod
    def get_cover_art(self) -> Optional[Path]:
        """Returns active or user-selected cover art path if available."""

    @abstractmethod
    def confirm_purge_stage(self, stage_dir: Path, flac_count: int) -> bool:
        """Asks the user whether an abandoned staging directory containing audio files should be purged."""


class NullInteractionAdapter(UserInteractionPort):
    def is_interactive(self) -> bool:
        return False

    def on_log(self, level: LogLevel, message: str) -> None:
        pass

    def on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        pass

    def on_whipper_output(self, line: str) -> None:
        pass

    def on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        pass

    def on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        pass

    def confirm_install_dependencies(self, missing_tools: List[str], install_cmd: str, manager: str) -> bool:
        return False

    def select_loaded_drive(self, drives_with_hw: List[Tuple[str, str]]) -> Optional[str]:
        return drives_with_hw[0][0] if drives_with_hw else None

    def prompt_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        return DiscPromptResult(DiscPromptAction.ABORT)

    def prompt_insert_disc(
        self,
        device: str,
        hardware_id: str,
        available_drives: Optional[List[Tuple[str, str]]] = None,
        allow_main_menu: bool = False,
    ) -> DiscPromptResult:
        return DiscPromptResult(DiscPromptAction.ABORT)

    def wait_for_user_reload(self, device: str, message: str) -> bool:
        return False

    def prompt_extraction_speed(self) -> Optional[int]:
        return None

    def prompt_select_release(self, releases: List[ReleaseOption]) -> Optional[str]:
        return releases[0].mbid if releases else None

    def prompt_calibration_start(self, device: str) -> Optional[CalibrationChoice]:
        return CalibrationChoice(CalibrationAction.ABORT)

    def prompt_calibration_retry(self, device: str, error_reason: Optional[str] = None) -> CalibrationChoice:
        return CalibrationChoice(CalibrationAction.ABORT)

    def prompt_cdr_detected(self, device: str) -> bool:
        return False

    def prompt_unknown_disc(self) -> bool:
        return False

    def prompt_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        return ExtractionErrorChoice(ExtractionErrorAction.ABORT)

    def prompt_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
        suggested_suffix: Optional[str] = None,
    ) -> ConflictChoice:
        return ConflictChoice.ABORT

    def get_cover_art(self) -> Optional[Path]:
        return None

    def confirm_purge_stage(self, stage_dir: Path, flac_count: int) -> bool:
        return False


class CliInteractionAdapter(UserInteractionPort):
    """Command-line interface adapter for interactive terminal and CLI execution."""

    def is_interactive(self) -> bool:
        return sys.stdin.isatty()

    def on_log(self, level: LogLevel, message: str) -> None:
        prefix = f"[{level.value}] " if level != LogLevel.INFO else ""
        sys.stdout.write(f"{prefix}{message}\n")
        sys.stdout.flush()

    def on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        pass

    def on_whipper_output(self, line: str) -> None:
        clean = line.strip()
        if clean and not clean.startswith("##:"):
            sys.stdout.write(f"{line}")
            sys.stdout.flush()

    def on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        sys.stdout.write(f"\rTranscoding: {completed}/{total} tracks ({track_name})")
        sys.stdout.flush()
        if completed == total:
            sys.stdout.write("\n")

    def on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        if ready is True:
            sys.stdout.write(f"\rDisc ready in {device}.\n")
        elif ready is False:
            sys.stdout.write(f"\rTimeout waiting for disc in {device}.\n")

    def confirm_install_dependencies(self, missing_tools: List[str], install_cmd: str, manager: str) -> bool:
        if not self.is_interactive():
            return False
        sys.stdout.write(f"Missing required dependencies: {', '.join(missing_tools)}\n")
        sys.stdout.write(f"Run '{install_cmd}'? [y/N]: ")
        sys.stdout.flush()
        try:
            return input().strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def select_loaded_drive(self, drives_with_hw: List[Tuple[str, str]]) -> Optional[str]:
        if not drives_with_hw:
            return None
        if len(drives_with_hw) == 1 or not self.is_interactive():
            return drives_with_hw[0][0]
        sys.stdout.write("Multiple loaded drives found:\n")
        for i, (d, hw) in enumerate(drives_with_hw, 1):
            sys.stdout.write(f"  [{i}] {d} ({hw})\n")
        sys.stdout.write(f"Select drive [1-{len(drives_with_hw)}]: ")
        sys.stdout.flush()
        try:
            choice = int(input().strip())
            return drives_with_hw[choice - 1][0]
        except (ValueError, IndexError, EOFError, KeyboardInterrupt):
            return drives_with_hw[0][0]

    def prompt_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        return DiscPromptResult(DiscPromptAction.ABORT)

    def prompt_insert_disc(
        self,
        device: str,
        hardware_id: str,
        available_drives: Optional[List[Tuple[str, str]]] = None,
        allow_main_menu: bool = False,
    ) -> DiscPromptResult:
        if not self.is_interactive():
            return DiscPromptResult(DiscPromptAction.ABORT)
        sys.stdout.write(f"No Audio CD detected in {device} ({hardware_id}).\n")
        sys.stdout.write("[r]etry, [e]ject, [a]bort: ")
        sys.stdout.flush()
        try:
            ans = input().strip().lower()
            if ans == "r":
                return DiscPromptResult(DiscPromptAction.RETRY)
            if ans == "e":
                return DiscPromptResult(DiscPromptAction.EJECT)
        except (EOFError, KeyboardInterrupt):
            pass
        return DiscPromptResult(DiscPromptAction.ABORT)

    def wait_for_user_reload(self, device: str, message: str) -> bool:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()
        if not self.is_interactive():
            return True
        try:
            input("Press [Enter] when disc is inserted...")
            return True
        except (EOFError, KeyboardInterrupt):
            return False

    def prompt_extraction_speed(self) -> Optional[int]:
        return None

    def prompt_select_release(self, releases: List[ReleaseOption]) -> Optional[str]:
        if not releases:
            return None
        if len(releases) == 1 or not self.is_interactive():
            return releases[0].mbid
        sys.stdout.write("Matching MusicBrainz releases:\n")
        for i, r in enumerate(releases, 1):
            extra = f" ({r.country or 'Unknown'} {r.year or ''})" if (r.country or r.year) else ""
            sys.stdout.write(f"  [{i}] {r.title} by {r.artist}{extra}\n")
        sys.stdout.write(f"Select release [1-{len(releases)}]: ")
        sys.stdout.flush()
        try:
            idx = int(input().strip()) - 1
            return releases[idx].mbid
        except (ValueError, IndexError, EOFError, KeyboardInterrupt):
            return releases[0].mbid

    def prompt_calibration_start(self, device: str) -> Optional[CalibrationChoice]:
        return CalibrationChoice(CalibrationAction.AUTO_DETECT)

    def prompt_calibration_retry(self, device: str, error_reason: Optional[str] = None) -> CalibrationChoice:
        if error_reason:
            sys.stdout.write(f"Calibration notice: {error_reason}\n")
        return CalibrationChoice(CalibrationAction.ABORT)

    def prompt_cdr_detected(self, device: str) -> bool:
        return False

    def prompt_unknown_disc(self) -> bool:
        return False

    def prompt_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        return ExtractionErrorChoice(ExtractionErrorAction.ABORT)

    def prompt_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
        suggested_suffix: Optional[str] = None,
    ) -> ConflictChoice:
        if warning:
            sys.stdout.write(f"{warning}\n")
        return ConflictChoice.TIMESTAMP_SUFFIX

    def get_cover_art(self) -> Optional[Path]:
        return None

    def confirm_purge_stage(self, stage_dir: Path, flac_count: int) -> bool:
        return False
