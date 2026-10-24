"""Subprocess wrapper for Whipper extraction with live output callbacks, cancellation, and speed wrapper injection."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import queue
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
    stalled: bool = False
    cancelled: bool = False


@dataclass
class WhipperEvent:
    pass


@dataclass
class TrackProgressEvent(WhipperEvent):
    current_track: int
    total_tracks: int
    percent: float
    phase: str
    try_number: int = 1
    speed_x: Optional[float] = None


@dataclass
class DriveStallEvent(WhipperEvent):
    seconds_stalled: float
    wchan: Optional[str] = None


@dataclass
class ReleasePickedEvent(WhipperEvent):
    mbid: str
    is_requested: bool = False


@dataclass
class DiscTOCEvent(WhipperEvent):
    lookup_url: Optional[str] = None
    disc_id: Optional[str] = None
    cddb_disc_id: Optional[str] = None
    duration: Optional[str] = None


@dataclass
class AlbumHeaderEvent(WhipperEvent):
    artist: Optional[str] = None
    title: Optional[str] = None


@dataclass
class ChecksumMatchEvent(WhipperEvent):
    track: int
    matched: bool
    crc: str = ""
    rip_quality: Optional[float] = None
    peak_level: Optional[float] = None


class WhipperStreamParser:
    """Parses raw Whipper and cd-paranoia stdout lines into structured domain events."""

    def __init__(self) -> None:
        self.in_candidate_table: bool = False
        self.tracks_started: bool = False

    def parse_line(self, line: str) -> Optional[WhipperEvent]:
        clean = line.strip()
        if not clean:
            return None

        # 1. Candidate table boundary markers
        if "Matching releases:" in line:
            self.in_candidate_table = True
            return None
        if self.in_candidate_table and ("Reading table" in line or "Table of contents" in line or "picked" in line):
            self.in_candidate_table = False

        # 2. Disc TOC & Lookup URLs
        if "Disc duration:" in line:
            m_dur = re.search(r"Disc duration:\s*([0-9:.]+)", line)
            if m_dur:
                return DiscTOCEvent(duration=m_dur.group(1).strip())
        if "MusicBrainz lookup URL" in line:
            m = re.search(r"MusicBrainz lookup URL\s*[:\s]\s*(https?://musicbrainz\.org/cdtoc/attach\S+)", line)
            if m:
                return DiscTOCEvent(lookup_url=m.group(1).strip())
        if "MusicBrainz disc id" in line:
            m = re.search(r"MusicBrainz disc id\s+([A-Za-z0-9_.-]+)", line)
            if m:
                return DiscTOCEvent(disc_id=m.group(1).strip())
        if "CDDB disc id:" in line:
            m = re.search(r"CDDB disc id:\s*([A-Za-z0-9]+)", line)
            if m:
                return DiscTOCEvent(cddb_disc_id=m.group(1).strip())

        # 3. Release Selection (Explicit pick logged by Whipper)
        m_picked = re.search(
            r"picked\s+(requested|first)?\s*release id\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            line,
            re.I,
        )
        if m_picked:
            self.in_candidate_table = False
            is_req = bool(m_picked.group(1) and m_picked.group(1).lower() == "requested")
            return ReleasePickedEvent(mbid=m_picked.group(2).lower(), is_requested=is_req)

        # 4. Track Progress (Reading, Verifying, FLAC Encoding)
        m_prog = re.search(
            r"^(Reading|Verifying)\s+track\s+(\d+)\s+of\s+(\d+)(?:\s+\(try\s+(\d+)\))?.*?(\d+)\s*%",
            clean,
            re.I,
        )
        if m_prog:
            self.tracks_started = True
            return TrackProgressEvent(
                phase=m_prog.group(1).capitalize(),
                current_track=int(m_prog.group(2)),
                total_tracks=int(m_prog.group(3)),
                try_number=int(m_prog.group(4)) if m_prog.group(4) else 1,
                percent=float(m_prog.group(5)),
            )

        # 5. Checksum & Quality Verification
        if "CRCs match for track" in line:
            m_crc = re.search(r"CRCs match for track\s+(\d+)", line)
            if m_crc:
                return ChecksumMatchEvent(track=int(m_crc.group(1)), matched=True)

        # 6. Header metadata (before track extraction starts)
        if not self.tracks_started and not self.in_candidate_table:
            m_art = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Artist\s*:\s*(.+)$", line)
            if m_art and not re.search(r"\btrack\b", line, re.I):
                return AlbumHeaderEvent(artist=m_art.group(1).strip())

            m_tit = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Title\s*:\s*(.+)$", line)
            if m_tit and not re.search(r"\btrack\b", line, re.I):
                return AlbumHeaderEvent(title=m_tit.group(1).strip())

        return None


class WhipperRunner:
    def __init__(
        self,
        cache_dir: Path,
        target_device: str = "/dev/cdrom",
        speed: Optional[int] = None,
        output_callback: Optional[Callable[[str], None]] = None,
        event_callback: Optional[Callable[[WhipperEvent], None]] = None,
        stall_warn_seconds: float = 90.0,
        stall_timeout_seconds: float = 180.0,
        full_log_path: Optional[Path] = None,
        max_retries: int = 10,
    ):
        self.cache_dir = cache_dir
        self.target_device = target_device
        self.speed = speed
        self.output_callback = output_callback or self._default_output
        self.event_callback = event_callback
        self.stall_warn_seconds = stall_warn_seconds
        self.stall_timeout_seconds = stall_timeout_seconds
        self.full_log_path = full_log_path
        self.max_retries = max_retries
        self.parser = WhipperStreamParser()
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

    def _get_active_wchan(self) -> Optional[str]:
        """Inspects /proc for cd-paranoia or whipper to check if stalled in kernel I/O."""
        proc = self.active_proc
        if not proc or proc.poll() is not None:
            return None
        pids = [proc.pid]
        proc_dir = Path("/proc")
        if proc_dir.exists():
            for p in proc_dir.iterdir():
                if p.name.isdigit():
                    try:
                        stat_file = p / "stat"
                        if stat_file.exists() and f" {proc.pid} " in stat_file.read_text(errors="ignore"):
                            pids.append(int(p.name))
                    except Exception:
                        pass
        for pid in reversed(pids):
            wchan_file = proc_dir / str(pid) / "wchan"
            if wchan_file.exists():
                wchan = wchan_file.read_text(errors="ignore").strip()
                if wchan and wchan != "0":
                    return wchan
        return None

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
        cmd.extend(["rip"])
        cmd.extend(["-r", str(self.max_retries)])
        if release_id:
            rid = release_id.strip().lower()
            if not _MBID_RE.fullmatch(rid):
                raise ValueError(f"Invalid MusicBrainz release id: {release_id!r}")
            cmd.extend(["-R", rid])
        if offset is not None:
            cmd.extend(["-o", str(offset)])
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
        was_stalled = False

        effective_cancel = cancel_event or threading.Event()
        self._cancel_event = effective_cancel

        log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        full_log_file = None
        if self.full_log_path:
            self.full_log_path.parent.mkdir(parents=True, exist_ok=True)
            full_fd = os.open(self.full_log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            full_log_file = open(full_fd, "w", encoding="utf-8", errors="replace")
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

                line_queue: queue.Queue[Optional[str]] = queue.Queue()

                def _stdout_reader() -> None:
                    if not proc or not proc.stdout:
                        line_queue.put(None)
                        return
                    try:
                        for l in iter(proc.stdout.readline, ""):
                            line_queue.put(l)
                    except Exception:
                        pass
                    finally:
                        line_queue.put(None)

                reader_t = threading.Thread(target=_stdout_reader, daemon=True)
                reader_t.start()

                last_output_time = time.monotonic()
                warned_stall = False

                while True:
                    if effective_cancel.is_set():
                        was_cancelled = True
                        break

                    try:
                        line = line_queue.get(timeout=0.25)
                    except queue.Empty:
                        if proc.poll() is not None:
                            break
                        idle_sec = time.monotonic() - last_output_time
                        if idle_sec >= self.stall_warn_seconds and not warned_stall:
                            warned_stall = True
                            wchan = self._get_active_wchan()
                            if self.event_callback:
                                self.event_callback(DriveStallEvent(seconds_stalled=idle_sec, wchan=wchan))
                            self.output_callback(
                                f"\n[WARN] Optical drive read stalled for {int(idle_sec)}s (waiting for disc response)...\n"
                            )
                        if idle_sec >= self.stall_timeout_seconds:
                            was_stalled = True
                            self.output_callback(
                                f"\n[ERROR] Optical drive stopped responding after {int(idle_sec)}s. Initiating recovery...\n"
                            )
                            break
                        continue

                    if line is None:
                        break

                    last_output_time = time.monotonic()
                    warned_stall = False

                    is_progress_tick = line.startswith("##:")
                    if not is_progress_tick:
                        log_file.write(line)
                        log_file.flush()
                        if full_log_file is not None:
                            full_log_file.write(line)
                            full_log_file.flush()
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

                    if self.event_callback:
                        ev = self.parser.parse_line(line)
                        if ev:
                            self.event_callback(ev)
        finally:
            if full_log_file is not None:
                full_log_file.close()
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
            stalled=was_stalled,
            cancelled=was_cancelled,
        )

    def cleanup_wrapper(self) -> None:
        if self.wrap_dir and self.wrap_dir.exists():
            shutil.rmtree(self.wrap_dir, ignore_errors=True)
            self.wrap_dir = None
