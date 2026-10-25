"""Transaction manager for archival publication, backup, conflict resolution, and rollback."""

from __future__ import annotations

import datetime
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple
from whipit.config import get_cache_dir, truncate_to_bytes
from whipit.models import (
    InvariantViolationError,
    JobManifest,
    WorkflowState,
    require_within_root,
)
from whipit.state_store import ManifestStore
from whipit.ui.base import ConflictChoice, LogLevel, NullInteractionAdapter, UserInteractionPort
from whipit.validation.flac import read_flac_tag

logger = logging.getLogger(__name__)


class TransactionError(Exception):
    def __init__(self, message: str, rollback_succeeded: bool = True):
        super().__init__(message)
        self.rollback_succeeded = rollback_succeeded


def validate_transaction_paths(manifest: JobManifest) -> None:
    paths = manifest.paths
    flac_root = (paths.flac_root or paths.output_root).resolve(strict=False)
    opus_root = paths.opus_root.resolve(strict=False) if paths.opus_root else flac_root
    candidate_stage = paths.candidate_stage.resolve(strict=False)

    try:
        candidate_stage.relative_to(flac_root)
    except ValueError as exc:
        raise InvariantViolationError("Candidate staging path is outside the output root") from exc

    if paths.candidate_stage_opus is not None:
        try:
            paths.candidate_stage_opus.resolve(strict=False).relative_to(opus_root)
        except ValueError as exc:
            raise InvariantViolationError("Candidate Opus staging path is outside the Opus root") from exc

    if paths.final_flac_dir is not None:
        resolved = paths.final_flac_dir.resolve(strict=False)
        try:
            resolved.relative_to(flac_root)
        except ValueError as exc:
            raise InvariantViolationError(
                f"Final archive path is outside the output root: {paths.final_flac_dir}"
            ) from exc

    if paths.final_opus_dir is not None:
        resolved = paths.final_opus_dir.resolve(strict=False)
        try:
            resolved.relative_to(opus_root)
        except ValueError as exc:
            raise InvariantViolationError(
                f"Final Opus path is outside the Opus root: {paths.final_opus_dir}"
            ) from exc

    if manifest.transaction.flac_backup_path is not None:
        resolved_bp = manifest.transaction.flac_backup_path.resolve(strict=False)
        try:
            resolved_bp.relative_to(flac_root)
        except ValueError as exc:
            raise InvariantViolationError(
                f"Backup path is outside the output root: {manifest.transaction.flac_backup_path}"
            ) from exc

    if manifest.transaction.opus_backup_path is not None:
        resolved_bp = manifest.transaction.opus_backup_path.resolve(strict=False)
        try:
            resolved_bp.relative_to(opus_root)
        except ValueError as exc:
            raise InvariantViolationError(
                f"Opus backup path is outside the Opus root: {manifest.transaction.opus_backup_path}"
            ) from exc

    whipper_stage = paths.whipper_stage.resolve(strict=False)
    cache_root = get_cache_dir().resolve(strict=False)
    try:
        whipper_stage.relative_to(cache_root)
    except ValueError as exc:
        raise InvariantViolationError("Cache staging path is outside the cache root") from exc

    managed_paths = [
        paths.candidate_stage,
        paths.candidate_stage_opus,
        paths.whipper_stage,
        paths.final_flac_dir,
        paths.final_opus_dir,
        manifest.transaction.flac_backup_path,
        manifest.transaction.opus_backup_path,
    ]
    for p in managed_paths:
        if p is not None and p.is_symlink():
            raise InvariantViolationError(f"Refusing symlink for managed transaction path: {p}")

    manifest.validate_paths()


def validate_publish_preconditions(manifest: JobManifest) -> None:
    if manifest.state not in (WorkflowState.CANDIDATE_VERIFIED, WorkflowState.PUBLISHING):
        state_val = getattr(manifest.state, "value", manifest.state)
        raise InvariantViolationError(f"Cannot publish from state {state_val!r}")

    if not manifest.verification.flac_integrity_passed:
        raise InvariantViolationError("Cannot publish: FLAC integrity verification did not pass")

    if not manifest.paths.final_flac_dir:
        raise InvariantViolationError("Missing FLAC destination")

    if manifest.paths.generate_opus and not manifest.paths.final_opus_dir:
        raise InvariantViolationError("Missing Opus destination")


def validate_commit_preconditions(manifest: JobManifest) -> None:
    journal = manifest.transaction
    flac_published = journal.flac_published
    opus_published = journal.opus_published if manifest.paths.generate_opus else True
    if not (flac_published and opus_published and journal.committed):
        raise InvariantViolationError("Cannot commit: transaction journal does not reflect published state")

    final_flac = manifest.paths.final_flac_dir
    final_opus = manifest.paths.final_opus_dir
    if not final_flac or not final_flac.exists():
        raise InvariantViolationError(f"Cannot commit: FLAC destination does not exist: {final_flac}")
    if manifest.paths.generate_opus:
        if not final_opus or not final_opus.exists():
            raise InvariantViolationError(f"Cannot commit: Opus destination does not exist: {final_opus}")


