"""Domain models, enumerations, and persistent state schemas."""

from __future__ import annotations

import datetime
import os
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator

from whipit.config import get_cache_dir, get_log_dir


class WhipItError(Exception):
    """Base exception for all domain errors in WhipIt."""


class InvariantViolationError(WhipItError, RuntimeError, ValueError):
    """Raised when an internal state invariant, path boundary, or precondition is violated."""


class StorageIOError(WhipItError, OSError):
    """Raised on critical storage, disk full (ENOSPC), or filesystem I/O failures."""


class WorkflowCancelledError(WhipItError):
    """Raised when a workflow or worker task is cancelled by user request."""


class NetworkTransientError(WhipItError):
    """Raised on temporary network timeouts, HTTP 503/429, or connection drops."""


class OptionalFeatureError(WhipItError):
    """Raised when an optional non-fatal integration (artwork, ReplayGain) fails."""


def require_within_root(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"Refusing symlink path: {path}")

    if not root.exists():
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    root_resolved = root.resolve(strict=True)
    candidate = path.resolve(strict=False)

    try:
        rel = candidate.relative_to(root_resolved)
        if rel == Path("."):
            raise ValueError(f"Path cannot be the root itself: {path}")
    except ValueError as exc:
        raise ValueError(f"Refusing filesystem path outside approved root: {path}") from exc

    return candidate


class WorkflowState(str, Enum):
    DISC_DETECTED = "DISC_DETECTED"
    RIPPING = "RIPPING"
    EXTRACTION_HALTED = "EXTRACTION_HALTED"
    RIP_EXTRACTED = "RIP_EXTRACTED"
    RIP_VALIDATED = "RIP_VALIDATED"
    CANDIDATE_STAGED = "CANDIDATE_STAGED"
    OPUS_ENCODED = "OPUS_ENCODED"
    REPLAYGAIN_APPLIED = "REPLAYGAIN_APPLIED"
    CANDIDATE_VERIFIED = "CANDIDATE_VERIFIED"
    PUBLISHING = "PUBLISHING"
    COMMITTED = "COMMITTED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    ABORTED = "ABORTED"


class WorkflowResult(str, Enum):
    SUCCESS = "SUCCESS"
    COMMITTED_PARTIAL = "COMMITTED_PARTIAL"
    ABORTED = "ABORTED"

    def __bool__(self) -> bool:
        return self != WorkflowResult.ABORTED

    @property
    def is_committed(self) -> bool:
        return self in (WorkflowResult.SUCCESS, WorkflowResult.COMMITTED_PARTIAL)

    def __eq__(self, other: object) -> bool:
        if other is True:
            return self in (WorkflowResult.SUCCESS, WorkflowResult.COMMITTED_PARTIAL)
        if other is False:
            return self == WorkflowResult.ABORTED
        return super().__eq__(other)


class TrackInfo(BaseModel):
    number: int
    title: str
    duration: Optional[str] = None


class ReleaseOption(BaseModel):
    mbid: str
    artist: str = "Unknown Artist"
    title: str = "Unknown Album"
    country: Optional[str] = None
    cat_no: Optional[str] = None
    barcode: Optional[str] = None
    year: Optional[str] = None
    duration: Optional[str] = None
    tracks: List[TrackInfo] = Field(default_factory=list)


class DeviceInfo(BaseModel):
    path: str
    hardware_id: str
    read_offset: Optional[int] = None
    speed: Optional[int] = None


class AlbumMetadata(BaseModel):
    artist: str = "Unknown Artist"
    album: str = "Unknown Album"
    disc_number: Optional[int] = None
    total_discs: Optional[int] = None
    album_stem: str = "Unknown Artist - Unknown Album"
    musicbrainz_id: Optional[str] = None
    musicbrainz_url: Optional[str] = None
    musicbrainz_lookup_url: Optional[str] = None
    discogs_url: Optional[str] = None


class TrackValidationInfo(BaseModel):
    expected_count: int = 0
    extracted_count: int = 0
    sequence_valid: bool = False
    missing_tracks: List[int] = Field(default_factory=list)


