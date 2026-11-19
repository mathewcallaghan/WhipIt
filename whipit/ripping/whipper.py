"""Subprocess wrapper for Whipper extraction with live output callbacks, cancellation, and speed wrapper injection."""

from __future__ import annotations

import os
import queue
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
    r"offset.*(unconfigured|not configured)|(unconfigured|missing).*offset|no read offset|unknown drive.*offset|configure.*(?:--offset|-o)", re.IGNORECASE
)
_RE_IS_CDR = re.compile(
    r"(?:inserted disc seems to be a CD-R|--cdr not passed|CD-R detected:\s*(?:true|yes))",
    re.IGNORECASE,
)
_RE_NETWORK_ERROR = re.compile(r"network error|NetworkError|connection.*refused|failed to connect", re.IGNORECASE)
_RE_METADATA_MISSING = re.compile(
    r"unable to retrieve disc metadata|release not found|NotFoundException|unknown argument not passed", re.IGNORECASE
)
_RE_NO_CD = re.compile(r"no CD detected|please insert one", re.IGNORECASE)
_RE_TOC_READ_ERROR = re.compile(
    r"cdrdao\.read-toc.*FileNotFoundError|FileNotFoundError.*cdrdao\.read-toc|Cannot read disk toc|Unit not ready,\s*giving up",
    re.IGNORECASE,
)


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
    toc_read_error: bool = False
    error_reason: Optional[str] = None


@dataclass
class WhipperEvent:
    pass


@dataclass
class PreExtractionEvent(WhipperEvent):
    phase: str = "Reading track pregaps & indexing audio sectors..."


@dataclass
class TrackProgressEvent(WhipperEvent):
    current_track: int
    total_tracks: int
    percent: float
    phase: str
    try_number: int = 1
    speed_x: Optional[float] = None
    max_tries: int = 10
    retry_reason: Optional[str] = None


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
class AccurateRipSummaryEvent(WhipperEvent):
    summary: str
    is_accurate: bool = False


@dataclass
class ChecksumMatchEvent(WhipperEvent):
    track: int
    matched: bool
    crc: str = ""
    rip_quality: Optional[float] = None
    peak_level: Optional[float] = None


