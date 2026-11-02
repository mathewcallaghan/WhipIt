"""Service encapsulating storage preflight checks, filesystem capacity, and path layout verification."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional, Tuple

from whipit.models import StoragePreflightResult
from whipit.ui.base import LogLevel, UserInteractionPort

logger = logging.getLogger(__name__)


class StorageService:
    """Preflight filesystem verification, capacity checks, and root path validation."""

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
        res = StorageService.evaluate_disk_space(
            cache_dir=cache_dir,
            flac_root=flac_root,
            opus_root=opus_root,
            generate_opus=generate_opus,
            ui=ui,
            min_cache_bytes=min_cache_bytes,
            min_output_bytes=min_output_bytes,
        )
        return res.success, res.error_message

    @staticmethod
    def evaluate_disk_space(
        cache_dir: Path,
        flac_root: Path,
        opus_root: Optional[Path],
        generate_opus: bool,
        ui: UserInteractionPort,
        min_cache_bytes: int = 1_000_000_000,
        min_output_bytes: int = 1_500_000_000,
    ) -> StoragePreflightResult:
        """Evaluates filesystem disk usage and returns typed StoragePreflightResult."""
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
                    msg = (
                        f"Insufficient disk space on {flac_root}. "
                        f"Available: {free_gb:.2f} GB, Required peak: {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=output_usage.free,
                        peak_required_bytes=required,
                    )
            else:
                if cache_usage.free < min_cache_bytes:
                    free_gb = cache_usage.free / (1024 ** 3)
                    req_gb = min_cache_bytes / (1024 ** 3)
                    msg = (
                        f"Insufficient cache disk space on {cache_dir}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=output_usage.free,
                        peak_required_bytes=min_cache_bytes,
                    )
                if output_usage.free < min_output_bytes:
                    free_gb = output_usage.free / (1024 ** 3)
                    req_gb = min_output_bytes / (1024 ** 3)
                    msg = (
                        f"Insufficient output disk space on {flac_root}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=output_usage.free,
                        peak_required_bytes=min_output_bytes,
                    )

            if generate_opus and opus_root and opus_root != flac_root:
                opus_usage = shutil.disk_usage(opus_root)
                min_opus_bytes = 400_000_000
                if opus_usage.free < min_opus_bytes:
                    free_gb = opus_usage.free / (1024 ** 3)
                    req_gb = min_opus_bytes / (1024 ** 3)
                    msg = (
                        f"Insufficient output disk space on {opus_root}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=opus_usage.free,
                        peak_required_bytes=min_opus_bytes,
                    )

            return StoragePreflightResult(
                success=True,
                available_cache_bytes=cache_usage.free,
                available_output_bytes=output_usage.free,
                peak_required_bytes=min_cache_bytes + min_output_bytes,
            )
        except OSError as exc:
            ui.on_log(
                LogLevel.ERROR,
                f"Disk-space preflight failed for {flac_root}: {exc}",
            )
            return StoragePreflightResult(
                success=False,
                error_message=f"Cannot verify available disk space on {flac_root}: {exc}",
            )
        except Exception as exc:
            ui.on_log(
                LogLevel.ERROR,
                f"Unexpected disk-space preflight error: {exc!r}",
            )
            return StoragePreflightResult(
                success=False,
                error_message="Disk-space preflight failed; refusing to start extraction.",
            )
