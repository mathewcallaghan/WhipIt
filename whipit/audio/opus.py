"""Parallel Opus encoding using opusenc with metadata, art synchronization, and progress callbacks."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from whipit.config import truncate_to_bytes

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 4
MAX_WORKERS = 8
MAX_ENCODER_LOG_BYTES = 1024 * 1024  # 1 MiB bound for child diagnostic output


def stop_process(
    proc: subprocess.Popen,
    grace_seconds: float = 2.0,
) -> None:
    """Terminates process group, reaps exit code, and ensures no lingering subprocess."""
    if proc.poll() is not None:
        try:
            proc.wait()
        except OSError:
            pass
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=grace_seconds)
        except (subprocess.TimeoutExpired, OSError):
            pass


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

    proc: Optional[subprocess.Popen] = None
    was_cancelled = False
    encode_failed = False
    fail_diagnostic = ""

    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as log:
            proc = subprocess.Popen(
                args,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            if active_procs is not None and procs_lock is not None:
                with procs_lock:
                    active_procs[flac_file] = proc

            while proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    was_cancelled = True
                    stop_process(proc, grace_seconds=2.0)
                    break
                try:
                    if os.fstat(log.fileno()).st_size > MAX_ENCODER_LOG_BYTES:
                        encode_failed = True
                        stop_process(proc, grace_seconds=1.0)
                        fail_diagnostic = (
                            f"Encoder log exceeded maximum allowed size ({MAX_ENCODER_LOG_BYTES} bytes)"
                        )
                        break
                except OSError:
                    pass
                time.sleep(0.08)

            if cancel_event and cancel_event.is_set():
                was_cancelled = True

            if not was_cancelled and not encode_failed and proc.returncode != 0:
                encode_failed = True
                try:
                    log_size = os.fstat(log.fileno()).st_size
                    seek_pos = max(0, log_size - 16_000)
                    log.seek(seek_pos)
                    fail_diagnostic = log.read(16_000).strip()
                except OSError:
                    log.seek(0)
                    fail_diagnostic = log.read(16_000).strip()
    except Exception as exc:
        encode_failed = True
        fail_diagnostic = str(exc)
    finally:
        if proc is not None:
            stop_process(proc, grace_seconds=2.0)
            for stream in (proc.stdout, proc.stderr, proc.stdin):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if active_procs is not None and procs_lock is not None and proc is not None:
            with procs_lock:
                active_procs.pop(flac_file, None)

    if was_cancelled or (cancel_event and cancel_event.is_set()):
        if temp_opus.exists():
            temp_opus.unlink(missing_ok=True)
        return False, f"Cancelled encoding {flac_file.name}"

    if encode_failed:
        if temp_opus.exists():
            temp_opus.unlink(missing_ok=True)
        return False, f"Failed encoding {flac_file.name}: {fail_diagnostic}"

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
    if max_workers is None:
        workers = min(DEFAULT_MAX_WORKERS, total)
    else:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        workers = min(max_workers, MAX_WORKERS, total)

    all_ok = True
    completed_count = 0

    active_procs: Dict[Path, subprocess.Popen] = {}
    procs_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        file_iter = iter(flac_files)
        futures: Dict[Any, Path] = {}

        def _submit_next() -> bool:
            try:
                f = next(file_iter)
            except StopIteration:
                return False
            fut = executor.submit(
                _encode_single_track,
                f,
                flac_root,
                opus_root,
                cover_art,
                cancel_event,
                active_procs,
                procs_lock,
            )
            futures[fut] = f
            return True

        max_in_flight = workers * 2
        for _ in range(min(max_in_flight, total)):
            _submit_next()

        while futures:
            if cancel_event and cancel_event.is_set():
                for pending in list(futures.keys()):
                    pending.cancel()
                with procs_lock:
                    for proc in list(active_procs.values()):
                        stop_process(proc, grace_seconds=2.0)
                return False

            done = {f for f in futures if f.done()}
            if not done:
                if cancel_event:
                    cancel_event.wait(timeout=0.08)
                else:
                    time.sleep(0.08)
                continue

            for future in done:
                track_file = futures.pop(future)
                try:
                    ok, message = future.result()
                except Exception:
                    logger.exception(
                        "Unexpected Opus worker failure for %s",
                        track_file,
                    )
                    ok = False
                    message = "Unexpected encoder worker failure"

                completed_count += 1
                if not ok:
                    logger.error("Opus encoding failed for %s: %s", track_file, message)
                    all_ok = False
                if progress_callback:
                    progress_callback(completed_count, total, track_file.name, ok)

                _submit_next()

    return all_ok
