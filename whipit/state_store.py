"""Atomic persistent storage for job manifests and process locks."""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import List, Optional, Set, Tuple
from whipit.config import get_log_dir
from whipit.models import CURRENT_SCHEMA_VERSION, JobManifest, WorkflowState

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _check_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"Invalid job id: {job_id!r}")
    return job_id


class ManifestStore:
    def __init__(self, directory: Optional[Path] = None):
        self.directory = directory or (get_log_dir() / "jobs")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.quarantine_directory = self.directory / "quarantine"

    def manifest_path(self, job_id: str) -> Path:
        return self.directory / f"{_check_id(job_id)}.json"

    def lock_path(self, job_id: str) -> Path:
        return self.directory / f"{_check_id(job_id)}.lock"

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

    def quarantine_manifest(self, path: Path, reason: str = "") -> Optional[Path]:
        """Moves an unparseable or schema-incompatible manifest to the quarantine directory."""
        if not path.exists():
            return None
        self.quarantine_directory.mkdir(parents=True, exist_ok=True)
        dest = self.quarantine_directory / path.name
        if dest.exists() and dest != path:
            dest = self.quarantine_directory / f"{path.stem}_{int(time.time())}{path.suffix}"
        try:
            shutil.move(str(path), str(dest))
            reason_file = dest.with_suffix(dest.suffix + ".reason")
            reason_file.write_text(reason + "\n", encoding="utf-8")
            return dest
        except Exception:
            return None

    def list_quarantined_job_ids(self) -> Set[str]:
        if not self.quarantine_directory.exists():
            return set()
        ids: Set[str] = set()
        for p in self.quarantine_directory.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "job_id" in data and isinstance(data["job_id"], str) and _JOB_ID_RE.fullmatch(data["job_id"]):
                    ids.add(data["job_id"])
                    continue
            except Exception:
                pass
            base = p.stem.split("_")[0]
            if base and _JOB_ID_RE.fullmatch(base):
                ids.add(base)
        return ids

    def _load_manifest_file(self, path: Path) -> Optional[JobManifest]:
        try:
            content = path.read_text(encoding="utf-8")
            raw_data = json.loads(content)
            version = raw_data.get("schema_version", 1)
            if not isinstance(version, int) or version > CURRENT_SCHEMA_VERSION:
                self.quarantine_manifest(
                    path,
                    reason=f"Unsupported schema version: {version} (max supported: {CURRENT_SCHEMA_VERSION})",
                )
                return None
            manifest = JobManifest.model_validate(raw_data)
            if manifest.job_id != path.stem:
                self.quarantine_manifest(
                    path,
                    reason=f"job_id '{manifest.job_id}' does not match filename '{path.stem}'",
                )
                return None
            return manifest
        except Exception as exc:
            self.quarantine_manifest(path, reason=f"Parse or validation error: {exc}")
            return None

    def load(self, job_id: str) -> JobManifest:
        target = self.manifest_path(job_id)
        if not target.exists():
            raise FileNotFoundError(f"Manifest not found for job: {job_id}")
        manifest = self._load_manifest_file(target)
        if manifest is None:
            raise ValueError(f"Manifest for job {job_id} could not be validated and was quarantined")
        return manifest

    def delete(self, job_id: str) -> bool:
        """Removes the job manifest file, lock file, and associated logs from disk."""
        valid_id = _check_id(job_id)
        lp = self.lock_path(valid_id)
        if lp.exists():
            try:
                lp.unlink(missing_ok=True)
            except OSError:
                pass

        for suffix in ("_full.log", "_error.log"):
            log_file = get_log_dir() / f"{valid_id}{suffix}"
            if log_file.exists():
                try:
                    log_file.unlink(missing_ok=True)
                except OSError:
                    pass

        target = self.manifest_path(valid_id)
        if target.exists():
            try:
                target.unlink()
                return True
            except Exception:
                return False
        return False

    def list_all_jobs(self) -> List[JobManifest]:
        jobs = []
        for path in sorted(self.directory.glob("*.json")):
            manifest = self._load_manifest_file(path)
            if manifest is not None:
                jobs.append(manifest)
        return jobs

    def list_uncommitted_jobs(self) -> List[JobManifest]:
        uncommitted = []
        for path in sorted(self.directory.glob("*.json")):
            manifest = self._load_manifest_file(path)
            if manifest is not None and not manifest.transaction.committed:
                has_flacs = (
                    manifest.paths.candidate_stage.exists()
                    and (manifest.paths.candidate_stage / "flac").exists()
                    and any((manifest.paths.candidate_stage / "flac").glob("*.flac"))
                )
                if manifest.state != WorkflowState.ABORTED or has_flacs:
                    uncommitted.append(manifest)
        return uncommitted

    def prune_committed_jobs(self, max_keep: int = 10, max_age_days: int = 7) -> int:
        """
        Removes committed job manifests older than max_age_days or exceeding max_keep.
        Also sweeps any stranded .replaced.* backups and orphaned execution logs.
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

        # Sweep orphaned log files in get_log_dir()
        log_dir = get_log_dir()
        if log_dir.exists():
            active_manifest_ids = {p.stem for p in self.directory.glob("*.json")}
            for log_file in log_dir.glob("*_full.log"):
                jid = log_file.name[:-9]
                if jid not in active_manifest_ids or log_file.stat().st_mtime < cutoff.timestamp():
                    try:
                        log_file.unlink(missing_ok=True)
                    except OSError:
                        pass
            for log_file in log_dir.glob("*_error.log"):
                jid = log_file.name[:-10]
                if jid not in active_manifest_ids or log_file.stat().st_mtime < cutoff.timestamp():
                    try:
                        log_file.unlink(missing_ok=True)
                    except OSError:
                        pass

        return pruned_count
