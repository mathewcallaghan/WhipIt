"""Background QThread workers for the WhipIt desktop GUI."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import QWidget

from whipit.audio.metadata import fetch_cover_art_from_caa, fetch_release_details
from whipit.bootstrap import (
    check_or_install_dependencies,
    ensure_first_run_initialized,
    get_missing_dependencies,
)
from whipit.config import (
    eject_drive,
    get_disc_status,
    release_drive_locks,
    resolve_optical_drive,
)
from whipit.inhibit import MediaDialogInhibitor, suppress_media_dialogs
from whipit.models import JobManifest, WorkflowResult
from whipit.recovery import RecoveryCoordinator
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.state_store import ManifestStore
from whipit.ui.base import LogLevel
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


class StartupCheckWorker(QThread):
    checks_completed = pyqtSignal(bool, list, list, list)

    def __init__(
        self,
        ui_adapter: GuiInteractionAdapter,
        output_root: Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.ui = ui_adapter
        self.output_root = output_root

    def run(self) -> None:
        try:
            MediaDialogInhibitor.restore_persisted_state()
            check_or_install_dependencies(interactive=True, ui=self.ui)
            missing = get_missing_dependencies()

            store = ManifestStore()
            store.prune_committed_jobs(max_keep=10, max_age_days=7)

            coordinator = RecoveryCoordinator(store, ui=self.ui)
            cleanup_actions = coordinator.cleanup_orphaned_stages(output_root=self.output_root)

            if MediaDialogInhibitor.has_pending_state():
                cleanup_actions.append(
                    "Warning: Desktop media automount settings could not be fully restored from a previous run. "
                    "Original values remain preserved in recovery storage and will be retried on next shutdown."
                )

            uncommitted_reports = coordinator.inspect_uncommitted_jobs()
            preserved_jobs = []

            for manifest, status in uncommitted_reports:
                stage_flac = manifest.paths.candidate_stage / "flac"
                has_flacs = stage_flac.exists() and any(stage_flac.glob("*.flac"))
                has_cache_flacs = manifest.paths.whipper_stage.exists() and any(manifest.paths.whipper_stage.rglob("*.flac"))
                tx = manifest.transaction
                has_populated_journal = bool(
                    tx.flac_backed_up
                    or tx.opus_backed_up
                    or tx.flac_published
                    or tx.opus_published
                    or tx.flac_backup_path
                    or tx.opus_backup_path
                )
                if not (has_flacs or has_cache_flacs or has_populated_journal):
                    _, acts = coordinator.recover_job(manifest.job_id, discard_candidate=True)
                    cleanup_actions.extend(acts)
                else:
                    preserved_jobs.append((manifest, status))

            self.checks_completed.emit(
                len(missing) == 0,
                missing,
                preserved_jobs,
                cleanup_actions,
            )
        except Exception as exc:
            self.checks_completed.emit(
                False,
                [],
                [],
                [f"Startup checks failed: {exc}"],
            )


class DrivePollWorker(QThread):
    status_polled = pyqtSignal(str, int, int)

    def __init__(self, device: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.device = device

    def run(self) -> None:
        try:
            drive_st, disc_st = get_disc_status(self.device)
        except Exception:
            drive_st, disc_st = 0, 0
        self.status_polled.emit(self.device, drive_st, disc_st)


class ResumeWorker(QThread):
    resume_finished = pyqtSignal(bool, list)

    def __init__(
        self,
        coordinator: RecoveryCoordinator,
        job_id: str,
        ui_adapter: GuiInteractionAdapter,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.job_id = job_id
        self.ui = ui_adapter

    def run(self) -> None:
        try:
            ok, actions = self.coordinator.resume_publication(self.job_id, ui=self.ui)
        except Exception as exc:
            ok, actions = False, [f"Resume publication failed: {exc}"]
        self.resume_finished.emit(ok, actions)


class DiscardWorker(QThread):
    discard_finished = pyqtSignal(bool, list, str)

    def __init__(
        self,
        coordinator: RecoveryCoordinator,
        job_id: str,
        album_stem: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.coordinator = coordinator
        self.job_id = job_id
        self.album_stem = album_stem

    def run(self) -> None:
        try:
            ok, actions = self.coordinator.recover_job(self.job_id, discard_candidate=True)
        except Exception as exc:
            ok, actions = False, [f"Discard error: {exc}"]
        self.discard_finished.emit(ok, actions, self.album_stem)


class EjectWorker(QThread):
    eject_finished = pyqtSignal(str)

    def __init__(self, device: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.device = device

    def run(self) -> None:
        try:
            import whipit.ui.gui.main_window as _mw
            eject_fn = getattr(_mw, "eject_drive", None) or eject_drive
            eject_fn(self.device)
        except Exception:
            pass
        self.eject_finished.emit(self.device)


class ReleaseDetailsWorker(QThread):
    details_found = pyqtSignal(str, str, list)

    def __init__(
        self,
        mbid: str,
        barcode: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        expected_tracks: Optional[int] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.mbid = mbid
        self.barcode = barcode
        self.artist = artist
        self.title = title
        self.expected_tracks = expected_tracks
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            details = fetch_release_details(
                self.mbid,
                barcode=self.barcode,
                artist=self.artist,
                title=self.title,
                expected_tracks=self.expected_tracks,
            )
            if not self._cancelled:
                self.details_found.emit(self.mbid, details.discogs_url or "", details.tracks)
        except Exception:
            pass


class IdentifyWorker(QThread):
    inspection_completed = pyqtSignal(object)
    line_received = pyqtSignal(str)

    def __init__(self, device: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.device = device

    def run(self) -> None:
        try:
            release_drive_locks(self.device)
            result = inspect_disc(self.device, timeout=120.0, output_callback=self.line_received.emit)
        except Exception as exc:
            result = DiscInspectionResult(success=False, error_message=f"Inspection failed: {exc}")
        self.inspection_completed.emit(result)


class CoverArtWorker(QThread):
    cover_found = pyqtSignal(str, str)
    cover_failed = pyqtSignal(str)

    def __init__(self, mbid: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.mbid = mbid
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            art_path = fetch_cover_art_from_caa(self.mbid)
            if not self._cancelled:
                if art_path and art_path.exists():
                    self.cover_found.emit(self.mbid, str(art_path))
                else:
                    self.cover_failed.emit(self.mbid)
        except Exception:
            if not self._cancelled:
                self.cover_failed.emit(self.mbid)


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
            with suppress_media_dialogs(self.requested_device, ui=self.ui):
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

                result = self.coordinator.run_workflow(
                    keep_going=self.keep_going,
                    unknown_disc=self.unknown_disc,
                    overwrite=self.overwrite,
                )

                if self.cancel_event.is_set() or result == WorkflowResult.ABORTED:
                    msg = "Rip cancelled by user." if self.cancel_event.is_set() else "Workflow completed with errors or was aborted."
                    self.workflow_finished.emit(False, msg)
                elif result == WorkflowResult.COMMITTED_PARTIAL:
                    dest = self.coordinator.manifest.paths.final_flac_dir
                    parent_dir = str(dest.parent) if dest else str(self.coordinator.output_root)
                    self.workflow_finished.emit(True, f"Partial archive rescued and committed to {parent_dir}")
                else:
                    dest = self.coordinator.manifest.paths.final_flac_dir
                    parent_dir = str(dest.parent) if dest else str(self.coordinator.output_root)
                    self.workflow_finished.emit(True, f"Archive committed to {parent_dir}")
        except Exception as exc:
            self.ui.on_log(LogLevel.ERROR, f"Unhandled worker error: {exc}")
            self.workflow_finished.emit(False, f"Error: {exc}")


__all__ = [
    "CoverArtWorker",
    "DiscardWorker",
    "DrivePollWorker",
    "EjectWorker",
    "IdentifyWorker",
    "ReleaseDetailsWorker",
    "ResumeWorker",
    "RipWorker",
    "StartupCheckWorker",
]
