"""Emergency raw sector extraction fallback using cdrdao (--paranoia-mode 0) and FLAC slicing."""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from whipit.config import release_drive_locks
from whipit.validation.flac import write_flac_tags

logger = logging.getLogger(__name__)

CD_FRAME_BYTES = 2352
CD_FRAMES_PER_SEC = 75
RAW_WARNING_TAG = "Extracted via raw sector recovery pass"

_RE_TOC_TRACK = re.compile(r"^\s*TRACK\s+(\S+)", re.IGNORECASE)
_RE_TOC_FILE = re.compile(r'^\s*(?:AUDIOFILE|FILE)\s+"([^"]+)"\s*(.*)$', re.IGNORECASE)
_RE_MSF = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")


@dataclass(frozen=True)
class RawTrackSlice:
    track_number: int
    segments: List[Tuple[int, Optional[int]]]


@dataclass
class RawExtractionResult:
    success: bool
    flac_files: List[Path] = field(default_factory=list)
    cue_path: Optional[Path] = None
    log_path: Optional[Path] = None
    cancelled: bool = False
    stalled: bool = False
    error_message: Optional[str] = None


def _msf_to_bytes(time_spec: str) -> Optional[int]:
    if time_spec == "0":
        return 0
    m = _RE_MSF.fullmatch(time_spec.strip())
    if not m:
        return None
    mins, secs, frames = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return ((mins * 60 + secs) * CD_FRAMES_PER_SEC + frames) * CD_FRAME_BYTES


def _parse_offset_token(offset_spec: str) -> Optional[int]:
    clean = offset_spec.strip()
    if clean.startswith("#") and clean[1:].isdigit():
        return int(clean[1:])
    return _msf_to_bytes(clean)


def parse_cdrdao_toc(toc_path: Path) -> List[RawTrackSlice]:
    """Parses audio track byte offsets and lengths from a cdrdao .toc file."""
    if not toc_path.exists():
        return []

    slices: List[RawTrackSlice] = []
    cd_track_num = 0
    in_audio_track = False
    current_segments: List[Tuple[int, Optional[int]]] = []

    def _flush() -> None:
        if in_audio_track and current_segments:
            slices.append(RawTrackSlice(track_number=cd_track_num, segments=list(current_segments)))

    for raw_line in toc_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue

        m_tr = _RE_TOC_TRACK.match(line)
        if m_tr:
            _flush()
            cd_track_num += 1
            in_audio_track = m_tr.group(1).upper() == "AUDIO"
            current_segments = []
            continue

        if not in_audio_track:
            continue

        m_file = _RE_TOC_FILE.match(line)
        if not m_file:
            continue

        rest = re.sub(r"//.*$", "", m_file.group(2)).strip()
        tokens = rest.split()
        byte_off: Optional[int] = 0
        byte_len: Optional[int] = None

        if len(tokens) >= 3 and tokens[0].startswith("#"):
            base = _parse_offset_token(tokens[0])
            rel = _parse_offset_token(tokens[1])
            byte_len = _msf_to_bytes(tokens[2])
            if base is None or rel is None:
                continue
            byte_off = base + rel
        elif len(tokens) == 2:
            byte_off = _parse_offset_token(tokens[0])
            byte_len = _msf_to_bytes(tokens[1])
            if byte_off is None:
                continue
        elif len(tokens) == 1:
            byte_off = _parse_offset_token(tokens[0])
            if byte_off is None:
                continue

        if byte_off is not None:
            current_segments.append((byte_off, byte_len))

    _flush()
    return slices


def _terminate_proc_group(proc: subprocess.Popen) -> None:
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