class ArchiveTransactionManager:
    def __init__(self, manifest: JobManifest, store: ManifestStore):
        self.manifest = manifest
        self.store = store

    def _existing_release_id(self, final_flac: Path) -> Optional[str]:
        if not final_flac.exists():
            return None
        flacs = sorted(list(final_flac.rglob("*.flac")))
        if not flacs:
            return None
        first = flacs[0]
        return read_flac_tag(first, "MUSICBRAINZ_ALBUMID") or read_flac_tag(first, "MUSICBRAINZ_RELEASEID")

    def _detect_downgrade(self, final_flac: Optional[Path]) -> Tuple[bool, Optional[str]]:
        if not final_flac or not final_flac.exists():
            return False, None

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

        warning: Optional[str] = None
        is_downgrade = False

        if existing_accurate and not new_accurate:
            warning = (
                "⚠️ WARNING: The existing archive is AccurateRip verified, but the new rip "
                "is NOT verified. Overwriting will replace a verified archive with an unverified one!"
            )
            is_downgrade = True
        elif is_partial:
            warning = (
                "⚠️ WARNING: The new rip is incomplete (partial tracks). "
                "Overwriting will replace an existing archive with a partial one!"
            )
            is_downgrade = True

        existing_id = self._existing_release_id(final_flac)
        new_id = self.manifest.metadata.musicbrainz_id
        if existing_id and new_id and existing_id != new_id:
            diff_msg = (
                "⚠️ WARNING: The existing archive is a DIFFERENT MusicBrainz release "
                "of this album. Overwriting will replace it; a backup will be kept."
            )
            warning = f"{warning}\n{diff_msg}" if warning else diff_msg
            is_downgrade = True

        if final_flac.exists() and not any(final_flac.rglob("*.flac")):
            msg = (
                "⚠️ WARNING: The destination exists but does not look like a WhipIt archive "
                "(no FLAC files). Overwriting will replace its contents; a backup will be kept."
            )
            warning = f"{warning}\n{msg}" if warning else msg
            is_downgrade = True

        return is_downgrade, warning

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

        if not collision:
            return True, overwrite, None

        _, warning = self._detect_downgrade(final_flac)

        if overwrite and not warning:
            return True, True, None

        if not ui.is_interactive():
            if warning:
                ui.on_log(LogLevel.WARN, warning)
            return False, False, None

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

    def execute_publication(
        self,
        stage_flac: Path,
        stage_opus: Optional[Path] = None,
        overwrite: bool = False,
    ) -> None:
        validate_transaction_paths(self.manifest)
        if self.manifest.state in (WorkflowState.CANDIDATE_VERIFIED, WorkflowState.PUBLISHING):
            validate_publish_preconditions(self.manifest)
        final_flac = self.manifest.paths.final_flac_dir
        final_opus = self.manifest.paths.final_opus_dir

        if not final_flac or (self.manifest.paths.generate_opus and not final_opus):
            raise TransactionError("Destination paths are not resolved in manifest.")

        flac_root = (self.manifest.paths.flac_root or self.manifest.paths.output_root).resolve(strict=False)
        opus_root = self.manifest.paths.opus_root.resolve(strict=False) if self.manifest.paths.opus_root else flac_root

        candidate_stage_flac = self.manifest.paths.candidate_stage
        candidate_stage_opus = self.manifest.paths.candidate_stage_opus or candidate_stage_flac

        require_within_root(stage_flac, candidate_stage_flac)
        require_within_root(final_flac, flac_root)
        if self.manifest.paths.generate_opus and final_opus and stage_opus:
            require_within_root(stage_opus, candidate_stage_opus)
            require_within_root(final_opus, opus_root)

        journal = self.manifest.transaction

        if overwrite:
            ts = self.manifest.created_at.strftime("%Y-%m-%d_%H-%M-%S")
            bak_suffix = f".replaced.{ts}"
            bak_flac_name = truncate_to_bytes(final_flac.name, max_bytes=220) + bak_suffix
            bak_flac = final_flac.parent / bak_flac_name

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

            if self.manifest.paths.generate_opus and final_opus and final_opus.exists():
                bak_opus_name = truncate_to_bytes(final_opus.name, max_bytes=220) + bak_suffix
                bak_opus = final_opus.parent / bak_opus_name
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
            if final_flac.exists():
                raise TransactionError(
                    f"Destination already exists: '{final_flac}'. "
                    "Use --overwrite or select an option to replace/rename."
                )
            if self.manifest.paths.generate_opus and final_opus and final_opus.exists():
                raise TransactionError(
                    f"Destination already exists: '{final_opus}'. "
                    "Use --overwrite or select an option to replace/rename."
                )

        final_flac.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.replace(stage_flac, final_flac)
            journal.flac_published = True
            self.store.save(self.manifest)
        except Exception as e:
            rb_ok = self.rollback()
            raise TransactionError(f"Failed publishing FLAC archive: {e}", rollback_succeeded=rb_ok) from e

        if self.manifest.paths.generate_opus and final_opus and stage_opus:
            final_opus.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(stage_opus, final_opus)
                journal.opus_published = True
                self.store.save(self.manifest)
            except Exception as e:
                rb_ok = self.rollback()
                raise TransactionError(f"Failed publishing Opus mirror: {e}", rollback_succeeded=rb_ok) from e

        journal.committed = True
        self.store.save(self.manifest)

    def rollback(self) -> bool:
        try:
            validate_transaction_paths(self.manifest)
        except Exception:
            return False

        journal = self.manifest.transaction
        final_flac = self.manifest.paths.final_flac_dir
        final_opus = self.manifest.paths.final_opus_dir

        flac_root = (self.manifest.paths.flac_root or self.manifest.paths.output_root).resolve(strict=False)
        opus_root = self.manifest.paths.opus_root.resolve(strict=False) if self.manifest.paths.opus_root else flac_root

        candidate_stage_flac = self.manifest.paths.candidate_stage
        candidate_stage_opus = self.manifest.paths.candidate_stage_opus or candidate_stage_flac

        rollback_failed = False

        if journal.opus_published and final_opus and final_opus.exists():
            target_undo = candidate_stage_opus / "opus"
            try:
                require_within_root(final_opus, opus_root)
                require_within_root(target_undo, candidate_stage_opus)
                if final_opus.is_symlink() or target_undo.is_symlink():
                    raise ValueError("Symlinks not allowed in rollback targets")
                if target_undo.exists():
                    shutil.rmtree(target_undo, ignore_errors=True)
                target_undo.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_opus, target_undo)
                journal.opus_published = False
                self.store.save(self.manifest)
            except Exception as exc:
                logger.error("Rollback failed reverting opus publication for job %s: %s", self.manifest.job_id, exc)
                rollback_failed = True

        if journal.flac_published and final_flac and final_flac.exists():
            target_undo = candidate_stage_flac / "flac"
            try:
                require_within_root(final_flac, flac_root)
                require_within_root(target_undo, candidate_stage_flac)
                if final_flac.is_symlink() or target_undo.is_symlink():
                    raise ValueError("Symlinks not allowed in rollback targets")
                if target_undo.exists():
                    shutil.rmtree(target_undo, ignore_errors=True)
                target_undo.parent.mkdir(parents=True, exist_ok=True)
                os.replace(final_flac, target_undo)
                journal.flac_published = False
                self.store.save(self.manifest)
            except Exception as exc:
                logger.error("Rollback failed reverting flac publication for job %s: %s", self.manifest.job_id, exc)
                rollback_failed = True

        if journal.opus_backed_up and journal.opus_backup_path and journal.opus_backup_path.exists() and final_opus:
            try:
                require_within_root(journal.opus_backup_path, opus_root)
                require_within_root(final_opus, opus_root)
                if journal.opus_backup_path.is_symlink() or final_opus.is_symlink():
                    raise ValueError("Symlinks not allowed in backup restoration")
                if final_opus.exists():
                    shutil.rmtree(final_opus, ignore_errors=True)
                final_opus.parent.mkdir(parents=True, exist_ok=True)
                os.replace(journal.opus_backup_path, final_opus)
                journal.opus_backed_up = False
                journal.opus_backup_path = None
                self.store.save(self.manifest)
            except Exception as exc:
                logger.error("Rollback failed restoring opus backup for job %s: %s", self.manifest.job_id, exc)
                rollback_failed = True

        if journal.flac_backed_up and journal.flac_backup_path and journal.flac_backup_path.exists() and final_flac:
            try:
                require_within_root(journal.flac_backup_path, flac_root)
                require_within_root(final_flac, flac_root)
                if journal.flac_backup_path.is_symlink() or final_flac.is_symlink():
                    raise ValueError("Symlinks not allowed in backup restoration")
                if final_flac.exists():
                    shutil.rmtree(final_flac, ignore_errors=True)
                final_flac.parent.mkdir(parents=True, exist_ok=True)
                os.replace(journal.flac_backup_path, final_flac)
                journal.flac_backed_up = False
                journal.flac_backup_path = None
                self.store.save(self.manifest)
            except Exception as exc:
                logger.error("Rollback failed restoring flac backup for job %s: %s", self.manifest.job_id, exc)
                rollback_failed = True

        return not rollback_failed
