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

from whipit.audio.metadata import clean_album_title
from whipit.config import drive_has_disc
from whipit.inhibit import unmount_cdda_device
from whipit.models import ReleaseOption, TrackInfo

_MB_REL_RE = re.compile(
    r"(?:https?://musicbrainz\.org/release/|(?:\bRelease|\bURL)\s*:\s*(?:https?://musicbrainz\.org/release/)?)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
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
            if hasattr(proc, "terminate"):
                proc.terminate()
            if hasattr(proc, "wait"):
                proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError, Exception):
            try:
                if hasattr(proc, "kill"):
                    proc.kill()
                if hasattr(proc, "wait"):
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

            clean_item = item.strip()
            has_seen_releases = any("matching releases:" in l.lower() for l in collected_lines)
            has_seen_not_found = any(
                "release not found" in l.lower() or "notfoundexception" in l.lower()
                for l in collected_lines
            )

            # Abort only when Whipper transitions to the slow trailing subchannel/TOC scan
            is_trailing_table_scan = (
                re.search(r"reading table\s+\d+\s*%", clean_item, re.I)
                or (
                    has_seen_releases
                    and (
                        re.search(r"^table of contents(?:\s*\(cdrdao\))?:?", clean_item, re.I)
                        or re.search(r"track\s+\d+\s+finished,\s*found\s+\d+\s+q\s+sub-channels", clean_item, re.I)
                    )
                )
                or (
                    has_seen_not_found
                    and (
                        re.search(r"reading table\s+\d+\s*%", clean_item, re.I)
                        or re.search(r"^table of contents(?:\s*\(cdrdao\))?:?", clean_item, re.I)
                        or re.search(r"track\s+\d+\s+finished,\s*found\s+\d+\s+q\s+sub-channels", clean_item, re.I)
                    )
                )
            )

            if is_trailing_table_scan:
                collected_lines.append(item)
                _terminate_process_group(proc)
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

    mb_releases_map = {}
    if disc_id:
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
            for r in releases:
                rid = r.get("id", "").lower()
                if not rid:
                    continue
                r_cat = None
                for li in (r.get("label-info") or []):
                    if isinstance(li, dict) and li.get("catalog-number"):
                        r_cat = li.get("catalog-number")
                        break
                caa_info = r.get("cover-art-archive") or {}
                has_art = bool(caa_info.get("artwork") or caa_info.get("front"))
                credits = r.get("artist-credit", [])
                r_artist = "".join(
                    (c.get("name", "") + c.get("joinphrase", "")) for c in credits if isinstance(c, dict)
                ).strip() if credits else None
                mb_releases_map[rid] = {
                    "artist": r_artist,
                    "title": r.get("title"),
                    "country": r.get("country"),
                    "date": r.get("date", "").split("-")[0] if r.get("date") else None,
                    "barcode": r.get("barcode"),
                    "cat_no": r_cat,
                    "disambiguation": r.get("disambiguation"),
                    "has_cover_art": has_art,
                }
            if releases and "Matching releases:" not in out:
                rel_lines = ["\nMatching releases:\n"]
                for r in releases:
                    r_id = r.get("id", "")
                    r_title = r.get("title", "")
                    r_artist = ""
                    credits = r.get("artist-credit", [])
                    if credits:
                        r_artist = "".join(
                            (c.get("name", "") + c.get("joinphrase", "")) for c in credits if isinstance(c, dict)
                        ).strip()
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

        raw_blocks = [b.strip() for b in re.split(r"\n\s*\n+", rel_section) if b.strip()]
        if len(raw_blocks) == 1 and len(_MB_REL_RE.findall(raw_blocks[0])) > 1:
            lines = raw_blocks[0].splitlines()
            current_block = []
            new_blocks = []
            for line in lines:
                if re.match(r"^\s*(?:Artist|Title)\s*:", line) and any(_MB_REL_RE.search(l) for l in current_block):
                    new_blocks.append("\n".join(current_block))
                    current_block = [line]
                else:
                    current_block.append(line)
            if current_block:
                new_blocks.append("\n".join(current_block))
            raw_blocks = new_blocks

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

                parsed_country = m_c.group(1).strip() if m_c else None
                parsed_cat = m_cat.group(1).strip() if m_cat else None
                parsed_bar = m_bar.group(1).strip() if m_bar else None
                parsed_year = m_y.group(1).strip() if m_y else None
                info = mb_releases_map.get(mbid, {})

                raw_title = info.get("title") or (m_t.group(1).strip() if m_t else "Unknown Album")
                cleaned_title = clean_album_title(raw_title, disambiguation=info.get("disambiguation"))
                parsed_artist = m_a.group(1).strip() if m_a else "Unknown Artist"
                release_artist = info.get("artist") or parsed_artist

                matching_releases.append(
                    ReleaseOption(
                        mbid=mbid,
                        artist=release_artist,
                        title=cleaned_title,
                        country=info.get("country") or parsed_country,
                        cat_no=info.get("cat_no") or parsed_cat,
                        barcode=info.get("barcode") or parsed_bar,
                        year=info.get("date") or parsed_year,
                        duration=format_duration(m_bdur.group(1).strip()) if m_bdur else duration,
                        has_cover_art=info.get("has_cover_art", False),
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
    primary_title = clean_album_title(matching_releases[0].title) if matching_releases else "Unknown Album"
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
        if getattr(details, "artist", None):
            primary_artist = details.artist
            if matching_releases:
                matching_releases[0].artist = details.artist
        if getattr(details, "title", None):
            primary_title = clean_album_title(details.title)

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
