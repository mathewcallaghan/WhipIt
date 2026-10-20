"""Parallel Opus encoding using opusenc with metadata, art synchronization, and progress callbacks."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from whipit.config import truncate_to_bytes


def _encode_single_track(
    flac_file: Path,
    flac_root: Path,
    opus_root: Path,
    cover_art: Optional[Path],
    cancel_event: Optional[threading.Event] = None,
    active_procs: Optional[Dict[Path, subprocess.Popen]] = None,
    procs_lock: Optional[threading.Lock] = None,
) -> Tuple[bool, str]:
    if cancel_event and cancel_event.is_set():
        return False, "Cancelled"

    try:
        rel_path = flac_file.relative_to(flac_root)
    except ValueError:
        rel_path = Path(flac_file.name)

    out_opus = (opus_root / rel_path).with_suffix(".opus")
    out_opus.parent.mkdir(parents=True, exist_ok=True)

    temp_suffix = f".tmp.{os.getpid()}_{threading.get_ident()}.opus"
    base_name = truncate_to_bytes(out_opus.stem, max_bytes=255 - len(temp_suffix.encode("utf-8")))
    temp_opus = out_opus.parent / f"{base_name}{temp_suffix}"

    if out_opus.exists():
        return True, f"Skipped existing: {out_opus.name}"

    args = ["opusenc", "--quiet", "--bitrate", "192"]

    has_flac_picture = False
    try:
        res = subprocess.run(
            ["metaflac", "--list", "--block-type=PICTURE", str(flac_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        has_flac_picture = "METADATA block #" in res.stdout
    except Exception:
        pass

    if not has_flac_picture and cover_art and cover_art.exists():
        args.extend(["--picture", str(cover_art)])

    args.extend([str(flac_file), str(temp_opus)])

    if cancel_event and cancel_event.is_set():
        return False, "Cancelled"

    proc = None
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if active_procs is not None and procs_lock is not None:
            with procs_lock:
                active_procs[flac_file] = proc

        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=0.4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                if temp_opus.exists():
                    temp_opus.unlink(missing_ok=True)
                return False, f"Cancelled encoding {flac_file.name}"
            time.sleep(0.08)

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            if temp_opus.exists():
                temp_opus.unlink(missing_ok=True)
            return False, f"Failed encoding {flac_file.name}: {stderr.strip()}"

    finally:
        if active_procs is not None and procs_lock is not None and proc is not None:
            with procs_lock:
                active_procs.pop(flac_file, None)

    if cancel_event and cancel_event.is_set():
        if temp_opus.exists():
            temp_opus.unlink(missing_ok=True)
        return False, "Cancelled"

    try:
        os.replace(temp_opus, out_opus)
        flac_stat = flac_file.stat()
        os.utime(out_opus, (flac_stat.st_atime, flac_stat.st_mtime))
    except Exception as exc:
        if temp_opus.exists():
            temp_opus.unlink(missing_ok=True)
        return False, f"Failed publishing {out_opus.name}: {exc}"

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

    active_procs: Dict[Path, subprocess.Popen] = {}
    procs_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _encode_single_track,
                f,
                flac_root,
                opus_root,
                cover_art,
                cancel_event,
                active_procs,
                procs_lock,
            ): f
            for f in flac_files
        }

        not_done = set(futures.keys())

        while not_done:
            if cancel_event and cancel_event.is_set():
                for pending in not_done:
                    pending.cancel()
                with procs_lock:
                    for proc in list(active_procs.values()):
                        try:
                            proc.terminate()
                        except OSError:
                            pass
                return False

            done = {f for f in not_done if f.done()}
            if not done:
                if cancel_event:
                    cancel_event.wait(timeout=0.1)
                else:
                    time.sleep(0.1)
                continue

            for future in done:
                not_done.remove(future)
                track_file = futures[future]
                try:
                    ok, _ = future.result()
                except Exception:
                    ok = False

                completed_count += 1
                if not ok:
                    all_ok = False
                if progress_callback:
                    progress_callback(completed_count, total, track_file.name, ok)

    return all_ok