class VerificationReport(BaseModel):
    health_status: str = "Unknown"
    accuraterip_summary: str = "Unknown"
    is_accurate: bool = False
    is_unverified: bool = False
    flac_integrity_passed: bool = False


class PathLayout(BaseModel):
    output_root: Path
    whipper_stage: Path
    candidate_stage: Path
    final_flac_dir: Optional[Path] = None
    final_opus_dir: Optional[Path] = None

    @model_validator(mode="after")
    def _validate_traversal(self) -> "PathLayout":
        for p in (self.output_root, self.whipper_stage, self.candidate_stage, self.final_flac_dir, self.final_opus_dir):
            if p is not None and any(part == ".." for part in p.parts):
                raise ValueError(f"Path traversal detected in path: {p}")
        return self


class TransactionJournal(BaseModel):
    flac_backed_up: bool = False
    opus_backed_up: bool = False
    flac_published: bool = False
    opus_published: bool = False
    committed: bool = False
    flac_backup_path: Optional[Path] = None
    opus_backup_path: Optional[Path] = None

    @model_validator(mode="after")
    def _validate_traversal(self) -> "TransactionJournal":
        for bp in (self.flac_backup_path, self.opus_backup_path):
            if bp is not None and any(part == ".." for part in bp.parts):
                raise ValueError(f"Path traversal detected in backup path: {bp}")
        return self


CURRENT_SCHEMA_VERSION: int = 1


class JobManifest(BaseModel):
    schema_version: int = CURRENT_SCHEMA_VERSION
    job_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    pid: int = Field(default_factory=os.getpid)
    created_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at: datetime.datetime = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    state: WorkflowState = WorkflowState.DISC_DETECTED
    device: DeviceInfo
    metadata: AlbumMetadata = Field(default_factory=AlbumMetadata)
    tracks: TrackValidationInfo = Field(default_factory=TrackValidationInfo)
    verification: VerificationReport = Field(default_factory=VerificationReport)
    paths: PathLayout
    transaction: TransactionJournal = Field(default_factory=TransactionJournal)
    error_log: Optional[Path] = None
    full_log: Optional[Path] = None

    def validate_paths(self) -> None:
        if self.paths.candidate_stage.name != f".whipit-stage.{self.job_id}":
            raise ValueError("candidate_stage does not match job id")
        if self.paths.whipper_stage.name != f"stage_{self.job_id}":
            raise ValueError("whipper_stage does not match job id")

        root = self.paths.output_root.resolve(strict=False)
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise ValueError("output_root is too broad")

        cache_root = get_cache_dir()
        log_root = get_log_dir()

        require_within_root(self.paths.whipper_stage, cache_root)
        if self.paths.whipper_stage.resolve(strict=False).parent != cache_root.resolve(strict=False):
            raise ValueError("whipper_stage is not directly under the cache dir")

        require_within_root(self.paths.candidate_stage, root)
        if self.paths.candidate_stage.resolve(strict=False).parent != root:
            raise ValueError("candidate_stage is not directly under output_root")

        for p, tag in ((self.paths.final_flac_dir, "(FLAC)"), (self.paths.final_opus_dir, "(Opus)")):
            if p is None:
                continue
            require_within_root(p, root)
            try:
                parts = p.resolve(strict=False).relative_to(root).parts
            except ValueError:
                raise ValueError("final path escapes output_root")
            base = parts[-2] if (parts and parts[-1].startswith("Disc ")) else (parts[-1] if parts else "")
            if not (2 <= len(parts) <= 3) or not base.endswith(tag):
                raise ValueError("final path does not look like a WhipIt archive directory")

        for bp in (self.transaction.flac_backup_path, self.transaction.opus_backup_path):
            if bp is not None:
                require_within_root(bp, root)
                if ".replaced." not in bp.name or bp.resolve(strict=False).parent.parent != root:
                    raise ValueError("unexpected backup path")

        for lp in (self.error_log, self.full_log):
            if lp is not None:
                require_within_root(lp, log_root)

    @model_validator(mode="after")
    def _check_layout(self) -> "JobManifest":
        self.validate_paths()
        return self

    def transition_to(self, new_state: WorkflowState) -> None:
        self.state = new_state
        self.updated_at = datetime.datetime.now(datetime.timezone.utc)
