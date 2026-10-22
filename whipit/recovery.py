"""Manifest reconciliation, post-crash state inspection, orphaned stage cleanup, and publication resumption."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from whipit.audio.metadata import (
    create_embedded_cover_art,
    embed_cover_art_flacs,
    find_cover_art,
    replicate_art_to_opus,
)
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.config import get_cache_dir, get_output_root, truncate_to_bytes
from whipit.models import JobManifest, WorkflowState
from whipit.notifications import notify_rip_success
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager, TransactionError
from whipit.ui.base import NullInteractionAdapter, UserInteractionPort
from whipit.validation.flac import verify_flac_integrity


class RecoveryCoordinator:
    def __init__(self, store: ManifestStore, ui: Optional[UserInteractionPort] = None):
        self.store = store
        self.ui = ui

    def inspect_uncommitted_jobs(self) -> List[Tuple[JobManifest, str]]:
        jobs = self.store.list_uncommitted_jobs()
        reports = []
        for j in jobs:
            if self.store.is_job_active(j.job_id):
                continue
            status = self._reconcile_job_state(j)
            reports.append((j, status))
        return reports

    def _reconcile_job_state(self, manifest: JobManifest) -> str:
        tx = manifest.transaction
        if tx.committed:
            return "Committed"

        stranded_backups = []
        if tx.flac_backed_up and tx.flac_backup_path and tx.flac_backup_path.exists():
            stranded_backups.append(str(tx.flac_backup_path))
        if tx.opus_backed_up and tx.opus_backup_path and tx.opus_backup_path.exists():
            stranded_backups.append(str(tx.opus_backup_path))

        if stranded_backups:
            return f"Requires Rollback (Stranded backups: {', '.join(stranded_backups)})"

        if manifest.state == WorkflowState.ROLLBACK_FAILED:
            return "Rollback Failed (Preserved for manual inspection)"

        if manifest.paths.candidate_stage.exists():
            stage_flac = manifest.paths.candidate_stage / "flac"
            if stage_flac.exists() and any(stage_flac.glob("*.flac")):
                if manifest.state == WorkflowState.PUBLISH_FAILED:
                    return f"Publication Failed (Rolled back, verified FLAC candidate preserved: {manifest.paths.candidate_stage})"
                return f"Verified FLAC Candidate Preserved ({manifest.paths.candidate_stage})"

            return f"Incomplete Publication (Candidate present: {manifest.paths.candidate_stage})"

        if manifest.paths.whipper_stage.exists():
            return f"Incomplete Extraction (Cache present: {manifest.paths.whipper_stage})"

        return "Aborted prior to publication"

    def cleanup_orphaned_stages(
        self,
        output_root: Optional[Path] = None,
        ui: Optional[UserInteractionPort] = None,
    ) -> List[str]:
        """
        Scans cache_dir and output_root for abandoned staging directories not owned by any active job.
        Reclaims stranded disk space, but never purges staging directories containing .flac files
        without explicit user confirmation.
        """
        actions = []
        effective_ui = ui or self.ui
        all_jobs = self.store.list_all_jobs()
        active_job_ids = {j.job_id for j in all_jobs if self.store.is_job_active(j.job_id)}
        known_uncommitted_ids = {j.job_id for j in all_jobs if not j.transaction.committed}
        quarantined_ids = self.store.list_quarantined_job_ids()
        protected_ids = active_job_ids | known_uncommitted_ids | quarantined_ids

        def _should_purge_stage(stage_path: Path) -> bool:
            try:
                flacs = list(stage_path.rglob("*.flac"))
            except OSError:
                flacs = []

            if not flacs:
                return True

            if effective_ui and effective_ui.is_interactive():
                return effective_ui.confirm_purge_stage(stage_path, len(flacs))

            return False

        # 1. Clean cache_dir stage_* directories
        cache_dir = get_cache_dir()
        if cache_dir.exists():
            for d in cache_dir.glob("stage_*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace("stage_", "")
                if job_id not in protected_ids:
                    if _should_purge_stage(d):
                        shutil.rmtree(d, ignore_errors=True)
                        actions.append(f"Purged abandoned cache stage: {d.name}")
                    else:
                        actions.append(f"Preserved orphaned cache stage with FLAC files: {d.name}")

        # 2. Clean output_root .whipit-stage.* directories
        root = output_root or get_output_root()
        if root.exists():
            for d in root.glob(".whipit-stage.*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace(".whipit-stage.", "")
                if job_id not in protected_ids:
                    if _should_purge_stage(d):
                        shutil.rmtree(d, ignore_errors=True)
                        actions.append(f"Purged abandoned candidate stage: {d.name}")
                    else:
                        actions.append(f"Preserved orphaned candidate stage with FLAC files: {d.name}")

        return actions

    def recover_job(self, job_id: str, discard_candidate: bool = False) -> Tuple[bool, List[str]]:
        """
        Safely rolls back partial publications and removes temporary stages only on success.
        If rollback fails, staging files are preserved to prevent data destruction.
        """
        if self.store.is_job_active(job_id):
            return False, [f"Job {job_id} is actively running in another process and cannot be recovered."]

        try:
            manifest = self.store.load(job_id)
        except FileNotFoundError:
            return True, [f"Manifest for job {job_id} not found (already cleared)."]
        except Exception as exc:
            return False, [f"Error loading manifest for job {job_id}: {exc}"]
        mgr = ArchiveTransactionManager(manifest, self.store)
        success = mgr.rollback()
        actions: List[str] = []

        if not success:
            manifest.transition_to(WorkflowState.ROLLBACK_FAILED)
            self.store.save(manifest)
            actions.append("Rollback failed! Staging and cache directories preserved for manual inspection.")
            return False, actions

        actions.append("Rolled back transactional changes")

        stage_flac = manifest.paths.candidate_stage / "flac"
        has_flacs = stage_flac.exists() and any(stage_flac.glob("*.flac"))

        if has_flacs and not discard_candidate:
            actions.append(f"Preserved extracted FLAC candidate: {manifest.paths.candidate_stage}")
        elif manifest.paths.candidate_stage.exists():
            try:
                shutil.rmtree(manifest.paths.candidate_stage, ignore_errors=True)
                actions.append(f"Removed staging directory: {manifest.paths.candidate_stage}")
            except Exception as e:
                actions.append(f"Failed removing staging directory: {e}")

        if manifest.paths.whipper_stage.exists():
            try:
                shutil.rmtree(manifest.paths.whipper_stage, ignore_errors=True)
                actions.append(f"Removed cache directory: {manifest.paths.whipper_stage}")
            except Exception as e:
                actions.append(f"Failed removing cache directory: {e}")

        if not (has_flacs and not discard_candidate):
            self.store.delete(job_id)
            actions.append("Cleared job manifest")
        else:
            self.store.save(manifest)

        return True, actions

    def _apply_timestamp_suffix(self, manifest: JobManifest, ts_suffix: str) -> None:
        if not ts_suffix or not manifest.paths.final_flac_dir or not manifest.paths.final_opus_dir:
            return
        output_root = manifest.paths.output_root or get_output_root()
        raw_stem = f"{manifest.metadata.artist} - {manifest.metadata.album}"
        stem = truncate_to_bytes(raw_stem, max_bytes=180)
        master_name = truncate_to_bytes(f"{stem} ({ts_suffix})", max_bytes=240)
        master_dir = output_root / master_name

        flac_name = truncate_to_bytes(f"{stem} (FLAC)", max_bytes=220)
        opus_name = truncate_to_bytes(f"{stem} (Opus)", max_bytes=220)
        flac_dir = master_dir / flac_name
        opus_dir = master_dir / opus_name

        is_multi = manifest.metadata.disc_number is not None and (
            manifest.metadata.disc_number > 1
            or (manifest.metadata.total_discs is not None and manifest.metadata.total_discs > 1)
        )
        if is_multi and manifest.metadata.disc_number:
            flac_dir = flac_dir / f"Disc {manifest.metadata.disc_number}"
            opus_dir = opus_dir / f"Disc {manifest.metadata.disc_number}"

        manifest.paths.final_flac_dir = flac_dir
        manifest.paths.final_opus_dir = opus_dir
        self.store.save(manifest)

    def resume_publication(
        self,
        job_id: str,
        overwrite: bool = False,
        ui: Optional[UserInteractionPort] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Resumes and finishes publication for a preserved candidate stage.
        Completes Opus encoding, ReplayGain tagging, integrity verification, and atomic commit.
        """
        if self.store.is_job_active(job_id):
            return False, [f"Job {job_id} is actively running in another process."]

        manifest = self.store.load(job_id)
        effective_ui = ui or self.ui or NullInteractionAdapter()
        stage_flac = manifest.paths.candidate_stage / "flac"
        stage_opus = manifest.paths.candidate_stage / "opus"

        if not stage_flac.exists():
            return False, [f"Staged FLAC directory does not exist: {stage_flac}"]

        c_flacs = sorted(list(stage_flac.glob("*.flac")))
        if not c_flacs:
            return False, [f"No FLAC files found in staging directory: {stage_flac}"]

        stage_opus.mkdir(parents=True, exist_ok=True)
        cover_art = find_cover_art(stage_flac)
        embed_art_temp: Optional[Path] = None
        embed_art: Optional[Path] = None

        if cover_art and cover_art.exists():
            replicate_art_to_opus(stage_flac, stage_opus)
            embed_art_temp = get_cache_dir() / f"embed_cover_{uuid.uuid4().hex[:8]}.jpg"
            embed_art = create_embedded_cover_art(cover_art, embed_art_temp, max_dimension=1000)
            embed_cover_art_flacs(c_flacs, embed_art)

        enc_ok = transcode_flacs_to_opus(
            c_flacs,
            stage_flac,
            stage_opus,
            embed_art or cover_art,
            progress_callback=effective_ui.on_transcode_progress,
        )
        if embed_art_temp and embed_art_temp.exists():
            embed_art_temp.unlink(missing_ok=True)

        if not enc_ok:
            return False, ["Opus transcoding failed during resume."]

        manifest.transition_to(WorkflowState.OPUS_ENCODED)

        apply_replaygain(stage_flac, "flac")
        apply_replaygain(stage_opus, "opus")
        manifest.transition_to(WorkflowState.REPLAYGAIN_APPLIED)

        if not verify_flac_integrity(c_flacs):
            return False, ["Candidate FLAC stream verification failed."]

        manifest.verification.flac_integrity_passed = True
        manifest.transition_to(WorkflowState.CANDIDATE_VERIFIED)

        # Ensure destination paths are resolved
        if not manifest.paths.final_flac_dir or not manifest.paths.final_opus_dir:
            output_root = manifest.paths.output_root or get_output_root()
            raw_stem = f"{manifest.metadata.artist} - {manifest.metadata.album}"
            stem = truncate_to_bytes(raw_stem, max_bytes=180)
            master_dir = output_root / truncate_to_bytes(stem, max_bytes=240)
            manifest.paths.final_flac_dir = master_dir / truncate_to_bytes(f"{stem} (FLAC)", max_bytes=220)
            manifest.paths.final_opus_dir = master_dir / truncate_to_bytes(f"{stem} (Opus)", max_bytes=220)
            self.store.save(manifest)

        tx_mgr = ArchiveTransactionManager(manifest, self.store)
        proceed, eff_overwrite, ts_suffix = tx_mgr.resolve_destination_conflict(
            overwrite=overwrite,
            ui=effective_ui,
        )
        if not proceed:
            return False, ["Publication cancelled or destination collision unresolvable."]

        if ts_suffix:
            self._apply_timestamp_suffix(manifest, ts_suffix)

        manifest.transition_to(WorkflowState.PUBLISHING)

        try:
            tx_mgr.execute_publication(stage_flac, stage_opus, overwrite=eff_overwrite)
        except Exception as exc:
            rb_ok = tx_mgr.rollback()
            manifest.transition_to(WorkflowState.PUBLISH_FAILED if rb_ok else WorkflowState.ROLLBACK_FAILED)
            self.store.save(manifest)
            return False, [f"Publication failed: {exc}"]

        manifest.transition_to(WorkflowState.COMMITTED)
        shutil.rmtree(manifest.paths.candidate_stage, ignore_errors=True)
        if manifest.paths.whipper_stage.exists():
            shutil.rmtree(manifest.paths.whipper_stage, ignore_errors=True)
        self.store.save(manifest)

        dest = manifest.paths.final_flac_dir
        final_dir = dest.parent if dest else (manifest.paths.output_root or get_output_root())
        published_art = find_cover_art(dest) if dest and dest.exists() else cover_art

        notify_rip_success(
            artist=manifest.metadata.artist,
            album=manifest.metadata.album,
            output_dir=final_dir,
            track_count=len(c_flacs),
            is_accurate=manifest.verification.is_accurate,
            cover_art=published_art,
        )

        return True, [f"Successfully published archive to {final_dir}"]
