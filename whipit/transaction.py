"""Transaction manager for archival publication, backup, conflict resolution, and rollback."""

from __future__ import annotations

import datetime
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple
from whipit.models import JobManifest
from whipit.state_store import ManifestStore
from whipit.ui.base import ConflictChoice, UserInteractionPort
from whipit.ui.base import ConflictChoice, NullInteractionAdapter, UserInteractionPort


class TransactionError(Exception):
    pass


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
            bak_flac = final_flac.parent / f"{final_flac.name}.replaced.{ts}"
            bak_opus = final_opus.parent / f"{final_opus.name}.replaced.{ts}"

            if final_flac.exists():
                try:
                    if bak_flac.exists():
                        shutil.rmtree(bak_flac, ignore_errors=True)
                    os.replace(final_flac, bak_flac)
                    journal.flac_backed_up = True
                    journal.flac_backup_path = bak_flac
                    self.store.save(self.manifest)
                except Exception as e:
                    self.rollback()
                    raise TransactionError(f"Failed backing up FLAC archive: {e}") from e

            if final_opus.exists():
                try:
                    if bak_opus.exists():
                        shutil.rmtree(bak_opus, ignore_errors=True)
                    os.replace(final_opus, bak_opus)
                    journal.opus_backed_up = True
                    journal.opus_backup_path = bak_opus
                    self.store.save(self.manifest)
                except Exception as e:
                    self.rollback()
                    raise TransactionError(f"Failed backing up Opus mirror: {e}") from e
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
            self.rollback()
            raise TransactionError(f"Failed publishing FLAC archive: {e}") from e

        try:
            os.replace(stage_opus, final_opus)
            journal.opus_published = True
            self.store.save(self.manifest)
        except Exception as e:
            self.rollback()
            raise TransactionError(f"Failed publishing Opus mirror: {e}") from e

        # Commit Phase
        journal.committed = True
        self.store.save(self.manifest)

        # Post-commit cleanup: remove backup after successful publication
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
                os.replace(final_opus, target_undo)
                journal.opus_published = False
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.flac_published and final_flac and final_flac.exists():
            target_undo = candidate_stage / "flac"
            try:
                os.replace(final_flac, target_undo)
                journal.flac_published = False
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.opus_backed_up and journal.opus_backup_path and journal.opus_backup_path.exists() and final_opus:
            try:
                os.replace(journal.opus_backup_path, final_opus)
                journal.opus_backed_up = False
                journal.opus_backup_path = None
                self.store.save(self.manifest)
            except Exception:
                rollback_failed = True

        if journal.flac_backed_up and journal.flac_backup_path and journal.opus_backup_path is not None or (
            journal.flac_backed_up and journal.flac_backup_path and journal.flac_backup_path.exists() and final_flac
        ):
            if journal.flac_backup_path and journal.flac_backup_path.exists() and final_flac:
                try:
                    os.replace(journal.flac_backup_path, final_flac)
                    journal.flac_backed_up = False
                    journal.flac_backup_path = None
                    self.store.save(self.manifest)
                except Exception:
                    rollback_failed = True

        return not rollback_failed
