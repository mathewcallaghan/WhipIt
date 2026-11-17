"""Service encapsulating storage preflight checks, filesystem capacity, and path layout verification."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

            checks: List[Tuple[str, Path, int]] = [
                ("cache", cache_dir, min_cache_bytes),
                ("output", flac_root, min_output_bytes),
            ]
            if generate_opus and opus_root and opus_root.resolve(strict=False) != flac_root.resolve(strict=False):
                checks.append(("opus", opus_root, 400_000_000))

            dev_groups: Dict[int, Tuple[Path, int, List[str]]] = {}
            unstat_checks: List[Tuple[str, Path, int]] = []
            for label, path, req_bytes in checks:
                try:
                    dev_id = path.stat().st_dev
                    if dev_id in dev_groups:
                        rep_path, total_req, labels = dev_groups[dev_id]
                        dev_groups[dev_id] = (rep_path, total_req + req_bytes, [*labels, label])
                    else:
                        dev_groups[dev_id] = (path, req_bytes, [label])
                except OSError:
                    unstat_checks.append((label, path, req_bytes))

            cache_usage = shutil.disk_usage(cache_dir)
            output_usage = shutil.disk_usage(flac_root)
            total_peak = sum(req for _, _, req in checks)

            for rep_path, req_bytes, labels in dev_groups.values():
                usage = shutil.disk_usage(rep_path)
                if usage.free < req_bytes:
                    free_gb = usage.free / (1024 ** 3)
                    req_gb = req_bytes / (1024 ** 3)
                    scope = "peak" if len(labels) > 1 else labels[0]
                    msg = (
                        f"Insufficient disk space on {rep_path}. "
                        f"Available: {free_gb:.2f} GB, Required ({scope}): {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=output_usage.free,
                        peak_required_bytes=req_bytes,
                    )

            for _label, path, req_bytes in unstat_checks:
                usage = shutil.disk_usage(path)
                if usage.free < req_bytes:
                    free_gb = usage.free / (1024 ** 3)
                    req_gb = req_bytes / (1024 ** 3)
                    msg = (
                        f"Insufficient disk space on {path}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                    return StoragePreflightResult(
                        success=False,
                        error_message=msg,
                        available_cache_bytes=cache_usage.free,
                        available_output_bytes=output_usage.free,
                        peak_required_bytes=req_bytes,
                    )

            return StoragePreflightResult(
                success=True,
                available_cache_bytes=cache_usage.free,
                available_output_bytes=output_usage.free,
                peak_required_bytes=total_peak,
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
