"""Subprocess wrapper for Whipper extraction with live output callbacks, cancellation, and speed wrapper injection."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from whipit.audio.metadata import _MBID_RE
from whipit.config import release_drive_locks

MAX_DIAGNOSTIC_LINES = 2_000
MAX_TRACEBACK_LINES = 500

_RE_OFFSET_UNCONFIGURED = re.compile(
    r"offset.*(unconfigured|not configured)|(unconfigured|missing).*offset|no read offset", re.I
)
_RE_IS_CDR = re.compile(r"--cdr|CD-R detected:\s*true", re.I)
_RE_NETWORK_ERROR = re.compile(r"network error|NetworkError|connection.*refused|failed to connect", re.I)
_RE_METADATA_MISSING = re.compile(
    r"unable to retrieve disc metadata|release not found|NotFoundException|unknown argument not passed", re.I
)
_RE_NO_CD = re.compile(r"no CD detected|please insert one", re.I)


@dataclass
class WhipperResult:
    returncode: int
    stdout: str
    stderr: str
    log_file: Path
    offset_unconfigured: bool
    is_cdr: bool
    network_error: bool
    metadata_missing: bool
    no_cd: bool
    cancelled: bool = False


class WhipperRunner:
    def __init__(
        self,
        cache_dir: Path,
        target_device: str = "/dev/cdrom",
        speed: Optional[int] = None,
        output_callback: Optional[Callable[[str], None]] = None,
    ):
        self.cache_dir = cache_dir
        self.target_device = target_device
        self.speed = speed
        self.output_callback = output_callback or self._default_output
        self.wrap_dir: Optional[Path] = None
        self.active_proc: Optional[subprocess.Popen] = None
        self._cancel_event: Optional[threading.Event] = None

    @staticmethod
    def _default_output(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    def _setup_speed_wrapper(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        paranoia_bin = shutil.which("cd-paranoia") or shutil.which("cdparanoia")
        if not paranoia_bin:
            raise FileNotFoundError("Neither cd-paranoia nor cdparanoia executable was found in PATH.")

        wrap_path = Path(tempfile.mkdtemp(prefix="wrap_", dir=self.cache_dir))
        binq = shlex.quote(paranoia_bin)
        speed_arg = f"-S {int(self.speed)} " if self.speed else ""
        script = (
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do case "$a" in -V|--version|-Q|--query|-h|--help) '
            f'exec {binq} "$@" ;; esac; done\n'
            f'exec {binq} {speed_arg}"$@"\n'
        )
        for name in ("cd-paranoia", "cdparanoia"):
            p = wrap_path / name
            p.write_text(script, encoding="utf-8")
            p.chmod(0o700)
        self.wrap_dir = wrap_path
        return wrap_path

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        proc = self.active_proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        if self.target_device:
            release_drive_locks(self.target_device)

    def _stop_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass

        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def run_rip(
        self,
        stage_dir: Path,
        offset: Optional[int] = None,
        release_id: Optional[str] = None,
        keep_going: bool = False,
        unknown_disc: bool = False,
        cdr: bool = False,
        extra_args: Optional[List[str]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> WhipperResult:
        stage_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_wrapper()
        self.wrap_dir = self._setup_speed_wrapper()
        env = os.environ.copy()
        env["PATH"] = f"{self.wrap_dir}:{env.get('PATH', '')}"
        env["PYTHONUNBUFFERED"] = "1"

        cmd = ["whipper", "cd"]
        if self.target_device:
            cmd.extend(["-d", self.target_device])
        cmd.extend(["rip", "-C", "complete"])
        if release_id:
            rid = release_id.strip().lower()
            if not _MBID_RE.fullmatch(rid):
                raise ValueError(f"Invalid MusicBrainz release id: {release_id!r}")
            cmd.extend(["-R", rid])
        if offset is not None:
            cmd.extend(["-O", str(offset)])
        if keep_going:
            cmd.append("--keep-going")
        if unknown_disc:
            cmd.append("-U")
        if cdr:
            cmd.append("--cdr")
        if extra_args:
            cmd.extend(extra_args)

        log_path = stage_dir / "whipper_exec.log"
        diagnostic_tail: deque[str] = deque(maxlen=MAX_DIAGNOSTIC_LINES)
        traceback_buffer: deque[str] = deque(maxlen=MAX_TRACEBACK_LINES)
        capturing_traceback = False
        was_cancelled = False

        offset_unconfigured = False
        is_cdr = False
        network_error = False
        metadata_missing = False
        no_cd = False

        effective_cancel = cancel_event or threading.Event()
        self._cancel_event = effective_cancel

        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        proc: Optional[subprocess.Popen] = None
        watcher_stop = threading.Event()
        watcher_thread: Optional[threading.Thread] = None

        def _cancel_watcher() -> None:
            while not watcher_stop.is_set():
                if proc and proc.poll() is not None:
                    break
                if effective_cancel.is_set():
                    if proc and proc.poll() is None:
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except (ProcessLookupError, OSError):
                            pass

                        start_wait = time.monotonic()
                        while not watcher_stop.is_set() and time.monotonic() - start_wait < 1.5:
                            if proc.poll() is not None:
                                break
                            time.sleep(0.05)

                        if proc.poll() is None and not watcher_stop.is_set():
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except (ProcessLookupError, OSError):
                                pass
                    break
                time.sleep(0.05)

        try:
            with open(log_fd, "w", encoding="utf-8", errors="replace") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    cwd=stage_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self.active_proc = proc

                watcher_thread = threading.Thread(target=_cancel_watcher, daemon=True)
                watcher_thread.start()

                if proc.stdout:
                    for line in proc.stdout:
                        if effective_cancel.is_set():
                            was_cancelled = True
                            break

                        is_progress_tick = line.startswith("##:")
                        if not is_progress_tick:
                            log_file.write(line)
                            diagnostic_tail.append(line)

                            if not offset_unconfigured and _RE_OFFSET_UNCONFIGURED.search(line):
                                offset_unconfigured = True
                            if not is_cdr and _RE_IS_CDR.search(line):
                                is_cdr = True
                            if not network_error and _RE_NETWORK_ERROR.search(line):
                                network_error = True
                            if not metadata_missing and _RE_METADATA_MISSING.search(line):
                                metadata_missing = True
                            if not no_cd and _RE_NO_CD.search(line):
                                no_cd = True

                        if "Traceback (most recent call last):" in line:
                            capturing_traceback = True

                        if capturing_traceback:
                            traceback_buffer.append(line)
                        elif not is_progress_tick:
                            self.output_callback(line)
        finally:
            watcher_stop.set()
            if watcher_thread is not None:
                watcher_thread.join(timeout=0.5)
            if effective_cancel.is_set():
                was_cancelled = True
            if proc is not None:
                self._stop_process(proc)
            self.active_proc = None
            self._cancel_event = None
            if was_cancelled and self.target_device:
                release_drive_locks(self.target_device)

        out_text = "".join(diagnostic_tail)

        if capturing_traceback and not (no_cd or offset_unconfigured or is_cdr or metadata_missing or network_error or was_cancelled):
            for tb_line in traceback_buffer:
                self.output_callback(tb_line)

        return WhipperResult(
            returncode=proc.returncode if proc and proc.returncode is not None else (-1 if was_cancelled else 1),
            stdout=out_text,
            stderr="",
            log_file=log_path,
            offset_unconfigured=offset_unconfigured,
            is_cdr=is_cdr,
            network_error=network_error,
            metadata_missing=metadata_missing,
            no_cd=no_cd,
            cancelled=was_cancelled,
        )

    def cleanup_wrapper(self) -> None:
        if self.wrap_dir and self.wrap_dir.exists():
            shutil.rmtree(self.wrap_dir, ignore_errors=True)
            self.wrap_dir = None
