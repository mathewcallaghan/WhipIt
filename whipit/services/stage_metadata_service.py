"""Service encapsulating candidate track tag normalization, CUE sheet updating, and artwork staging."""

from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from whipit.audio.metadata import (
    _MAX_ART_BYTES,
    ART_NAMES,
    _image_ext,
    fetch_cover_art_from_caa,
    find_cover_art,
)
from whipit.models import AlbumMetadata
from whipit.naming import format_track_filename
from whipit.ui.base import LogLevel, UserInteractionPort
from whipit.validation.flac import read_flac_tag, write_flac_tags

logger = logging.getLogger(__name__)


def _wf() -> Any:
    """Dynamically resolves whipit.workflow to honor test monkeypatching across the legacy interface."""
    return sys.modules.get("whipit.workflow", sys.modules[__name__])


def _cue_escape(val: str) -> str:
    val = re.sub(r"[\x00-\x1f\x7f]", " ", val or "")
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _ascii_int(s: Optional[str], max_digits: int = 4) -> Optional[int]:
    clean = (s or "").split("/")[0].strip()
    return int(clean) if clean.isascii() and clean.isdecimal() and 0 < len(clean) <= max_digits else None


class StageMetadataService:
    """Handles candidate metadata application, file renaming, CUE sheet synchronization, and cover art resolution."""

    def normalize_candidate_tracks(
        self,
        stage_flac: Path,
        meta: AlbumMetadata,
        apply_tags: bool = False,
    ) -> None:
        """Renames candidate FLAC tracks to portable filenames, applies custom tags, and updates CUE sheets."""
        wf = _wf()
        fn_read_tag = getattr(wf, "read_flac_tag", read_flac_tag)
        fn_write_tags = getattr(wf, "write_flac_tags", write_flac_tags)
        fn_format_name = getattr(wf, "format_track_filename", format_track_filename)
        fn_ascii_int = getattr(wf, "_ascii_int", _ascii_int)

        flacs = sorted(list(stage_flac.glob("*.flac")))
        renamed_mapping: List[Tuple[int, str, Path]] = []
        old_to_new_names: dict[str, str] = {}

        for f in flacs:
            tn_tag = fn_read_tag(f, "TRACKNUMBER") or "1"
            t_num = fn_ascii_int(tn_tag) or 1
            custom_title = meta.custom_track_titles.get(t_num)
            track_title = custom_title or fn_read_tag(f, "TITLE") or f"Track {t_num}"
            track_artist = meta.artist if meta.artist != "Unknown Artist" else (fn_read_tag(f, "ARTIST") or "Unknown Artist")

            if apply_tags:
                tags: dict[str, str] = {}
                if meta.artist and meta.artist != "Unknown Artist":
                    tags["ARTIST"] = meta.artist
                    tags["ALBUMARTIST"] = meta.artist
                if meta.album and meta.album != "Unknown Album":
                    tags["ALBUM"] = meta.album
                if custom_title:
                    tags["TITLE"] = custom_title
                if meta.year:
                    tags["DATE"] = meta.year
                if meta.genre:
                    tags["GENRE"] = meta.genre
                fn_write_tags(f, tags)

            new_fname = fn_format_name(t_num, track_artist, track_title)
            new_path = stage_flac / new_fname
            old_name = f.name
            if new_path != f:
                if new_path.exists():
                    new_path.unlink(missing_ok=True)
                f.rename(new_path)
                old_to_new_names[old_name] = new_fname
                renamed_mapping.append((t_num, track_title, new_path))
            else:
                renamed_mapping.append((t_num, track_title, f))

        self.update_cue_sheet_metadata(stage_flac, meta, old_to_new_names, renamed_mapping)

    def update_cue_sheet_metadata(
        self,
        stage_flac: Path,
        meta: AlbumMetadata,
        old_to_new_names: dict[str, str],
        tracks: List[Tuple[int, str, Path]],
    ) -> None:
        """Updates CUE sheet references with sanitized filenames, artist, and album titles."""
        from whipit.ripping.reports import find_primary_cue

        wf = _wf()
        fn_find_cue = getattr(wf, "find_primary_cue", find_primary_cue)
        fn_cue_escape = getattr(wf, "_cue_escape", _cue_escape)

        cue_path = fn_find_cue(stage_flac)
        if not cue_path:
            return
        try:
            content = cue_path.read_text(encoding="utf-8", errors="replace")
            for old_n, new_n in old_to_new_names.items():
                content = content.replace(f'FILE "{old_n}"', f'FILE "{fn_cue_escape(new_n)}"')
                content = content.replace(f'FILE "{fn_cue_escape(old_n)}"', f'FILE "{fn_cue_escape(new_n)}"')
            for t_num, _, new_path in tracks:
                esc_new = fn_cue_escape(new_path.name)
                content = re.sub(
                    rf'FILE\s+".*?"\s+WAVE(?=\s*\n\s*TRACK\s+0*{t_num}\s+AUDIO)',
                    f'FILE "{esc_new}" WAVE',
                    content,
                    flags=re.IGNORECASE,
                )
            if meta.artist and meta.artist != "Unknown Artist":
                content = re.sub(r'^PERFORMER\s+".*?"', f'PERFORMER "{fn_cue_escape(meta.artist)}"', content, flags=re.M)
            if meta.album and meta.album != "Unknown Album":
                content = re.sub(r'^TITLE\s+".*?"', f'TITLE "{fn_cue_escape(meta.album)}"', content, flags=re.M)
            cue_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not update CUE sheet with custom metadata: %s", exc)

    def resolve_and_stage_artwork(
        self,
        stage_flac: Path,
        user_art: Optional[Path],
        mbid: Optional[str],
        ui: UserInteractionPort,
    ) -> Optional[Path]:
        """Validates user-selected art, discovers local directory art, or fetches from Cover Art Archive."""
        cover_art: Optional[Path] = None

        if user_art and user_art.is_file():
            data = b""
            try:
                with open(user_art, "rb") as fh:
                    data = fh.read(_MAX_ART_BYTES + 1)
            except OSError:
                data = b""

            ext = _image_ext(data[:16])
            if ext and len(data) <= _MAX_ART_BYTES:
                target_art = stage_flac / f"cover{ext}"
                for old_name in ART_NAMES:
                    for old_f in stage_flac.glob(old_name):
                        if old_f.resolve() != target_art.resolve():
                            old_f.unlink(missing_ok=True)
                target_art.write_bytes(data)
                cover_art = target_art
                ui.on_log(LogLevel.INFO, f"Applied album cover art: {cover_art.name}")
            else:
                ui.on_log(
                    LogLevel.WARN,
                    f"Selected cover art '{user_art.name}' was rejected "
                    f"(must be a JPEG/PNG of at most {_MAX_ART_BYTES // (1024 * 1024)} MB); ignoring it.",
                )
                cover_art = find_cover_art(stage_flac)
        else:
            if user_art:
                ui.on_log(
                    LogLevel.WARN,
                    f"Selected cover art '{user_art}' is no longer available; ignoring it.",
                )
            cover_art = find_cover_art(stage_flac)
            if not cover_art and mbid:
                downloaded = fetch_cover_art_from_caa(mbid)
                if downloaded and downloaded.is_file():
                    ext = downloaded.suffix.lower()
                    target_art = stage_flac / f"cover{ext}"
                    try:
                        shutil.copy2(downloaded, target_art)
                        cover_art = target_art
                        ui.on_log(LogLevel.INFO, f"Retrieved album cover art from Cover Art Archive: {cover_art.name}")
                    except OSError:
                        pass

        return cover_art
