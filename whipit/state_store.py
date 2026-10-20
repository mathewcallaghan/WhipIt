"""Atomic persistent storage for job manifests and process locks."""

from __future__ import annotations

import datetime
import fcntl
import os
import shutil
from pathlib import Path
from typing import List, Optional
from whipit.config import get_log_dir
from whipit.models import JobManifest, WorkflowState


class ManifestStore:
    def __init__(self, directory: Optional[Path] = None):
        self.directory = directory or (get_log_dir() / "jobs")
        self.directory.mkdir(parents=True, exist_ok=True)

    def manifest_path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.json"

    def lock_path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.lock"

    def acquire_job_lock(self, job_id: str) -> Optional[int]:
        """Acquires a non-blocking kernel process lock (flock) for an active job."""
        lp = self.lock_path(job_id)
        try:
            fd = os.open(lp, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
            return fd
        except (BlockingIOError, OSError):
            return None

    def release_job_lock(self, job_id: str, fd: Optional[int] = None) -> None:
        """Releases the process lock and removes the lock file."""
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
        lp = self.lock_path(job_id)
        if lp.exists():
            try:
                lp.unlink(missing_ok=True)
            except OSError:
                pass

    def is_job_active(self, job_id: str) -> bool:
        """Checks if another live process holds the lock for this job."""
        lp = self.lock_path(job_id)
        if not lp.exists():
            return False
        try:
            fd = os.open(lp, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except (BlockingIOError, OSError):
                return True
            finally:
                os.close(fd)
        except OSError:
            return False

    def save(self, manifest: JobManifest) -> None:
        target = self.manifest_path(manifest.job_id)
        tmp = target.with_suffix(f".tmp.{os.getpid()}")
        payload = manifest.model_dump_json(indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        dir_fd = os.open(self.directory, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def load(self, job_id: str) -> JobManifest:
        target = self.manifest_path(job_id)
        if not target.exists():
            raise FileNotFoundError(f"Manifest not found for job: {job_id}")
        content = target.read_text(encoding="utf-8")
        return JobManifest.model_validate_json(content)

    def delete(self, job_id: str) -> bool:
        """Removes the job manifest file and lock file from disk."""
        lp = self.lock_path(job_id)
        if lp.exists():
            try:
                lp.unlink(missing_ok=True)
            except OSError:
                pass
        target = self.manifest_path(job_id)
        if target.exists():
            try:
                target.unlink()
                return True
            except Exception:
                return False
        return False

    def list_all_jobs(self) -> List[JobManifest]:
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                manifest = JobManifest.model_validate_json(path.read_text(encoding="utf-8"))
                jobs.append(manifest)
            except Exception:
                continue
        return jobs

    def list_uncommitted_jobs(self) -> List[JobManifest]:
        uncommitted = []
        for path in self.directory.glob("*.json"):
            try:
                manifest = JobManifest.model_validate_json(path.read_text(encoding="utf-8"))
                if not manifest.transaction.committed:
                    has_flacs = (
                        manifest.paths.candidate_stage.exists()
                        and (manifest.paths.candidate_stage / "flac").exists()
                        and any((manifest.paths.candidate_stage / "flac").glob("*.flac"))
                    )
                    if manifest.state != WorkflowState.ABORTED or has_flacs:
                        uncommitted.append(manifest)
            except Exception:
                continue
        return uncommitted

    def prune_committed_jobs(self, max_keep: int = 10, max_age_days: int = 7) -> int:
        """
        Removes committed job manifests older than max_age_days or exceeding max_keep.
        Also sweeps any stranded .replaced.* backups left by crashes after commitment.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=max_age_days)
        pruned_count = 0

        committed_jobs = [j for j in self.list_all_jobs() if j.transaction.committed]
        committed_jobs.sort(key=lambda j: j.updated_at)

        for job in committed_jobs:
            tx = job.transaction
            changed = False
            if tx.flac_backup_path and tx.flac_backup_path.exists():
                shutil.rmtree(tx.flac_backup_path, ignore_errors=True)
                tx.flac_backed_up = False
                tx.flac_backup_path = None
                changed = True
            if tx.opus_backup_path and tx.opus_backup_path.exists():
                shutil.rmtree(tx.opus_backup_path, ignore_errors=True)
                tx.opus_backed_up = False
                tx.opus_backup_path = None
                changed = True
            if changed:
                self.save(job)

        excess_count = max(0, len(committed_jobs) - max_keep)
        for idx, job in enumerate(committed_jobs):
            is_excess = idx < excess_count
            is_expired = job.updated_at < cutoff
            if is_excess or is_expired:
                self.delete(job.job_id)
                pruned_count += 1

        return pruned_count
