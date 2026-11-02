"""Service encapsulating Whipper extraction execution, CUE/log reporting, and audio inventory validation."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

from whipit.audio.metadata import _MBID_RE, clean_album_title, fetch_discogs_url_from_mbid
from whipit.config import format_time_span
from whipit.models import AlbumMetadata, RipExecutionResult
from whipit.naming import format_album_stem, sanitise_string, truncate_to_bytes
from whipit.ripping.engine import RippingEngine
from whipit.ripping.identify import _MB_REL_RE
from whipit.ripping.reports import (
    find_primary_cue,
    find_whipper_log,
    parse_cue_expected_tracks,
    parse_whipper_report,
)
from whipit.ui.base import LogLevel, UserInteractionPort
from whipit.validation.flac import get_flac_duration_seconds, read_flac_tag
from whipit.validation.tracks import validate_track_sequence

logger = logging.getLogger(__name__)


def _wf() -> Any:
    return sys.modules.get("whipit.workflow", sys.modules[__name__])


def _cue_escape(val: str) -> str:
    val = re.sub(r"[\x00-\x1f\x7f]", " ", val or "")
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _ascii_int(s: Optional[str], max_digits: int = 4) -> Optional[int]:
    clean = (s or "").split("/")[0].strip()
    return int(clean) if clean.isascii() and clean.isdecimal() and 0 < len(clean) <= max_digits else None


class ExtractionService:
    """Orchestrates optical disc extraction, audio track inspection, and metadata harvesting."""

    def __init__(self) -> None:
        self.engine: Optional[RippingEngine] = None

    def resolve_release_identification(
        self,
        device_path: str,
        release_id: Optional[str],
        current_metadata: AlbumMetadata,
        unknown_disc: bool,
        ui: UserInteractionPort,
        cancel_event: threading.Event,
    ) -> Tuple[bool, Optional[str], AlbumMetadata]:
        """Queries MusicBrainz if release_id is unspecified and prompts user if multiple pressings match."""
        if release_id or current_metadata.musicbrainz_id or unknown_disc or not ui.is_interactive():
            return True, release_id or current_metadata.musicbrainz_id, current_metadata

        from whipit.ripping.identify import inspect_disc

        ui.on_log(LogLevel.INFO, "Checking MusicBrainz for matching pressings...")
        insp = inspect_disc(device_path)
        chosen_id = release_id
        if insp.matching_releases and len(insp.matching_releases) > 1:
            chosen_mbid = ui.prompt_select_release(insp.matching_releases)
            if not chosen_mbid or cancel_event.is_set():
                ui.on_log(LogLevel.WARN, "Extraction cancelled at pressing selection.")
                return False, None, current_metadata
            chosen_id = chosen_mbid
        elif insp.matching_releases and len(insp.matching_releases) == 1:
            chosen_id = insp.matching_releases[0].mbid

        chosen_rel = None
        if insp.matching_releases:
            chosen_rel = next((r for r in insp.matching_releases if r.mbid == chosen_id), insp.matching_releases[0])

        rel_artist = chosen_rel.artist if chosen_rel and chosen_rel.artist != "Unknown Artist" else insp.artist
        rel_title = chosen_rel.title if chosen_rel and chosen_rel.title != "Unknown Album" else insp.title

        metadata = current_metadata
        if chosen_id and rel_artist != "Unknown Artist":
            clean_title = clean_album_title(rel_title)
            metadata = AlbumMetadata(
                artist=sanitise_string(rel_artist),
                album=sanitise_string(clean_title),
                album_stem=format_album_stem(rel_artist, clean_title),
                musicbrainz_id=chosen_id,
                musicbrainz_url=f"https://musicbrainz.org/release/{chosen_id}",
                musicbrainz_lookup_url=insp.mb_lookup_url,
                discogs_url=insp.discogs_url,
                year=current_metadata.year,
                genre=current_metadata.genre,
                custom_track_titles=current_metadata.custom_track_titles,
            )

        return True, chosen_id, metadata

    def extract_and_validate(
        self,
        device_path: str,
        speed: Optional[int],
        effective_offset: Optional[int],
        release_id: Optional[str],
        whipper_stage: Path,
        cache_dir: Path,
        full_log_path: Optional[Path],
        auto_eject: bool,
        keep_going: bool,
        unknown_disc: bool,
        current_metadata: AlbumMetadata,
        ui: UserInteractionPort,
        cancel_event: threading.Event,
    ) -> RipExecutionResult:
        wf = _wf()
        cls_engine = getattr(wf, "RippingEngine", RippingEngine)

        engine = cls_engine(
            cache_dir=cache_dir,
            stage_dir=whipper_stage,
            device_path=device_path,
            speed=speed,
            offset=effective_offset,
            release_id=release_id,
            ui=ui,
            cancel_event=cancel_event,
            full_log_path=full_log_path,
            auto_eject=auto_eject,
        )
        self.engine = engine

        rip_result = engine.run_extraction(keep_going=keep_going, unknown_disc=unknown_disc)

        exec_log = whipper_stage / "whipper_exec.log"
        if exec_log.exists() and full_log_path:
            try:
                shutil.copy2(exec_log, full_log_path)
                try:
                    os.chmod(full_log_path, 0o600)
                except OSError:
                    pass
            except Exception:
                pass

        if not rip_result.success or rip_result.cancelled or cancel_event.is_set():
            return RipExecutionResult(
                success=False,
                cancelled=bool(rip_result.cancelled or cancel_event.is_set()),
                partial=rip_result.partial,
                device_path=rip_result.device_path or device_path,
                effective_offset=rip_result.effective_offset,
                speed=rip_result.speed,
                elapsed_seconds=getattr(rip_result, "elapsed_seconds", None),
                average_speed_x=getattr(rip_result, "average_speed_x", None),
                content_dir=whipper_stage,
                flac_files=[],
                expected_tracks=0,
                health_status="Unknown",
                accuraterip_summary="Unknown",
                missing_tracks=[],
                metadata=current_metadata,
                error_message="Extraction halted or aborted by user.",
            )

        content_dir = whipper_stage
        while True:
            subdirs = [d for d in content_dir.iterdir() if d.is_dir()]
            subfiles = [f for f in content_dir.iterdir() if f.is_file() and f.name != "whipper_exec.log"]
            if len(subdirs) == 1 and len(subfiles) == 0:
                content_dir = subdirs[0]
            else:
                break

        flac_files = sorted(list(content_dir.glob("*.flac")))
        if not flac_files:
            return RipExecutionResult(
                success=False,
                cancelled=False,
                partial=rip_result.partial,
                device_path=rip_result.device_path,
                effective_offset=rip_result.effective_offset,
                speed=rip_result.speed,
                elapsed_seconds=getattr(rip_result, "elapsed_seconds", None),
                average_speed_x=getattr(rip_result, "average_speed_x", None),
                content_dir=content_dir,
                flac_files=[],
                expected_tracks=0,
                health_status="Unknown",
                accuraterip_summary="Unknown",
                missing_tracks=[],
                metadata=current_metadata,
                error_message="No audio tracks were extracted.",
            )

        fn_read_tag = getattr(wf, "read_flac_tag", read_flac_tag)
        fn_find_cue = getattr(wf, "find_primary_cue", find_primary_cue)
        fn_find_log = getattr(wf, "find_whipper_log", find_whipper_log)
        fn_parse_cue = getattr(wf, "parse_cue_expected_tracks", parse_cue_expected_tracks)
        fn_parse_rep = getattr(wf, "parse_whipper_report", parse_whipper_report)
        fn_flac_dur = getattr(wf, "get_flac_duration_seconds", get_flac_duration_seconds)
        fn_val_seq = getattr(wf, "validate_track_sequence", validate_track_sequence)

        sample_flac = flac_files[0] if flac_files else None
        tag_artist = "Unknown Artist"
        tag_album = "Unknown Album"
        if sample_flac:
            tag_artist = fn_read_tag(sample_flac, "ALBUMARTIST") or fn_read_tag(sample_flac, "ARTIST") or "Unknown Artist"
            tag_album = fn_read_tag(sample_flac, "ALBUM") or "Unknown Album"
        cleaned_album_tag = clean_album_title(tag_album)
        stub_stem = truncate_to_bytes(f"{sanitise_string(tag_artist)} - {sanitise_string(cleaned_album_tag)}", max_bytes=180)

        cue_path = fn_find_cue(whipper_stage)
        log_path = fn_find_log(whipper_stage)

        if not cue_path or not log_path:
            ui.on_log(LogLevel.WARN, "Missing CUE/log from partial rip; synthesizing metadata...")

        if not cue_path:
            cue_path = content_dir / f"{stub_stem} (WhipIt partial stub).cue"
            cue_artist = _cue_escape(tag_artist)
            cue_album = _cue_escape(tag_album)
            cue_lines = [f'PERFORMER "{cue_artist}"', f'TITLE "{cue_album} (Partial)"']
            for f in flac_files:
                tn = fn_read_tag(f, "TRACKNUMBER") or "1"
                t_num = _ascii_int(tn) or 1
                cue_lines.append(f'FILE "{f.name}" WAVE')
                cue_lines.append(f"  TRACK {t_num:02d} AUDIO")
                cue_lines.append("    INDEX 01 00:00:00")
            cue_path.write_text("\n".join(cue_lines) + "\n", encoding="utf-8")

        if not log_path:
            log_path = content_dir / f"{stub_stem} (WhipIt partial stub).log"
            log_lines = [
                "WhipIt synthesized stub log — NOT produced by whipper; no verification was performed.",
                "Health status: Extraction halted with errors (Partial)",
                "AccurateRip summary: Unverified (Partial rip)",
            ]
            for f in flac_files:
                log_lines.append(f"Filename: {f.name}")
                log_lines.append("Status: Not verified")
            log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        expected_tracks = fn_parse_cue(cue_path)
        if expected_tracks == 0:
            if exec_log.exists():
                m_tot = re.findall(r"(?:Reading|Copying|Ripping)\s+track\s+\d+\s+of\s+(\d+)", exec_log.read_text(errors="ignore"), re.I)
                if m_tot:
                    expected_tracks = max(int(t) for t in m_tot)
            if expected_tracks == 0:
                expected_tracks = len(flac_files)

        health, ar_summary, _, _ = fn_parse_rep(log_path)

        rip_elapsed = getattr(rip_result, "elapsed_seconds", None)
        total_audio_sec = sum(fn_flac_dur(f) or 0.0 for f in flac_files)

        avg_speed_x: Optional[float] = None
        if rip_elapsed and rip_elapsed > 0 and total_audio_sec > 0:
            avg_speed_x = round(total_audio_sec / rip_elapsed, 2)
            try:
                rip_result.average_speed_x = avg_speed_x
            except (AttributeError, TypeError):
                pass
            ui.on_log(
                LogLevel.INFO,
                f"Extraction throughput: {len(flac_files)} tracks [{format_time_span(total_audio_sec)} audio] in {format_time_span(rip_elapsed)} (Average speed: {avg_speed_x:.1f}x).",
            )

        seq_ok, errors = fn_val_seq(content_dir, expected_tracks)

        extracted_numbers = set()
        for f in flac_files:
            tag = fn_read_tag(f, "TRACKNUMBER")
            parsed_num = _ascii_int(tag)
            if parsed_num is not None:
                extracted_numbers.add(parsed_num)
        missing = [i for i in range(1, expected_tracks + 1) if i not in extracted_numbers]

        is_partial = rip_result.partial or not seq_ok or (expected_tracks > len(flac_files)) or bool(missing)
        has_health_errors = (health != "No errors occurred" and health != "Unknown")

        if is_partial and missing:
            ui.on_log(LogLevel.WARN, f"Partial extraction: {len(flac_files)} of {expected_tracks} tracks readable. Missing tracks: {missing}")
        elif has_health_errors:
            ui.on_log(LogLevel.WARN, f"Extraction health note: {health}")

        metadata = current_metadata
        if sample_flac:
            artist = fn_read_tag(sample_flac, "ALBUMARTIST") or fn_read_tag(sample_flac, "ARTIST") or "Unknown Artist"
            album = fn_read_tag(sample_flac, "ALBUM") or "Unknown Album"
            disc_str = fn_read_tag(sample_flac, "DISCNUMBER") or fn_read_tag(sample_flac, "DISC")
            total_disc_str = fn_read_tag(sample_flac, "DISCTOTAL") or fn_read_tag(sample_flac, "TOTALDISCS")

            disc_no = _ascii_int(disc_str)
            tot_discs = _ascii_int(total_disc_str)

            mbid = release_id or fn_read_tag(sample_flac, "MUSICBRAINZ_ALBUMID") or fn_read_tag(sample_flac, "MUSICBRAINZ_RELEASEID")
            if not mbid and exec_log.exists():
                m_mb = _MB_REL_RE.search(exec_log.read_text(errors="ignore"))
                if m_mb:
                    mbid = m_mb.group(1).lower()
            mbid = mbid.strip().lower() if (mbid and _MBID_RE.fullmatch(mbid.strip())) else None

            lookup_url = None
            if exec_log.exists():
                m_look = re.search(r"MusicBrainz lookup URL\s*[:\s]\s*(https?://musicbrainz\.org/cdtoc/attach\S+)", exec_log.read_text(errors="ignore"))
                if m_look:
                    lookup_url = m_look.group(1).strip()

            mb_url = f"https://musicbrainz.org/release/{mbid}" if mbid else None
            try:
                discogs_url = fetch_discogs_url_from_mbid(mbid, artist=artist, title=album) if mbid else None
            except Exception as exc:
                ui.on_log(LogLevel.WARN, f"Metadata enrichment skipped: {exc}")
                discogs_url = None

            if mb_url:
                ui.on_log(LogLevel.INFO, f"MusicBrainz Release: {mb_url}")
            if discogs_url:
                ui.on_log(LogLevel.INFO, f"Discogs Entry: {discogs_url}")
            if lookup_url:
                ui.on_log(LogLevel.INFO, f"MusicBrainz Submit/Attach URL: {lookup_url}")

            raw_artist = current_metadata.artist if current_metadata.artist != "Unknown Artist" else artist
            raw_album = current_metadata.album if current_metadata.album != "Unknown Album" else album
            if raw_artist == raw_album and artist != "Unknown Artist" and artist != album:
                raw_artist = artist
            final_artist = sanitise_string(raw_artist)
            final_album = sanitise_string(clean_album_title(raw_album))
            final_disc_no = current_metadata.disc_number if current_metadata.disc_number is not None else disc_no
            final_tot_discs = current_metadata.total_discs if current_metadata.total_discs is not None else tot_discs
            stem = format_album_stem(final_artist, final_album)

            metadata = AlbumMetadata(
                artist=final_artist,
                album=final_album,
                disc_number=final_disc_no,
                total_discs=final_tot_discs,
                album_stem=stem,
                musicbrainz_id=mbid,
                musicbrainz_url=mb_url,
                musicbrainz_lookup_url=lookup_url,
                discogs_url=discogs_url,
                year=current_metadata.year,
                genre=current_metadata.genre,
                custom_track_titles=current_metadata.custom_track_titles,
            )

        return RipExecutionResult(
            success=True,
            cancelled=False,
            partial=is_partial,
            device_path=rip_result.device_path,
            effective_offset=rip_result.effective_offset,
            speed=rip_result.speed,
            elapsed_seconds=rip_elapsed,
            average_speed_x=avg_speed_x,
            content_dir=content_dir,
            flac_files=flac_files,
            expected_tracks=expected_tracks,
            health_status=health,
            accuraterip_summary=ar_summary,
            missing_tracks=missing,
            metadata=metadata,
            cue_path=cue_path,
            log_path=log_path,
            raw_tag_artist=tag_artist,
            raw_tag_album=tag_album,
        )
