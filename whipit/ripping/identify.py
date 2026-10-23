"""Non-destructive optical disc identification and MusicBrainz lookup."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from whipit.config import drive_has_disc
from whipit.inhibit import unmount_cdda_device
from whipit.models import ReleaseOption, TrackInfo

_MB_REL_RE = re.compile(
    r"https?://musicbrainz\.org/release/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


@dataclass
class DiscInspectionResult:
    success: bool
    artist: str = "Unknown Artist"
    title: str = "Unknown Album"
    duration: Optional[str] = None
    tracks_count: Optional[int] = None
    mbid: Optional[str] = None
    mb_url: Optional[str] = None
    mb_lookup_url: Optional[str] = None
    discogs_url: Optional[str] = None
    barcode: Optional[str] = None
    cat_no: Optional[str] = None
    matching_releases: List[ReleaseOption] = field(default_factory=list)
    tracks: List[TrackInfo] = field(default_factory=list)
    raw_output: str = ""
    error_message: Optional[str] = None


def format_duration(dur_str: Optional[str]) -> Optional[str]:
    if not dur_str:
        return None
    try:
        parts = dur_str.split(".")[0].split(":")
        if len(parts) == 3:
            h, m, s = int(parts[0]), parts[1], parts[2]
            return f"{m}:{s}" if h == 0 else f"{h}:{m}:{s}"
        return dur_str.split(".")[0]
    except Exception:
        return dur_str


def _terminate_process_group(proc: subprocess.Popen, timeout: float = 1.0) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
    except (ProcessLookupError, OSError):
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
                proc.wait()
            except OSError:
                pass


def inspect_disc(
    device_path: str = "/dev/cdrom",
    timeout: float = 120.0,
    output_callback: Optional[Callable[[str], None]] = None,
) -> DiscInspectionResult:
    unmount_cdda_device(device_path)

    if not drive_has_disc(device_path, wait_ready=True, timeout=8.0):
        return DiscInspectionResult(
            success=False,
            error_message=f"No Audio CD detected in {device_path} (tray empty or disc unreadable).",
        )

    unmount_cdda_device(device_path)

    cmd = ["whipper", "--drive-auto-close=False", "cd", "-d", device_path, "info"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    collected_lines = []
    start_time = time.time()
    timed_out = False
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
            start_new_session=True,
        )

        line_queue: queue.Queue[Optional[str]] = queue.Queue()

        def _reader() -> None:
            if not proc or not proc.stdout:
                line_queue.put(None)
                return
            try:
                for line in iter(proc.stdout.readline, ""):
                    line_queue.put(line)
            except Exception:
                pass
            finally:
                line_queue.put(None)
                try:
                    if proc and proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass

        threading.Thread(target=_reader, daemon=True).start()

        while True:
            if time.time() - start_time > timeout:
                timed_out = True
                _terminate_process_group(proc)
                break

            try:
                item = line_queue.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    while not line_queue.empty():
                        leftover = line_queue.get_nowait()
                        if leftover:
                            collected_lines.append(leftover)
                            if output_callback:
                                output_callback(leftover.rstrip())
                    break
                continue

            if item is None:
                break

            collected_lines.append(item)
            if output_callback:
                output_callback(item.rstrip())

        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc, timeout=0.5)

    except Exception as exc:
        return DiscInspectionResult(
            success=False,
            raw_output="".join(collected_lines),
            error_message=f"Error executing whipper: {exc}",
        )

    out = "".join(collected_lines)

    duration = None
    tracks_count = None

    m_dur = re.search(r"Disc duration:\s*([0-9:.]+)", out)
    if m_dur:
        duration = format_duration(m_dur.group(1).strip())

    m_tracks = re.search(r"(\d+)\s+audio tracks?", out, re.I)
    if m_tracks:
        tracks_count = int(m_tracks.group(1))

    m_lookup = re.search(r"MusicBrainz lookup URL\s*[:\s]\s*(https?://musicbrainz\.org/cdtoc/attach\S+)", out)
    mb_lookup_url = m_lookup.group(1).strip() if m_lookup else None

    m_discid = re.search(r"MusicBrainz disc id\s+([A-Za-z0-9_.-]+)", out)
    disc_id = m_discid.group(1).strip() if m_discid else None

    # If Whipper timed out or failed on MusicBrainz lookup, but read the disc ID from TOC:
    if disc_id and "Matching releases:" not in out:
        try:
            from whipit import __version__
            from whipit.net import fetch_bounded
            mb_disc_url = f"https://musicbrainz.org/ws/2/discid/{disc_id}?fmt=json"
            req_headers = {
                "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
                "Accept": "application/json",
            }
            raw_mb = fetch_bounded(
                mb_disc_url,
                headers=req_headers,
                suffixes=("musicbrainz.org",),
                max_bytes=1_000_000,
                timeout=10.0,
            )
            mb_json = json.loads(raw_mb.decode("utf-8"))
            releases = mb_json.get("releases", [])
            if releases:
                rel_lines = ["\nMatching releases:\n"]
                for r in releases:
                    r_id = r.get("id", "")
                    r_title = r.get("title", "")
                    r_artist = ""
                    credits = r.get("artist-credit", [])
                    if credits and isinstance(credits[0], dict):
                        r_artist = credits[0].get("name", "")
                    r_date = r.get("date", "")
                    r_country = r.get("country", "")
                    r_barcode = r.get("barcode", "")
                    rel_lines.append(f"Artist : {r_artist}\nTitle : {r_title}\nRelease : https://musicbrainz.org/release/{r_id}\n")
                    if r_date:
                        rel_lines.append(f"Date : {r_date}\n")
                    if r_country:
                        rel_lines.append(f"Country : {r_country}\n")
                    if r_barcode:
                        rel_lines.append(f"Barcode : {r_barcode}\n")
                    rel_lines.append("\n")
                out += "".join(rel_lines)
        except Exception:
            pass

    matching_releases: List[ReleaseOption] = []
    if "Matching releases:" in out:
        rel_section = out.split("Matching releases:", 1)[1]
        for end_marker in ("Table of contents", "Reading table", "CRITICAL:", "Traceback"):
            if end_marker in rel_section:
                rel_section = rel_section.split(end_marker, 1)[0]

        raw_blocks = re.split(r"(?=^\s*Artist\s*:)", rel_section, flags=re.M)
        for block in raw_blocks:
            if not block.strip():
                continue

            m_u = _MB_REL_RE.search(block)
            if m_u:
                mbid = m_u.group(1).lower()
                m_a = re.search(r"^\s*Artist\s*:\s*(.+)$", block, re.M)
                m_t = re.search(r"^\s*Title\s*:\s*(.+)$", block, re.M)
                m_c = re.search(r"^\s*Country\s*:\s*(.+)$", block, re.M)
                m_cat = re.search(r"^\s*Cat no\s*:\s*(.+)$", block, re.M)
                m_bar = re.search(r"^\s*Barcode\s*:\s*(.+)$", block, re.M)
                m_bdur = re.search(r"^\s*Duration\s*:\s*(.+)$", block, re.M)
                m_y = re.search(r"^\s*(?:Year|Date)\s*:\s*(.+)$", block, re.M)

                matching_releases.append(
                    ReleaseOption(
                        mbid=mbid,
                        artist=m_a.group(1).strip() if m_a else "Unknown Artist",
                        title=m_t.group(1).strip() if m_t else "Unknown Album",
                        country=m_c.group(1).strip() if m_c else None,
                        cat_no=m_cat.group(1).strip() if m_cat else None,
                        barcode=m_bar.group(1).strip() if m_bar else None,
                        year=m_y.group(1).strip() if m_y else None,
                        duration=format_duration(m_bdur.group(1).strip()) if m_bdur else duration,
                    )
                )

    if timed_out and not matching_releases:
        return DiscInspectionResult(
            success=False,
            duration=duration,
            tracks_count=tracks_count,
            mb_lookup_url=mb_lookup_url,
            raw_output=out,
            error_message=f"Timeout ({timeout:.0f}s) while inspecting disc in {device_path}.",
        )

    if "no CD detected" in out.lower():
        return DiscInspectionResult(
            success=False,
            raw_output=out,
            error_message=f"No Audio CD detected in {device_path}.",
        )

    primary_artist = matching_releases[0].artist if matching_releases else "Unknown Artist"
    primary_title = matching_releases[0].title if matching_releases else "Unknown Album"
    primary_mbid = matching_releases[0].mbid if matching_releases else None
    primary_url = f"https://musicbrainz.org/release/{primary_mbid}" if primary_mbid else None
    primary_barcode = matching_releases[0].barcode if matching_releases else None
    primary_cat = matching_releases[0].cat_no if matching_releases else None
    primary_tracks: List[TrackInfo] = []
    discogs_url: Optional[str] = None

    if primary_mbid:
        from whipit.audio.metadata import fetch_release_details

        details = fetch_release_details(
            primary_mbid,
            barcode=primary_barcode,
            artist=primary_artist,
            title=primary_title,
            expected_tracks=tracks_count,
        )
        primary_tracks = getattr(details, "tracks", [])
        discogs_url = getattr(details, "discogs_url", None)

    returncode = proc.returncode if proc and proc.returncode is not None else 0
    success = (returncode == 0) or bool(primary_url or primary_artist != "Unknown Artist")
    err_msg = None
    if not success:
        if "release not found" in out.lower() or "not found in musicbrainz" in out.lower():
            err_msg = "Disc detected, but not catalogued in MusicBrainz database."
        else:
            err_msg = "Failed to inspect disc metadata."

    return DiscInspectionResult(
        success=success,
        artist=primary_artist,
        title=primary_title,
        duration=duration,
        tracks_count=tracks_count,
        mbid=primary_mbid,
        mb_url=primary_url,
        mb_lookup_url=mb_lookup_url,
        discogs_url=discogs_url,
        barcode=primary_barcode,
        cat_no=primary_cat,
        matching_releases=matching_releases,
        tracks=primary_tracks,
        raw_output=out,
        error_message=err_msg,
    )
