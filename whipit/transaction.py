"""Transaction manager for archival publication, backup, conflict resolution, and rollback."""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple
from whipit.config import truncate_to_bytes
from whipit.models import JobManifest
from whipit.state_store import ManifestStore
from whipit.ui.base import ConflictChoice, NullInteractionAdapter, UserInteractionPort


class TransactionError(Exception):
    def __init__(self, message: str, rollback_succeeded: bool = True):
        super().__init__(message)
        self.rollback_succeeded = rollback_succeeded


class ArchiveTransactionManager:
    def __init__(self, manifest: JobManifest, store: ManifestStore):
        self.manifest = manifest
        self.store = store

    def resolve_destination_conflict(
        self,
        overwrite: bool = False,
        ui: Optional[UserInteractionPort] = None,
    ) -> Tuple[bool, bool, Optional[str]]:
        """
        Checks for existing destination folders and queries the UI port for resolution if needed.
        Returns: (proceed, effective_overwrite, timestamp_suffix)
        """
        if ui is None:
            ui = NullInteractionAdapter()

        final_flac = self.manifest.paths.final_flac_dir
        final_opus = self.manifest.paths.final_opus_dir
        collision = (final_flac and final_flac.exists()) or (final_opus and final_opus.exists())

        if not collision or overwrite:
            return True, overwrite, None

        if not ui.is_interactive():
            return False, False, None

        warning: Optional[str] = None
        if final_flac and final_flac.exists():
            from whipit.ripping.reports import find_whipper_log, parse_whipper_report

            existing_log = find_whipper_log(final_flac)
            existing_accurate = False
            if existing_log:
                _, existing_ar, _, _ = parse_whipper_report(existing_log)
                existing_accurate = (existing_ar == "All tracks accurately ripped")

            new_accurate = self.manifest.verification.is_accurate
            is_partial = bool(self.manifest.tracks.missing_tracks) or (
                self.manifest.tracks.expected_count > 0
                and self.manifest.tracks.extracted_count < self.manifest.tracks.expected_count
            )

            if existing_accurate and not new_accurate:
                warning = (
                    "⚠️ WARNING: The existing archive is AccurateRip verified, but the new rip "
                    "is NOT verified. Overwriting will replace a verified archive with an unverified one!"
                )
            elif is_partial:
                warning = (
                    "⚠️ WARNING: The new rip is incomplete (partial tracks). "
                    "Overwriting will replace an existing archive with a partial one!"
                )

        try:
            choice = ui.prompt_conflict_resolution(final_flac, final_opus, warning=warning)
        except TypeError:
            choice = ui.prompt_conflict_resolution(final_flac, final_opus)
        if choice == ConflictChoice.OVERWRITE:
            return True, True, None
        if choice == ConflictChoice.TIMESTAMP_SUFFIX:
            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            return True, False, ts
        return False, False, None

    def execute_publication(self, stage_flac: Path, stage_opus: Path, overwrite: bool = False) -> None:
        final_flac = self.manifest.paths.final_flac_dir
        final_opus = self.manifest.paths.final_opus_dir

        if not final_flac or not final_opus:
            raise TransactionError("Destination paths are not resolved in manifest.")

        journal = self.manifest.transaction

        if overwrite:
            ts = self.manifest.created_at.strftime("%Y-%m-%d_%H-%M-%S")
            bak_suffix = f".replaced.{ts}"
            bak_flac_name = truncate_to_bytes(final_flac.name, max_bytes=220) + bak_suffix
            bak_opus_name = truncate_to_bytes(final_opus.name, max_bytes=220) + bak_suffix
            bak_flac = final_flac.parent / bak_flac_name
            bak_opus = final_opus.parent / bak_opus_name

            if final_flac.exists():
                try:
                    if bak_flac.exists():
                        shutil.rmtree(bak_flac, ignore_errors=True)
                    os.replace(final_flac, bak_flac)
                    journal.flac_backed_up = True
                    journal.flac_backup_path = bak_flac
                    self.store.save(self.manifest)
                except Exception as e:
                    rb_ok = self.rollback()
                    raise TransactionError(f"Failed backing up FLAC archive: {e}", rollback_succeeded=rb_ok) from e

            if final_opus.exists():
                try:
                    if bak_opus.exists():
                        shutil.rmtree(bak_opus, ignore_errors=True)
                    os.replace(final_opus, bak_opus)
                    journal.opus_backed_up = True
                    journal.opus_backup_path = bak_opus
                    self.store.save(self.manifest)
                except Exception as e:
                    rb_ok = self.rollback()
                    raise TransactionError(f"Failed backing up Opus mirror: {e}", rollback_succeeded=rb_ok) from e
        else:
            if final_flac.exists() or final_opus.exists():
                existing = final_flac if final_flac.exists() else final_opus
                raise TransactionError(
                    f"Destination already exists: '{existing}'. "
                    "Use --overwrite or select an option to replace/rename."
                )

        final_flac.parent.mkdir(parents=True, exist_ok=True)
        final_opus.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.replace(stage_flac, final_flac)
            journal.flac_published = True
            self.store.save(self.manifest)
        except Exception as e:
            rb_ok = self.rollback()
            raise TransactionError(f"Failed publishing FLAC archive: {e}", rollback_succeeded=rb_ok) from e

        try:
            os.replace(stage_opus, final_opus)
            journal.opus_published = True
            self.store.save(self.manifest)
        except Exception as e:
            rb_ok = self.rollback()
            raise TransactionError(f"Failed publishing Opus mirror: {e}", rollback_succeeded=rb_ok) from e

        journal.committed = True
        self.store.save(self.manifest)

        if journal.flac_backed_up and journal.flac_backup_path and journal.flac_backup_path.exists():
            shutil.rmtree(journal.flac_backup_path, ignore_errors=True)
            journal.flac_backed_up = False
            journal.flac_backup_path = None

        if journal.opus_backed_up and journal.opus_backup_path and journal.opus_backup_path.exists():
            shutil.rmtree(journal.opus_backup_path, ignore_errors=True)
            journal.opus_backed_up = False
            journal.opus_backup_path = None

        self.store.save(self.manifest)

    def rollback(self) -> bool:
        journal = self.manifest.transaction
        final_flac = self.manifest.paths.final_flac_dir
        final_opus = self.manifest.paths.final_opus_dir
        candidate_stage = self.manifest.paths.candidate_stage

        rollback_failed = False

        if journal.opus_published and final_opus and final_opus.exists():
            target_undo = candidate_stage / "opus"
            try:
                if target_undo.exists():
                    shutil.rmtree(target_undo, ignore_errors=True)
                target_undo.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_opus, target_undo)
                journal.opus_published = False
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.flac_published and final_flac and final_flac.exists():
            target_undo = candidate_stage / "flac"
            try:
                if target_undo.exists():
                    shutil.rmtree(target_undo, ignore_errors=True)
                target_undo.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_flac, target_undo)
                journal.flac_published = False
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.opus_backed_up and journal.opus_backup_path and journal.opus_backup_path.exists() and final_opus:
            try:
                if final_opus.exists():
                    shutil.rmtree(final_opus, ignore_errors=True)
                final_opus.parent.mkdir(parents=True, exist_ok=True)
                os.replace(journal.opus_backup_path, final_opus)
                journal.opus_backed_up = False
                journal.opus_backup_path = None
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.flac_backed_up and journal.flac_backup_path and journal.flac_backup_path.exists() and final_flac:
            try:
                if final_flac.exists():
                    shutil.rmtree(final_flac, ignore_errors=True)
                final_flac.parent.mkdir(parents=True, exist_ok=True)
                os.replace(journal.flac_backup_path, final_flac)
                journal.flac_backed_up = False
                journal.flac_backup_path = None
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        return not rollback_failed
