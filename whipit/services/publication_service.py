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
from whipit.ui.base import UserInteractionPort

logger = logging.getLogger(__name__)


def _wf() -> Any:
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
        from whipit.services.storage_service import StorageService
        return StorageService.check_disk_space(
            cache_dir=cache_dir,
            flac_root=flac_root,
            opus_root=opus_root,
            generate_opus=generate_opus,
            ui=ui,
            min_cache_bytes=min_cache_bytes,
            min_output_bytes=min_output_bytes,
        )

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
        from whipit.services.recovery_service import RecoveryService
        return RecoveryService.has_staged_flacs(candidate_stage)

    @staticmethod
    def has_cached_flacs(whipper_stage: Path) -> bool:
        from whipit.services.recovery_service import RecoveryService
        return RecoveryService.has_cached_flacs(whipper_stage)

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
        from whipit.services.recovery_service import RecoveryService
        RecoveryService.cleanup_aborted_session(
            manifest=manifest,
            store=store,
            whipper_stage=whipper_stage,
            candidate_stage=candidate_stage,
            candidate_stage_opus=candidate_stage_opus,
            ui=ui,
            job_id=job_id,
        )

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
