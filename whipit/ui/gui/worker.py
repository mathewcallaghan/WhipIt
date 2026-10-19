"""Background QThread worker executing RipWorkflowCoordinator off the main GUI thread."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from whipit.bootstrap import ensure_first_run_initialized
from whipit.config import release_drive_locks, resolve_optical_drive
from whipit.inhibit import suppress_media_dialogs
from whipit.ui.base import LogLevel
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


class RipWorker(QThread):
    workflow_finished = pyqtSignal(bool, str)

    def __init__(
        self,
        ui_adapter: GuiInteractionAdapter,
        device: Optional[str] = None,
        speed: Optional[int] = None,
        manual_offset: Optional[int] = None,
        release_id: Optional[str] = None,
        output_root: Optional[Path] = None,
        keep_going: bool = False,
        unknown_disc: bool = False,
        overwrite: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.ui = ui_adapter
        self.requested_device = device
        self.speed = speed
        self.manual_offset = manual_offset
        self.release_id = release_id
        self.output_root = output_root
        self.keep_going = keep_going
        self.unknown_disc = unknown_disc
        self.overwrite = overwrite

        self.cancel_event = threading.Event()
        self.coordinator: Optional[RipWorkflowCoordinator] = None

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.coordinator:
            self.coordinator.cancel()
        if self.requested_device:
            release_drive_locks(self.requested_device)

    def run(self) -> None:
        try:
            with suppress_media_dialogs(self.requested_device):
                selected_device = resolve_optical_drive(self.requested_device, ui=self.ui)
                if not selected_device or self.cancel_event.is_set():
                    self.workflow_finished.emit(False, "Drive selection or disc readiness cancelled.")
                    return

                ensure_first_run_initialized(selected_device, ui=self.ui)

                self.coordinator = RipWorkflowCoordinator(
                    device=selected_device,
                    speed=self.speed,
                    manual_offset=self.manual_offset,
                    release_id=self.release_id,
                    output_root=self.output_root,
                    ui=self.ui,
                    cancel_event=self.cancel_event,
                )

                success = self.coordinator.run_workflow(
                    keep_going=self.keep_going,
                    unknown_disc=self.unknown_disc,
                    overwrite=self.overwrite,
                )

                if self.cancel_event.is_set():
                    self.workflow_finished.emit(False, "Rip cancelled by user.")
                elif success:
                    dest = self.coordinator.manifest.paths.final_flac_dir
                    parent_dir = str(dest.parent) if dest else str(self.coordinator.output_root)
                    self.workflow_finished.emit(True, f"Archive committed to {parent_dir}")
                else:
                    self.workflow_finished.emit(False, "Workflow completed with errors or was aborted.")
        except Exception as exc:
            self.ui.on_log(LogLevel.ERROR, f"Unhandled worker error: {exc}")
            self.workflow_finished.emit(False, f"Error: {exc}")
