"""Manifest reconciliation and post-crash state inspection."""

from __future__ import annotations

import shutil
from typing import List, Tuple
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

        if manifest.paths.candidate_stage.exists():
            return f"Incomplete Publication (Candidate present: {manifest.paths.candidate_stage})"

        if manifest.paths.whipper_stage.exists():
            return f"Incomplete Extraction (Cache present: {manifest.paths.whipper_stage})"

        return "Aborted prior to publication"

    def recover_job(self, job_id: str) -> Tuple[bool, List[str]]:
        """Rolls back partial publications, deletes temporary stages, and removes the job."""
        manifest = self.store.load(job_id)
        mgr = ArchiveTransactionManager(manifest, self.store)
        success = mgr.rollback()
        actions: List[str] = []

        if success:
            actions.append("Rolled back transactional changes")

        # 1. Clean up candidate staging directory (e.g. ~/.whipit-stage.<uuid>)
        if manifest.paths.candidate_stage.exists():
            try:
                shutil.rmtree(manifest.paths.candidate_stage, ignore_errors=True)
                actions.append(f"Removed staging directory: {manifest.paths.candidate_stage}")
            except Exception as e:
                actions.append(f"Failed removing staging directory: {e}")

        # 2. Clean up whipper extraction cache (e.g. ~/.cache/whipit/stage_<uuid>)
        if manifest.paths.whipper_stage.exists():
            try:
                shutil.rmtree(manifest.paths.whipper_stage, ignore_errors=True)
                actions.append(f"Removed cache directory: {manifest.paths.whipper_stage}")
            except Exception as e:
                actions.append(f"Failed removing cache directory: {e}")

        # 3. Remove or finalize manifest
        if success:
            self.store.delete(job_id)
            actions.append("Cleared job manifest")
        else:
            manifest.transition_to(WorkflowState.ROLLBACK_FAILED)
            self.store.save(manifest)

        return success, actions
