"""Atomic persistent storage for job manifests."""

from __future__ import annotations

import os
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
        """Removes the job manifest file from disk."""
        target = self.manifest_path(job_id)
        if target.exists():
            try:
                target.unlink()
                return True
            except Exception:
                return False
        return False

    def list_uncommitted_jobs(self) -> List[JobManifest]:
        uncommitted = []
        for path in self.directory.glob("*.json"):
            try:
                manifest = JobManifest.model_validate_json(path.read_text(encoding="utf-8"))
                if not manifest.transaction.committed and manifest.state != WorkflowState.ABORTED:
                    uncommitted.append(manifest)
            except Exception:
                continue
        return uncommitted
