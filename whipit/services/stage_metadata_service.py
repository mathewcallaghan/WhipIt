"""Service encapsulating candidate track tag normalization, CUE sheet updating, and artwork staging."""

from __future__ import annotations

import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import whipit.ripping.reports as ri_reports
import whipit.validation.flac as val_flac
import whipit.validation.integrity as val_integrity
from whipit.audio.metadata import (
    _MAX_ART_BYTES,
    ART_NAMES,
    _image_ext,
    fetch_cover_art_from_caa,
    find_cover_art,
)
from whipit.models import AlbumMetadata, CandidateStagingResult
from whipit.naming import format_track_filename, sanitise_string
from whipit.ui.base import LogLevel, UserInteractionPort

logger = logging.getLogger(__name__)


def _wf() -> Any:
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
        wf = _wf()
        fn_read = getattr(wf, "read_flac_tag", val_flac.read_flac_tag)
        fn_write = getattr(wf, "write_flac_tags", val_flac.write_flac_tags)
        fn_fmt = getattr(wf, "format_track_filename", format_track_filename)
        fn_ai = getattr(wf, "_ascii_int", _ascii_int)

        flacs = sorted(list(stage_flac.glob("*.flac")))
        renamed_mapping: List[Tuple[int, str, Path]] = []
        old_to_new_names: Dict[str, str] = {}

        for f in flacs:
            tn_tag = fn_read(f, "TRACKNUMBER") or "1"
            t_num = fn_ai(tn_tag) or 1
            custom_title = meta.custom_track_titles.get(t_num)
            track_title = custom_title or fn_read(f, "TITLE") or f"Track {t_num}"
            track_artist = meta.artist if meta.artist != "Unknown Artist" else (fn_read(f, "ARTIST") or "Unknown Artist")

            if apply_tags:
                tags: Dict[str, str] = {}
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
                fn_write(f, tags)

            new_fname = fn_fmt(t_num, track_artist, track_title)
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
        old_to_new_names: Dict[str, str],
        tracks: List[Tuple[int, str, Path]],
    ) -> None:
        wf = _wf()
        fn_cue = getattr(wf, "find_primary_cue", ri_reports.find_primary_cue)
        fn_esc = getattr(wf, "_cue_escape", _cue_escape)

        cue_path = fn_cue(stage_flac)
        if not cue_path:
            return
        try:
            content = cue_path.read_text(encoding="utf-8", errors="replace")
            for old_n, new_n in old_to_new_names.items():
                content = content.replace(f'FILE "{old_n}"', f'FILE "{fn_esc(new_n)}"')
                content = content.replace(f'FILE "{fn_esc(old_n)}"', f'FILE "{fn_esc(new_n)}"')
            for t_num, _, new_path in tracks:
                esc_new = fn_esc(new_path.name)
                content = re.sub(
                    rf'FILE\s+".*?"\s+WAVE(?=\s*\n\s*TRACK\s+0*{t_num}\s+AUDIO)',
                    f'FILE "{esc_new}" WAVE',
                    content,
                    flags=re.IGNORECASE,
                )
            if meta.artist and meta.artist != "Unknown Artist":
                content = re.sub(r'^PERFORMER\s+".*?"', f'PERFORMER "{fn_esc(meta.artist)}"', content, flags=re.M)
            if meta.album and meta.album != "Unknown Album":
                content = re.sub(r'^TITLE\s+".*?"', f'TITLE "{fn_esc(meta.album)}"', content, flags=re.M)
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

    def stage_candidate(
        self,
        content_dir: Path,
        candidate_stage: Path,
        candidate_stage_opus: Optional[Path],
        generate_opus: bool,
        expected_tracks: int,
        flac_files: List[Path],
        is_partial: bool,
        metadata: AlbumMetadata,
        raw_tag_artist: str,
        raw_tag_album: str,
        user_art: Optional[Path],
        ui: UserInteractionPort,
    ) -> CandidateStagingResult:
        wf = _wf()
        fn_vrc = getattr(wf, "verify_release_candidate", val_integrity.verify_release_candidate)
        fn_ct = getattr(wf, "compare_trees", val_integrity.compare_trees)
        fn_sha = getattr(wf, "flac_audio_sha256", val_flac.flac_audio_sha256)

        candidate_stage.mkdir(parents=True, exist_ok=True)
        stage_flac = candidate_stage / "flac"
        if stage_flac.exists():
            shutil.rmtree(stage_flac, ignore_errors=True)
        shutil.copytree(content_dir, stage_flac)
        (stage_flac / "whipper_exec.log").unlink(missing_ok=True)

        stage_opus: Optional[Path] = None
        if generate_opus:
            if candidate_stage_opus:
                candidate_stage_opus.mkdir(parents=True, exist_ok=True)
                stage_opus = candidate_stage_opus / "opus"
            else:
                stage_opus = candidate_stage / "opus"
            stage_opus.mkdir(parents=True, exist_ok=True)

        c_ok, c_errs = fn_vrc(
            stage_flac,
            expected_count=len(flac_files) if is_partial else expected_tracks,
            total_tracks=expected_tracks,
            allow_partial=is_partial,
        )
        if not c_ok or not fn_ct(content_dir, stage_flac, ["whipper_exec.log"]):
            return CandidateStagingResult(
                success=False,
                stage_flac=stage_flac,
                stage_opus=stage_opus,
                flac_files=[],
                cover_art=None,
                error_message="Candidate staging verification failed.",
                verification_errors=c_errs,
            )

        has_custom_meta = bool(
            (metadata.artist != "Unknown Artist" and metadata.artist != sanitise_string(raw_tag_artist))
            or (metadata.album != "Unknown Album" and metadata.album != sanitise_string(raw_tag_album))
            or metadata.year
            or metadata.genre
            or metadata.custom_track_titles
        )

        if has_custom_meta:
            ui.on_log(LogLevel.INFO, "Applying custom metadata tags to candidate FLAC tracks...")
            self.normalize_candidate_tracks(stage_flac, metadata, apply_tags=True)
        else:
            ui.on_log(LogLevel.INFO, "Normalizing track filenames for portable cross-platform filesystem compatibility...")
            self.normalize_candidate_tracks(stage_flac, metadata, apply_tags=False)

        (candidate_stage / ".staged-ok").write_text(
            "".join(f"{p.name}\t{fn_sha(p) or '-'}\n" for p in sorted(stage_flac.glob("*.flac"))),
            encoding="utf-8",
        )

        for f in content_dir.glob("*.flac"):
            f.unlink(missing_ok=True)
        for f in content_dir.glob("*.wav"):
            f.unlink(missing_ok=True)

        c_flacs = sorted(list(stage_flac.glob("*.flac")))
        cover_art = self.resolve_and_stage_artwork(
            stage_flac=stage_flac,
            user_art=user_art,
            mbid=metadata.musicbrainz_id,
            ui=ui,
        )

        return CandidateStagingResult(
            success=True,
            stage_flac=stage_flac,
            stage_opus=stage_opus,
            flac_files=c_flacs,
            cover_art=cover_art,
        )

    def replicate_multidisc_artwork(
        self,
        final_dir: Path,
        cover_art: Optional[Path],
    ) -> None:
        if not cover_art or not cover_art.exists() or not final_dir.parent.exists():
            return
        parent_art = final_dir.parent / cover_art.name
        if not parent_art.exists():
            try:
                shutil.copy2(cover_art, parent_art)
            except OSError:
                pass