class WhipperStreamParser:
    """Parses raw Whipper and cd-paranoia stdout lines into structured domain events."""

    def __init__(self, max_tries: int = 10) -> None:
        self.in_candidate_table: bool = False
        self.tracks_started: bool = False
        self.max_tries: int = max_tries
        self.current_track: Optional[int] = None
        self.current_try: int = 1
        self.last_retry_reason: Optional[str] = None

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
        # Pre-extraction pregap & index table reading
        if not self.tracks_started and not self.in_candidate_table:
            if re.search(r"Reading table\s+\d+\s*%", line, re.IGNORECASE) or re.search(r"(?:analyzing|reading)\s+pregap", line, re.IGNORECASE):
                return PreExtractionEvent()

        # Calibration live progress streaming
        m_cal_try = re.search(r"trying read offset\s+([+-]?\d+)", clean, re.IGNORECASE)
        if m_cal_try:
            return PreExtractionEvent(phase=f"Calibrating: Testing read offset {int(m_cal_try.group(1)):+d}...")

        m_cal_likely = re.search(r"offset of device is likely\s+([+-]?\d+),\s*confirming", clean, re.IGNORECASE)
        if m_cal_likely:
            return PreExtractionEvent(phase=f"Calibrating: Offset {int(m_cal_likely.group(1)):+d} matched track 1, confirming...")

        m_cal_rip = re.search(r"(?:Ripping|Reading)\s+track\s+(\d+)\s+with read offset\s+([+-]?\d+)(?:\s+(\d+)\s*%)?", clean, re.IGNORECASE)
        if m_cal_rip:
            t_num = int(m_cal_rip.group(1))
            off_num = int(m_cal_rip.group(2))
            pct_str = f" ({m_cal_rip.group(3)}%)" if m_cal_rip.group(3) else ""
            return PreExtractionEvent(phase=f"Calibrating: Confirming offset {off_num:+d} — Track {t_num}{pct_str}...")

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
            re.IGNORECASE,
        )
        if m_picked:
            self.in_candidate_table = False
            is_req = bool(m_picked.group(1) and m_picked.group(1).lower() == "requested")
            return ReleasePickedEvent(mbid=m_picked.group(2).lower(), is_requested=is_req)

        # 3b. Track Retry Trigger Reasons
        lower_clean = clean.lower()
        if "checksums do not match" in lower_clean:
            self.last_retry_reason = "Checksums do not match"
        elif "couldn't read any frames" in lower_clean or "file size 0" in lower_clean:
            self.last_retry_reason = "Drive read error"

        # 4. Track Progress (Reading, Verifying, FLAC Encoding)
        m_prog = re.search(
            r"(?:^|:\s*|\b)(Reading|Verifying|Ripping)\s+track\s+(\d+)\s+of\s+(\d+)(?:\s+\(try\s+(\d+)\))?.*?(\d+)\s*%",
            clean,
            re.IGNORECASE,
        )
        if m_prog:
            self.tracks_started = True
            t_num = int(m_prog.group(2))
            t_tot = int(m_prog.group(3))
            if m_prog.group(4):
                self.current_try = int(m_prog.group(4))
            elif self.current_track != t_num:
                self.current_try = 1
                self.last_retry_reason = None
            self.current_track = t_num
            m_spd = re.search(r"(?:@|\b)\s*([\d.]+)\s*[xX]\b", clean)
            speed_val = float(m_spd.group(1)) if m_spd else None
            return TrackProgressEvent(
                phase=m_prog.group(1).capitalize(),
                current_track=t_num,
                total_tracks=t_tot,
                try_number=self.current_try,
                percent=float(m_prog.group(5)),
                speed_x=speed_val,
                max_tries=self.max_tries,
                retry_reason=self.last_retry_reason if self.current_try > 1 else None,
            )

        # 4b. Track Start / Retry Header (without percentage, e.g. 'ripping track 8 of 12 (try 2): ...')
        if "with read offset" not in lower_clean:
            m_track_hdr = re.search(
                r"(?:^|:\s*|\b)Ripping\s+track\s+(\d+)\s+of\s+(\d+)(?:\s+\(try\s+(\d+)\))?\s*(?::|$)",
                clean,
                re.IGNORECASE,
            )
            if m_track_hdr:
                self.tracks_started = True
                t_num = int(m_track_hdr.group(1))
                t_tot = int(m_track_hdr.group(2))
                try_num = int(m_track_hdr.group(3)) if m_track_hdr.group(3) else 1
                if try_num == 1 and self.current_track != t_num:
                    self.last_retry_reason = None
                elif try_num == 1:
                    self.last_retry_reason = None
                self.current_track = t_num
                self.current_try = try_num
                return TrackProgressEvent(
                    phase="Ripping",
                    current_track=t_num,
                    total_tracks=t_tot,
                    try_number=self.current_try,
                    percent=0.0,
                    max_tries=self.max_tries,
                    retry_reason=self.last_retry_reason if self.current_try > 1 else None,
                )

        # 5. AccurateRip Summary Output
        if "AccurateRip summary:" in line:
            m_ar = re.search(r"AccurateRip summary:\s*(.+)$", line)
            if m_ar:
                summary_text = m_ar.group(1).strip()
                return AccurateRipSummaryEvent(summary=summary_text, is_accurate=(summary_text.lower() == "all tracks accurately ripped"))

        # 5. Checksum & Quality Verification
        if "CRCs match for track" in line:
            m_crc = re.search(r"CRCs match for track\s+(\d+)", line)
            if m_crc:
                return ChecksumMatchEvent(track=int(m_crc.group(1)), matched=True)

        # 6. Header metadata (before track extraction starts)
        if not self.tracks_started and not self.in_candidate_table:
            m_art = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Artist\s*:\s*(.+)$", line)
            if m_art and not re.search(r"\btrack\b", line, re.IGNORECASE):
                return AlbumHeaderEvent(artist=m_art.group(1).strip())

            m_tit = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Title\s*:\s*(.+)$", line)
            if m_tit and not re.search(r"\btrack\b", line, re.IGNORECASE):
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
        stall_warn_seconds: float = 30.0,
        stall_timeout_seconds: float = 60.0,
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
        self.parser = WhipperStreamParser(max_tries=max_retries)
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
                        if stat_file.exists():
                            stat_txt = stat_file.read_text(errors="ignore")
                            after_comm = stat_txt.rsplit(")", 1)[1].split()
                            if len(after_comm) > 1 and after_comm[1] == str(proc.pid):
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
        toc_read_error = False
        last_error_reason: Optional[str] = None

        effective_cancel = cancel_event or threading.Event()
        self._cancel_event = effective_cancel

        proc: Optional[subprocess.Popen] = None
        watcher_stop = threading.Event()
        watcher_thread: Optional[threading.Thread] = None
        full_log_file = None
        log_fd: Optional[int] = None

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
            log_fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(log_fd, "w", encoding="utf-8", errors="replace") as log_file:
                log_fd = None
                if self.full_log_path:
                    self.full_log_path.parent.mkdir(parents=True, exist_ok=True)
                    full_fd = os.open(self.full_log_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                    try:
                        full_log_file = os.fdopen(full_fd, "w", encoding="utf-8", errors="replace")
                    except Exception:
                        os.close(full_fd)
                        raise

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
                        for ln in iter(proc.stdout.readline, ""):
                            line_queue.put(ln)
                    except Exception:
                        pass
                    finally:
                        line_queue.put(None)

                reader_t = threading.Thread(target=_stdout_reader, daemon=True)
                reader_t.start()

                def _process_line(line_text: str) -> None:
                    nonlocal offset_unconfigured, is_cdr, network_error
                    nonlocal metadata_missing, no_cd, toc_read_error, capturing_traceback, last_error_reason
                    is_progress_tick = line_text.startswith("##:")
                    if not is_progress_tick:
                        log_file.write(line_text)
                        log_file.flush()
                        if full_log_file is not None:
                            full_log_file.write(line_text)
                            full_log_file.flush()
                        diagnostic_tail.append(line_text)
                        if not offset_unconfigured and _RE_OFFSET_UNCONFIGURED.search(line_text):
                            offset_unconfigured = True
                        if not is_cdr and _RE_IS_CDR.search(line_text):
                            is_cdr = True
                        if not network_error and _RE_NETWORK_ERROR.search(line_text):
                            network_error = True
                        if not metadata_missing and _RE_METADATA_MISSING.search(line_text):
                            metadata_missing = True
                        if not no_cd and _RE_NO_CD.search(line_text):
                            no_cd = True
                        if not toc_read_error and _RE_TOC_READ_ERROR.search(line_text):
                            toc_read_error = True

                        stripped = line_text.strip()
                        m_giveup = re.search(r"giving up on track\s+\d+\s+after\s+\d+\s+times?", stripped, re.IGNORECASE)
                        if m_giveup:
                            last_error_reason = f"Whipper {m_giveup.group(0)}."
                        elif stripped.startswith(("CRITICAL:", "ERROR:")):
                            last_error_reason = stripped.split(":", 2)[-1].strip() or stripped
                        elif stripped.startswith("RuntimeError:"):
                            last_error_reason = stripped

                    if "Traceback (most recent call last):" in line_text:
                        capturing_traceback = True
                    elif capturing_traceback and re.match(r"^(?:INFO|WARNING):whipper\.", line_text):
                        capturing_traceback = False

                    if capturing_traceback:
                        traceback_buffer.append(line_text)
                    elif not is_progress_tick:
                        self.output_callback(line_text)

                    if self.event_callback:
                        ev = self.parser.parse_line(line_text)
                        if ev:
                            self.event_callback(ev)

                last_output_time = time.monotonic()
                warned_stall = False
                idle_sec = 0.0

                while True:
                    if effective_cancel.is_set():
                        was_cancelled = True
                        break

                    try:
                        line = line_queue.get(timeout=0.25)
                    except queue.Empty:
                        if proc.poll() is not None:
                            reader_t.join(timeout=1.0)
                            while not line_queue.empty():
                                leftover = line_queue.get_nowait()
                                if leftover is not None:
                                    _process_line(leftover)
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
                    idle_sec = 0.0
                    warned_stall = False
                    _process_line(line)
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass
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

        stalled_flag = was_stalled or idle_sec >= self.stall_timeout_seconds
        if stalled_flag and not last_error_reason:
            last_error_reason = "Optical drive stopped responding (read stall)."

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
            stalled=stalled_flag,
            cancelled=was_cancelled,
            toc_read_error=toc_read_error,
            error_reason=last_error_reason,
        )

    def cleanup_wrapper(self) -> None:
        if self.wrap_dir and self.wrap_dir.exists():
            shutil.rmtree(self.wrap_dir, ignore_errors=True)
            self.wrap_dir = None
