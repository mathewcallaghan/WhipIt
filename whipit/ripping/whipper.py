"""Subprocess wrapper for Whipper extraction with live output callbacks, cancellation, and speed wrapper injection."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from whipit.config import release_drive_locks


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

    @staticmethod
    def _default_output(line: str) -> None:
        sys.stdout.write(line)
        sys.stdout.flush()

    def _setup_speed_wrapper(self) -> Path:
        wrap_path = self.cache_dir / f"wrap_{os.getpid()}_{threading.get_ident()}"
        wrap_path.mkdir(parents=True, exist_ok=True)
        paranoia_bin = shutil.which("cd-paranoia") or shutil.which("cdparanoia")
        if not paranoia_bin:
            raise FileNotFoundError("Neither cd-paranoia nor cdparanoia executable was found in PATH.")

        wrapper_script = wrap_path / "cd-paranoia"
        if self.speed:
            script_content = f"""#!/usr/bin/env bash
for a in "$@"; do
    case "$a" in
        -V|--version|-Q|--query|-h|--help) exec "{paranoia_bin}" "$@" ;;
    esac
done
exec "{paranoia_bin}" -S "{self.speed}" "$@"
"""
        else:
            script_content = f"""#!/usr/bin/env bash
exec "{paranoia_bin}" "$@"
"""
        wrapper_script.write_text(script_content, encoding="utf-8")
        wrapper_script.chmod(0o755)
        cdparanoia_alias = wrap_path / "cdparanoia"
        if not cdparanoia_alias.exists():
            shutil.copy2(wrapper_script, cdparanoia_alias)
        return wrap_path

    def cancel(self) -> None:
        proc = self.active_proc
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

            def _force_kill():
                time.sleep(1.0)
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

            threading.Thread(target=_force_kill, daemon=True).start()

        if self.target_device:
            release_drive_locks(self.target_device)

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
        self.wrap_dir = self._setup_speed_wrapper()
        env = os.environ.copy()
        env["PATH"] = f"{self.wrap_dir}:{env.get('PATH', '')}"
        env["PYTHONUNBUFFERED"] = "1"

        cmd = ["whipper", "cd"]
        if self.target_device:
            cmd.extend(["-d", self.target_device])
        cmd.extend(["rip", "-C", "complete"])
        if release_id:
            cmd.extend(["-R", release_id])
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
        collected_lines: List[str] = []
        traceback_buffer: List[str] = []
        capturing_traceback = False
        was_cancelled = False

        with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
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

            watcher_stop = threading.Event()

            def _cancel_watcher():
                while not watcher_stop.is_set() and proc.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        self.cancel()
                        break
                    time.sleep(0.15)

            if cancel_event:
                threading.Thread(target=_cancel_watcher, daemon=True).start()

            if proc.stdout:
                for line in proc.stdout:
                    if cancel_event and cancel_event.is_set():
                        was_cancelled = True
                        break

                    log_file.write(line)
                    collected_lines.append(line)

                    if "Traceback (most recent call last):" in line:
                        capturing_traceback = True

                    if capturing_traceback:
                        traceback_buffer.append(line)
                    else:
                        self.output_callback(line)

            watcher_stop.set()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                proc.wait()

        self.active_proc = None

        if (cancel_event and cancel_event.is_set()) or was_cancelled:
            was_cancelled = True
            if self.target_device:
                release_drive_locks(self.target_device)

        out_text = "".join(collected_lines)

        offset_unconfigured = bool(
            re.search(r"offset.*(unconfigured|not configured)|(unconfigured|missing).*offset|no read offset", out_text, re.I)
        )
        is_cdr = bool(re.search(r"--cdr|CD-R detected:\s*true", out_text, re.I))
        network_error = bool(re.search(r"network error|NetworkError|connection.*refused|failed to connect", out_text, re.I))
        metadata_missing = bool(
            re.search(r"unable to retrieve disc metadata|release not found|NotFoundException|unknown argument not passed", out_text, re.I)
        )
        no_cd = bool(
            re.search(r"no CD detected|please insert one", out_text, re.I)
        )

        if capturing_traceback and not (no_cd or offset_unconfigured or is_cdr or metadata_missing or network_error or was_cancelled):
            for tb_line in traceback_buffer:
                self.output_callback(tb_line)

        return WhipperResult(
            returncode=proc.returncode,
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