class RawSectorExtractor:
    """Executes raw sequential cdrdao read-cd extraction and slices tracks to FLAC."""

    def __init__(
        self,
        device_path: str,
        speed: Optional[int] = None,
        output_callback: Optional[Callable[[str], None]] = None,
        stall_timeout_seconds: float = 60.0,
    ) -> None:
        self.device_path = device_path
        self.speed = speed
        self.output_callback = output_callback or (lambda _line: None)
        self.stall_timeout_seconds = stall_timeout_seconds

    def _run_cdrdao(
        self,
        bin_path: Path,
        toc_path: Path,
        cancel_event: Optional[threading.Event],
    ) -> Tuple[int, bool, bool, str]:
        cmd = [
            "cdrdao",
            "read-cd",
            "--read-raw",
            "--paranoia-mode",
            "0",
            "--device",
            self.device_path,
        ]
        if self.speed:
            cmd.extend(["--speed", str(int(self.speed))])
        cmd.extend(["--datafile", str(bin_path), str(toc_path)])

        proc = subprocess.Popen(
            cmd,
            cwd=bin_path.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        line_queue: queue.Queue[Optional[str]] = queue.Queue()

        def _reader() -> None:
            if not proc.stdout:
                line_queue.put(None)
                return
            try:
                for ln in iter(proc.stdout.readline, ""):
                    line_queue.put(ln)
            except Exception:
                pass
            finally:
                line_queue.put(None)

        reader_t = threading.Thread(target=_reader, daemon=True)
        reader_t.start()

        last_output = time.monotonic()
        was_cancelled = False
        was_stalled = False
        tail_lines: List[str] = []

        try:
            while True:
                if cancel_event and cancel_event.is_set():
                    was_cancelled = True
                    break
                try:
                    ln = line_queue.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        reader_t.join(timeout=1.0)
                        while not line_queue.empty():
                            leftover = line_queue.get_nowait()
                            if leftover:
                                tail_lines.append(leftover)
                                self.output_callback(leftover)
                        break
                    if time.monotonic() - last_output >= self.stall_timeout_seconds:
                        was_stalled = True
                        break
                    continue

                if ln is None:
                    break
                last_output = time.monotonic()
                tail_lines.append(ln)
                if len(tail_lines) > 200:
                    tail_lines.pop(0)
                self.output_callback(ln)
        finally:
            _terminate_proc_group(proc)
            if was_cancelled or was_stalled:
                release_drive_locks(self.device_path)

        rc = proc.returncode if proc.returncode is not None else 1
        return rc, was_cancelled, was_stalled, "".join(tail_lines[-20:]).strip()

    def _encode_slice_to_flac(
        self,
        bin_path: Path,
        bin_size: int,
        track_slice: RawTrackSlice,
        out_flac: Path,
        cancel_event: Optional[threading.Event],
    ) -> bool:
        cmd = [
            "flac",
            "--silent",
            "--force",
            "--force-raw-format",
            "--endian=big",
            "--sign=signed",
            "--channels=2",
            "--bps=16",
            "--sample-rate=44100",
            "-o",
            str(out_flac),
            "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            assert proc.stdin is not None
            with open(bin_path, "rb") as bin_fh:
                for offset, length in track_slice.segments:
                    if offset >= bin_size:
                        continue
                    available = bin_size - offset
                    to_read = available if length is None else min(length, available)
                    to_read -= to_read % 4
                    if to_read <= 0:
                        continue
                    bin_fh.seek(offset)
                    remaining = to_read
                    while remaining > 0:
                        if cancel_event and cancel_event.is_set():
                            proc.terminate()
                            return False
                        chunk = bin_fh.read(min(65536, remaining))
                        if not chunk:
                            break
                        usable = len(chunk) - (len(chunk) % 4)
                        if usable > 0:
                            proc.stdin.write(chunk[:usable])
                        remaining -= len(chunk)
            proc.stdin.close()
            _, _err = proc.communicate(timeout=120.0)
            return proc.returncode == 0 and out_flac.exists() and out_flac.stat().st_size > 0
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass
            out_flac.unlink(missing_ok=True)
            return False

    def run_raw_extraction(
        self,
        stage_dir: Path,
        cancel_event: Optional[threading.Event] = None,
    ) -> RawExtractionResult:
        if not shutil.which("cdrdao"):
            return RawExtractionResult(
                success=False,
                error_message="cdrdao executable not found in PATH.",
            )

        stage_dir.mkdir(parents=True, exist_ok=True)
        bin_path = stage_dir / "raw_recovery.bin"
        toc_path = stage_dir / "raw_recovery.toc"

        try:
            rc, was_cancelled, was_stalled, tail = self._run_cdrdao(bin_path, toc_path, cancel_event)
            if was_cancelled:
                return RawExtractionResult(success=False, cancelled=True, error_message="Raw extraction cancelled.")
            if was_stalled:
                return RawExtractionResult(
                    success=False,
                    stalled=True,
                    error_message="cdrdao raw extraction stalled waiting for drive.",
                )
            if rc != 0 or not bin_path.exists() or not toc_path.exists():
                return RawExtractionResult(
                    success=False,
                    error_message=f"cdrdao raw extraction failed (exit {rc}): {tail}" if tail else f"cdrdao exited {rc}",
                )

            slices = parse_cdrdao_toc(toc_path)
            if not slices:
                return RawExtractionResult(
                    success=False,
                    error_message="No audio tracks found in cdrdao TOC output.",
                )

            bin_size = bin_path.stat().st_size
            flac_files: List[Path] = []
            for track_slice in slices:
                if cancel_event and cancel_event.is_set():
                    return RawExtractionResult(success=False, cancelled=True, error_message="Raw extraction cancelled.")
                out_flac = stage_dir / f"{track_slice.track_number:02d}.flac"
                self.output_callback(f"Encoding raw track {track_slice.track_number} to {out_flac.name}...\n")
                if not self._encode_slice_to_flac(bin_path, bin_size, track_slice, out_flac, cancel_event):
                    if cancel_event and cancel_event.is_set():
                        return RawExtractionResult(success=False, cancelled=True, error_message="Raw extraction cancelled.")
                    return RawExtractionResult(
                        success=False,
                        error_message=f"Failed encoding raw slice for track {track_slice.track_number}.",
                    )
                write_flac_tags(
                    out_flac,
                    {
                        "TRACKNUMBER": str(track_slice.track_number),
                        "WARNING": RAW_WARNING_TAG,
                    },
                )
                flac_files.append(out_flac)

            cue_path = stage_dir / "raw_recovery.cue"
            cue_lines = ['PERFORMER "Unknown Artist"', 'TITLE "Unknown Album"']
            for idx, flac_file in enumerate(flac_files):
                t_num = slices[idx].track_number
                cue_lines.append(f'FILE "{flac_file.name}" WAVE')
                cue_lines.append(f"  TRACK {t_num:02d} AUDIO")
                cue_lines.append("    INDEX 01 00:00:00")
            cue_path.write_text("\n".join(cue_lines) + "\n", encoding="utf-8")

            log_path = stage_dir / "raw_recovery.log"
            log_lines = [
                "WhipIt emergency raw sector recovery log — NOT produced by whipper; no AccurateRip verification.",
                "Health status: Extraction completed via raw sector recovery (unverified)",
                "AccurateRip summary: Unverified (Raw sector recovery)",
            ]
            for flac_file in flac_files:
                log_lines.append(f"Filename: {flac_file.name}")
                log_lines.append("Status: Not verified")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

            return RawExtractionResult(
                success=True,
                flac_files=flac_files,
                cue_path=cue_path,
                log_path=log_path,
            )
        finally:
            bin_path.unlink(missing_ok=True)
            toc_path.unlink(missing_ok=True)
