"""Manifest reconciliation, post-crash state inspection, and orphaned stage cleanup."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional, Tuple
from whipit.config import get_cache_dir, get_output_root
from whipit.models import JobManifest, WorkflowState
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager


class RecoveryCoordinator:
    def __init__(self, store: ManifestStore):
        self.store = store

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

    def cleanup_orphaned_stages(self, output_root: Optional[Path] = None) -> List[str]:
        """
        Scans cache_dir and output_root for abandoned staging directories not owned by any active job.
        Reclaims stranded disk space.
        """
        actions = []
        all_jobs = self.store.list_all_jobs()
        active_job_ids = {j.job_id for j in all_jobs if self.store.is_job_active(j.job_id)}
        known_uncommitted_ids = {j.job_id for j in all_jobs if not j.transaction.committed}

        # 1. Clean cache_dir stage_* directories
        cache_dir = get_cache_dir()
        if cache_dir.exists():
            for d in cache_dir.glob("stage_*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace("stage_", "")
                if job_id not in active_job_ids and job_id not in known_uncommitted_ids:
                    shutil.rmtree(d, ignore_errors=True)
                    actions.append(f"Purged abandoned cache stage: {d.name}")

        # 2. Clean output_root .whipit-stage.* directories
        root = output_root or get_output_root()
        if root.exists():
            for d in root.glob(".whipit-stage.*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace(".whipit-stage.", "")
                if job_id not in active_job_ids and job_id not in known_uncommitted_ids:
                    shutil.rmtree(d, ignore_errors=True)
                    actions.append(f"Purged abandoned candidate stage: {d.name}")

        return actions

    def recover_job(self, job_id: str, discard_candidate: bool = False) -> Tuple[bool, List[str]]:
        """
        Safely rolls back partial publications and removes temporary stages only on success.
        If rollback fails, staging files are preserved to prevent data destruction.
        """
        if self.store.is_job_active(job_id):
            return False, [f"Job {job_id} is actively running in another process and cannot be recovered."]

        manifest = self.store.load(job_id)
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
