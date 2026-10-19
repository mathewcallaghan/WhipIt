"""Parallel Opus encoding using opusenc with metadata, art synchronization, and progress callbacks."""

from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def _encode_single_track(
    flac_file: Path,
    flac_root: Path,
    opus_root: Path,
    cover_art: Optional[Path],
) -> Tuple[bool, str]:
    rel_path = flac_file.relative_to(flac_root)
    out_opus = (opus_root / rel_path).with_suffix(".opus")
    out_opus.parent.mkdir(parents=True, exist_ok=True)
    temp_opus = out_opus.with_suffix(f".opus.tmp.{os.getpid()}")

    if out_opus.exists():
        return True, f"Skipped existing: {out_opus.name}"

    args = ["opusenc", "--quiet", "--bitrate", "192"]

    res = subprocess.run(["metaflac", "--list", "--block-type=PICTURE", str(flac_file)], capture_output=True, text=True)
    has_flac_picture = "METADATA block #" in res.stdout

    if not has_flac_picture and cover_art and cover_art.exists():
        args.extend(["--picture", str(cover_art)])

    args.extend([str(flac_file), str(temp_opus)])

    run_res = subprocess.run(args, capture_output=True, text=True)
    if run_res.returncode != 0:
        if temp_opus.exists():
            temp_opus.unlink(missing_ok=True)
        return False, f"Failed encoding {flac_file.name}: {run_res.stderr}"

    os.replace(temp_opus, out_opus)
    try:
        flac_stat = flac_file.stat()
        os.utime(out_opus, (flac_stat.st_atime, flac_stat.st_mtime))
    except Exception:
        pass

    return True, f"Encoded: {out_opus.name}"


def transcode_flacs_to_opus(
    flac_files: List[Path],
    flac_root: Path,
    opus_root: Path,
    cover_art: Optional[Path],
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int, str, bool], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    if not flac_files:
        return True

    total = len(flac_files)
    workers = max_workers or max(1, os.cpu_count() or 4)
    all_ok = True
    completed_count = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_encode_single_track, f, flac_root, opus_root, cover_art): f
            for f in flac_files
        }
        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                for pending in futures:
                    pending.cancel()
                return False

            track_file = futures[future]
            ok, _ = future.result()
            completed_count += 1
            if not ok:
                all_ok = False
            if progress_callback:
                progress_callback(completed_count, total, track_file.name, ok)

    return all_ok
