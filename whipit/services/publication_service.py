"""Service encapsulating disk preflight checks, path resolution, transaction publishing, and session cleanup."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

from whipit.models import AlbumMetadata, JobManifest, PublicationExecutionResult
from whipit.naming import format_album_stem, truncate_to_bytes
from whipit.state_store import ManifestStore
from whipit.transaction import (
    ArchiveTransactionManager,
    TransactionError,
)
from whipit.ui.base import LogLevel, UserInteractionPort

logger = logging.getLogger(__name__)


def _wf() -> Any:
    """Dynamically resolves whipit.workflow to honor test monkeypatching across the legacy interface."""
    return sys.modules.get("whipit.workflow", sys.modules[__name__])


class PublicationService:
    """Handles storage preflight checks, archival destination calculation, atomic publication, and aborted session cleanup."""

    @staticmethod
    def check_disk_space(
        cache_dir: Path,
        flac_root: Path,
        opus_root: Optional[Path],
        generate_opus: bool,
        ui: UserInteractionPort,
        min_cache_bytes: int = 1_000_000_000,
        min_output_bytes: int = 1_500_000_000,
    ) -> Tuple[bool, Optional[str]]:
        """Preflight check ensuring sufficient free disk space exists before ripping starts."""
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            flac_root.mkdir(parents=True, exist_ok=True)
            if generate_opus and opus_root:
                opus_root.mkdir(parents=True, exist_ok=True)

            cache_usage = shutil.disk_usage(cache_dir)
            output_usage = shutil.disk_usage(flac_root)

            same_fs = False
            try:
                same_fs = cache_dir.stat().st_dev == flac_root.stat().st_dev
            except Exception:
                pass

            if same_fs:
                required = min_cache_bytes + min_output_bytes
                if cache_usage.free < required:
                    free_gb = cache_usage.free / (1024 ** 3)
                    req_gb = required / (1024 ** 3)
                    return False, (
                        f"Insufficient disk space on {flac_root}. "
                        f"Available: {free_gb:.2f} GB, Required peak: {req_gb:.2f} GB."
                    )
            else:
                if cache_usage.free < min_cache_bytes:
                    free_gb = cache_usage.free / (1024 ** 3)
                    req_gb = min_cache_bytes / (1024 ** 3)
                    return False, (
                        f"Insufficient cache disk space on {cache_dir}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                if output_usage.free < min_output_bytes:
                    free_gb = output_usage.free / (1024 ** 3)
                    req_gb = min_output_bytes / (1024 ** 3)
                    return False, (
                        f"Insufficient output disk space on {flac_root}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )

            if generate_opus and opus_root and opus_root != flac_root:
                opus_usage = shutil.disk_usage(opus_root)
                min_opus_bytes = 400_000_000
                if opus_usage.free < min_opus_bytes:
                    free_gb = opus_usage.free / (1024 ** 3)
                    req_gb = min_opus_bytes / (1024 ** 3)
                    return False, (
                        f"Insufficient output disk space on {opus_root}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )

            return True, None
        except OSError as exc:
            ui.on_log(
                LogLevel.ERROR,
                f"Disk-space preflight failed for {flac_root}: {exc}",
            )
            return False, f"Cannot verify available disk space on {flac_root}: {exc}"
        except Exception as exc:
            ui.on_log(
                LogLevel.ERROR,
                f"Unexpected disk-space preflight error: {exc!r}",
            )
            return False, "Disk-space preflight failed; refusing to start extraction."

    @staticmethod
    def resolve_paths(
        manifest: JobManifest,
        metadata: AlbumMetadata,
        flac_root: Path,
        opus_root: Optional[Path],
        generate_opus: bool,
        partial: bool = False,
        timestamp_suffix: str = "",
        store: Optional[ManifestStore] = None,
    ) -> None:
        """Calculates final FLAC and Opus archive destination paths."""
        stem = format_album_stem(metadata.artist, metadata.album)

        if partial:
            suffix = f" (Partial {timestamp_suffix})" if timestamp_suffix else " (Partial)"
        elif timestamp_suffix:
            suffix = f" ({timestamp_suffix})"
        else:
            suffix = ""

        master_name = truncate_to_bytes(f"{stem}{suffix}", max_bytes=240)

        shared_root = bool(generate_opus and opus_root and flac_root.resolve() == opus_root.resolve())

        if shared_root:
            flac_name = truncate_to_bytes(f"{master_name} (FLAC)", max_bytes=240)
            flac_dir = flac_root / flac_name
            opus_name = truncate_to_bytes(f"{master_name} (Opus)", max_bytes=240)
            opus_dir = opus_root / opus_name
        else:
            flac_dir = flac_root / master_name
            opus_dir = (opus_root / master_name) if (generate_opus and opus_root) else None

        is_multi = metadata.disc_number is not None and (
            metadata.disc_number > 1 or (metadata.total_discs is not None and metadata.total_discs > 1)
        )
        if is_multi and metadata.disc_number:
            flac_dir = flac_dir / f"Disc {metadata.disc_number}"
            if opus_dir:
                opus_dir = opus_dir / f"Disc {metadata.disc_number}"

        manifest.paths.final_flac_dir = flac_dir
        manifest.paths.final_opus_dir = opus_dir
        if store:
            store.save(manifest)

    @staticmethod
    def has_staged_flacs(candidate_stage: Path) -> bool:
        stage_flac = candidate_stage / "flac"
        return stage_flac.exists() and any(stage_flac.glob("*.flac"))

    @staticmethod
    def has_cached_flacs(whipper_stage: Path) -> bool:
        return whipper_stage.exists() and any(whipper_stage.rglob("*.flac"))

    def cleanup_aborted_session(
        self,
        manifest: JobManifest,
        store: ManifestStore,
        whipper_stage: Path,
        candidate_stage: Path,
        candidate_stage_opus: Optional[Path],
        ui: UserInteractionPort,
        job_id: str,
    ) -> None:
        """Preserves candidate stage or extraction cache if extracted FLACs exist; purges only unrecoverable sessions."""
        try:
            if self.has_staged_flacs(candidate_stage):
                if whipper_stage.exists():
                    shutil.rmtree(whipper_stage, ignore_errors=True)
                ui.on_log(
                    LogLevel.WARN,
                    f"Preserving extracted FLAC candidate for recovery at: {candidate_stage}",
                )
                store.save(manifest)
                return

            if self.has_cached_flacs(whipper_stage):
                if candidate_stage.exists():
                    shutil.rmtree(candidate_stage, ignore_errors=True)
                if candidate_stage_opus and candidate_stage_opus.exists():
                    shutil.rmtree(candidate_stage_opus, ignore_errors=True)
                ui.on_log(
                    LogLevel.WARN,
                    f"Preserving extracted FLAC cache for recovery at: {whipper_stage}",
                )
                store.save(manifest)
                return

            if whipper_stage.exists():
                shutil.rmtree(whipper_stage, ignore_errors=True)
            if candidate_stage.exists():
                shutil.rmtree(candidate_stage, ignore_errors=True)
            if candidate_stage_opus and candidate_stage_opus.exists():
                shutil.rmtree(candidate_stage_opus, ignore_errors=True)
            store.delete(job_id, delete_logs=False)
        except Exception as exc:
            logger.warning("Error during session cleanup for job %s: %s", job_id, exc)

    def resolve_conflict(
        self,
        manifest: JobManifest,
        store: ManifestStore,
        overwrite: bool,
        ui: UserInteractionPort,
    ) -> Tuple[bool, bool, Optional[str]]:
        """Resolves destination collisions via UI prompts or existing archive downgrade checks."""
        wf = _wf()
        cls_atm = getattr(wf, "ArchiveTransactionManager", ArchiveTransactionManager)
        tx_mgr = cls_atm(manifest, store)
        return tx_mgr.resolve_destination_conflict(overwrite=overwrite, ui=ui)

    def publish(
        self,
        manifest: JobManifest,
        store: ManifestStore,
        stage_flac: Path,
        stage_opus: Optional[Path],
        overwrite: bool,
    ) -> PublicationExecutionResult:
        """Executes transactional move of staged audio into final archival destinations."""
        wf = _wf()
        cls_atm = getattr(wf, "ArchiveTransactionManager", ArchiveTransactionManager)
        tx_mgr = cls_atm(manifest, store)

        try:
            tx_mgr.execute_publication(stage_flac, stage_opus, overwrite=overwrite)
            return PublicationExecutionResult(
                success=True,
                committed=True,
                rollback_succeeded=True,
                final_flac_dir=manifest.paths.final_flac_dir,
                final_opus_dir=manifest.paths.final_opus_dir,
            )
        except TransactionError as e:
            return PublicationExecutionResult(
                success=False,
                committed=False,
                rollback_succeeded=e.rollback_succeeded,
                error_message=str(e),
            )
        except Exception as e:
            rb_ok = tx_mgr.rollback()
            return PublicationExecutionResult(
                success=False,
                committed=False,
                rollback_succeeded=rb_ok,
                error_message=str(e),
            )
