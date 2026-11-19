"""Service encapsulating candidate track tag normalization, CUE sheet updating, and artwork staging."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, FrozenSet, List, Optional, Set, Tuple

import whipit.audio.opus as aud_opus
import whipit.audio.replaygain as aud_rg
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
from whipit.models import AlbumMetadata, CandidateStagingResult, require_within_root
from whipit.naming import format_track_filename, sanitize_filename, truncate_to_bytes
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


def _literal_repl(rep: str) -> Callable[[re.Match[str]], str]:
    return lambda _match: rep


RELEASE_GROUPING_TAGS_TO_CLEAR = (
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_RELEASEID",
    "MUSICBRAINZ_RELEASEGROUPID",
    "MUSICBRAINZ_ALBUMARTISTID",
    "LABELID",
    "ALBUM_GROUPING_KEY",
    "ALBUMSORT",
    "ALBUMARTISTSORT",
    "DISCSUBTITLE",
    "SETSUBTITLE",
)

GROUPING_UNIFORMITY_KEYS = (
    "MUSICBRAINZ_ALBUMID",
    "MUSICBRAINZ_RELEASEID",
    "LABELID",
    "ALBUM_GROUPING_KEY",
)


class StageMetadataService:
    """Handles candidate metadata application, file renaming, CUE sheet synchronization, and cover art resolution."""

    def inspect_stage_consistency(
        self,
        staged_flacs: List[Path],
        meta: AlbumMetadata,
        fn_read: Any,
    ) -> Tuple[bool, bool, Dict[str, str]]:
        """Checks staged tracks for ALBUM/ALBUMARTIST mismatches and partial/conflicting grouping tags."""
        if not staged_flacs:
            return False, False, {}

        needs_norm = False
        strip_grouping = bool(getattr(meta, "clear_musicbrainz_release_id", False))
        grouping_updates: Dict[str, str] = {}

        if meta.artist != "Unknown Artist":
            for f in staged_flacs:
                if fn_read(f, "ALBUMARTIST") != meta.artist:
                    needs_norm = True
                    break

        if meta.album != "Unknown Album":
            for f in staged_flacs:
                if fn_read(f, "ALBUM") != meta.album:
                    needs_norm = True
                    break

        if not strip_grouping:
            for key in GROUPING_UNIFORMITY_KEYS:
                vals = [((fn_read(f, key) or "").strip() or None) for f in staged_flacs]
                non_empty = {v for v in vals if v is not None}
                if len(non_empty) > 1:
                    strip_grouping = True
                    grouping_updates.clear()
                    needs_norm = True
                    break
                if len(non_empty) == 1 and any(v is None for v in vals):
                    only_val = next(iter(non_empty))
                    if key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEID"):
                        if meta.musicbrainz_id and only_val.lower() == meta.musicbrainz_id.lower():
                            grouping_updates[key] = meta.musicbrainz_id
                            needs_norm = True
                        else:
                            strip_grouping = True
                            grouping_updates.clear()
                            needs_norm = True
                            break
                    else:
                        grouping_updates[key] = only_val
                        needs_norm = True

        if strip_grouping:
            needs_norm = True

        return needs_norm, strip_grouping, grouping_updates

    def normalize_candidate_tracks(
        self,
        stage_flac: Path,
        meta: AlbumMetadata,
        apply_tags: bool = False,
        override_track_artist: bool = True,
        force_strip_grouping_tags: bool = False,
        grouping_tag_updates: Optional[Dict[str, str]] = None,
    ) -> None:
        wf = _wf()
        fn_read = getattr(wf, "read_flac_tag", val_flac.read_flac_tag)
        fn_write = getattr(wf, "write_flac_tags", val_flac.write_flac_tags)
        fn_remove = getattr(wf, "remove_flac_tags", val_flac.remove_flac_tags)
        fn_fmt = getattr(wf, "format_track_filename", format_track_filename)
        fn_ai = getattr(wf, "_ascii_int", _ascii_int)

        flacs = sorted(list(stage_flac.glob("*.flac")))
        existing_artists = [
            a.strip()
            for a in (fn_read(f, "ARTIST") for f in flacs)
            if a and a.strip() and a.strip() != "Unknown Artist"
        ]
        has_heterogeneous_track_artists = len(set(existing_artists)) > 1
        effective_override_track_artist = override_track_artist and not has_heterogeneous_track_artists

        renamed_mapping: List[Tuple[int, str, Path]] = []
        old_to_new_names: Dict[str, str] = {}

        for f in flacs:
            tn_tag = fn_read(f, "TRACKNUMBER") or "1"
            t_num = fn_ai(tn_tag) or 1
            custom_title = meta.custom_track_titles.get(t_num)
            custom_artist = getattr(meta, "custom_track_artists", {}).get(t_num)
            track_title = custom_title or fn_read(f, "TITLE") or f"Track {t_num}"
            existing_track_artist = fn_read(f, "ARTIST")
            has_existing_artist = bool(existing_track_artist and existing_track_artist != "Unknown Artist")

            if custom_artist:
                track_artist = custom_artist
            elif meta.artist and meta.artist != "Unknown Artist" and (effective_override_track_artist or not has_existing_artist):
                track_artist = meta.artist
            else:
                track_artist = existing_track_artist or "Unknown Artist"

            if apply_tags:
                tags: Dict[str, str] = {}
                if custom_artist:
                    tags["ARTIST"] = custom_artist
                    if meta.artist and meta.artist != "Unknown Artist":
                        tags["ALBUMARTIST"] = meta.artist
                elif meta.artist and meta.artist != "Unknown Artist":
                    tags["ALBUMARTIST"] = meta.artist
                    if effective_override_track_artist or not has_existing_artist:
                        tags["ARTIST"] = meta.artist
                if meta.album and meta.album != "Unknown Album":
                    tags["ALBUM"] = meta.album
                if custom_title:
                    tags["TITLE"] = custom_title
                if meta.year:
                    tags["DATE"] = meta.year
                if meta.genre:
                    tags["GENRE"] = meta.genre
                if meta.disc_number is not None:
                    tags["DISCNUMBER"] = str(meta.disc_number)
                if meta.total_discs is not None:
                    tags["DISCTOTAL"] = str(meta.total_discs)
                    tags["TOTALDISCS"] = str(meta.total_discs)

                tags_to_remove: List[str] = []
                if "ALBUMARTIST" in tags and fn_read(f, "ALBUM ARTIST"):
                    tags_to_remove.append("ALBUM ARTIST")
                if force_strip_grouping_tags or getattr(meta, "clear_musicbrainz_release_id", False):
                    tags_to_remove.extend(RELEASE_GROUPING_TAGS_TO_CLEAR)
                    if getattr(meta, "clear_musicbrainz_release_id", False):
                        if not meta.year:
                            tags_to_remove.append("DATE")
                        if meta.disc_number is None and meta.total_discs is None:
                            tags_to_remove.extend(("DISCNUMBER", "DISCTOTAL", "TOTALDISCS"))
                elif grouping_tag_updates:
                    for g_key, g_val in grouping_tag_updates.items():
                        if g_val and fn_read(f, g_key) != g_val:
                            tags[g_key] = g_val

                if tags and fn_write(f, tags) is False:
                    logger.warning("Failed writing metadata tags to %s", f.name)
                if tags_to_remove and fn_remove(f, tags_to_remove) is False:
                    logger.warning("Failed removing stale metadata tags from %s", f.name)

            new_fname = fn_fmt(t_num, track_artist, track_title)
            new_path = stage_flac / new_fname
            old_name = f.name
            if new_path != f:
                if new_path.exists():
                    stem_raw = Path(new_fname).stem
                    ext = Path(new_fname).suffix or ".flac"
                    max_stem = 255 - len(ext.encode("utf-8"))
                    counter = 2
                    while new_path.exists() and new_path != f:
                        suffix_tag = f" ({counter})"
                        safe_stem = truncate_to_bytes(
                            stem_raw,
                            max_bytes=max_stem - len(suffix_tag.encode("utf-8")),
                        )
                        new_fname = f"{safe_stem}{suffix_tag}{ext}"
                        new_path = stage_flac / new_fname
                        counter += 1
                f.rename(new_path)
                old_to_new_names[old_name] = new_fname
                renamed_mapping.append((t_num, track_title, new_path))
            else:
                renamed_mapping.append((t_num, track_title, f))

        self.normalize_sidecar_filenames(stage_flac)
        self.update_cue_sheet_metadata(stage_flac, meta, old_to_new_names, renamed_mapping)

    SIDECAR_EXTENSIONS: ClassVar[FrozenSet[str]] = frozenset({".cue", ".log", ".toc", ".m3u", ".m3u8"})

    def normalize_sidecar_filenames(self, stage_flac: Path) -> None:
        """Sanitizes sidecar filenames (.cue, .log, .toc, .m3u) for cross-platform and SMB compatibility."""
        wf = _wf()
        fn_sanitize = getattr(wf, "sanitize_filename", sanitize_filename)
        for p in list(stage_flac.iterdir()):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in self.SIDECAR_EXTENSIONS:
                continue
            if p.name == "whipper_exec.log":
                continue

            max_stem = 255 - len(ext.encode("utf-8"))
            safe_stem = fn_sanitize(p.stem, max_bytes=max_stem)
            new_name = f"{safe_stem}{ext}"
            new_path = p.parent / new_name

            if new_path != p:
                if new_path.exists():
                    counter = 2
                    while new_path.exists() and new_path != p:
                        tag = f" ({counter})"
                        b_stem = truncate_to_bytes(safe_stem, max_bytes=max_stem - len(tag.encode("utf-8")))
                        new_path = p.parent / f"{b_stem}{tag}{ext}"
                        counter += 1
                p.rename(new_path)

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
                    rf'FILE\s+"(?:\\.|[^"\\])*"\s+WAVE(?=\s*\n\s*TRACK\s+0*{t_num}\s+AUDIO)',
                    _literal_repl(f'FILE "{esc_new}" WAVE'),
                    content,
                    flags=re.IGNORECASE,
                )
            if meta.artist and meta.artist != "Unknown Artist":
                content = re.sub(
                    r'^PERFORMER\s+"(?:\\.|[^"\\])*"',
                    _literal_repl(f'PERFORMER "{fn_esc(meta.artist)}"'),
                    content,
                    flags=re.MULTILINE,
                )
            if meta.album and meta.album != "Unknown Album":
                content = re.sub(
                    r'^TITLE\s+"(?:\\.|[^"\\])*"',
                    _literal_repl(f'TITLE "{fn_esc(meta.album)}"'),
                    content,
                    flags=re.MULTILINE,
                )
            if meta.disc_number is not None:
                if re.search(r"^REM\s+DISCNUMBER\s+\d+", content, flags=re.MULTILINE | re.IGNORECASE):
                    content = re.sub(
                        r"^REM\s+DISCNUMBER\s+\d+",
                        f"REM DISCNUMBER {meta.disc_number}",
                        content,
                        flags=re.MULTILINE | re.IGNORECASE,
                    )
                else:
                    content = f"REM DISCNUMBER {meta.disc_number}\n" + content
            if meta.total_discs is not None:
                if re.search(r"^REM\s+TOTALDISCS\s+\d+", content, flags=re.MULTILINE | re.IGNORECASE):
                    content = re.sub(
                        r"^REM\s+TOTALDISCS\s+\d+",
                        f"REM TOTALDISCS {meta.total_discs}",
                        content,
                        flags=re.MULTILINE | re.IGNORECASE,
                    )
                else:
                    content = f"REM TOTALDISCS {meta.total_discs}\n" + content
            if getattr(meta, "custom_track_artists", None):
                for t_num, t_art in meta.custom_track_artists.items():
                    if t_art:
                        esc_art = fn_esc(t_art)
                        track_block_re = rf"(TRACK\s+0*{t_num}\s+AUDIO(?:\s*\n\s*(?!TRACK\b)[^\n]+)*)"
                        def _repl_track_block(m: re.Match[str], performer: str = esc_art) -> str:
                            block = m.group(1)
                            if re.search(r'^\s*PERFORMER\s+"(?:\\.|[^"\\])*"', block, flags=re.MULTILINE):
                                return re.sub(
                                    r'^\s*PERFORMER\s+"(?:\\.|[^"\\])*"',
                                    _literal_repl(f'    PERFORMER "{performer}"'),
                                    block,
                                    flags=re.MULTILINE,
                                )
                            lines = block.splitlines()
                            lines.insert(1, f'    PERFORMER "{performer}"')
                            return "\n".join(lines)
                        content = re.sub(track_block_re, _repl_track_block, content, flags=re.IGNORECASE)
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
        art_mbid: Optional[str] = None,
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

        fn_read = getattr(wf, "read_flac_tag", val_flac.read_flac_tag)
        staged_flacs = sorted(stage_flac.glob("*.flac"))
        artist_overridden = bool(
            metadata.artist != "Unknown Artist" and metadata.artist != raw_tag_artist
        )
        album_tag_mismatch = bool(
            metadata.album != "Unknown Album" and metadata.album != raw_tag_album
        )
        needs_consistency_norm, strip_grouping, grouping_updates = self.inspect_stage_consistency(
            staged_flacs,
            metadata,
            fn_read,
        )
        has_custom_meta = bool(
            artist_overridden
            or album_tag_mismatch
            or needs_consistency_norm
            or metadata.year
            or metadata.genre
            or metadata.custom_track_titles
            or getattr(metadata, "custom_track_artists", None)
            or metadata.disc_number is not None
            or metadata.total_discs is not None
            or getattr(metadata, "clear_musicbrainz_release_id", False)
        )

        if has_custom_meta:
            ui.on_log(LogLevel.INFO, "Applying normalized metadata tags to candidate FLAC tracks...")
            self.normalize_candidate_tracks(
                stage_flac,
                metadata,
                apply_tags=True,
                override_track_artist=artist_overridden,
                force_strip_grouping_tags=strip_grouping,
                grouping_tag_updates=grouping_updates,
            )
        else:
            ui.on_log(LogLevel.INFO, "Normalizing track filenames for portable cross-platform filesystem compatibility...")
            self.normalize_candidate_tracks(
                stage_flac,
                metadata,
                apply_tags=False,
                override_track_artist=False,
            )

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
            mbid=art_mbid or metadata.musicbrainz_id,
            ui=ui,
        )

        return CandidateStagingResult(
            success=True,
            stage_flac=stage_flac,
            stage_opus=stage_opus,
            flac_files=c_flacs,
            cover_art=cover_art,
        )

    @staticmethod
    def _find_sibling_disc_dirs(final_flac_dir: Optional[Path], flac_root: Path) -> List[Path]:
        if final_flac_dir is None or not re.fullmatch(r"Disc \d+", final_flac_dir.name):
            return []
        album_dir = final_flac_dir.parent
        if not album_dir.exists() or not album_dir.is_dir() or album_dir.is_symlink():
            return []
        if album_dir.resolve(strict=False) == flac_root.resolve(strict=False):
            return []
        target_resolved = final_flac_dir.resolve(strict=False)
        siblings: List[Path] = []
        for entry in sorted(album_dir.iterdir()):
            if (
                entry.is_dir()
                and not entry.is_symlink()
                and re.fullmatch(r"Disc \d+", entry.name)
                and entry.resolve(strict=False) != target_resolved
            ):
                siblings.append(entry)
        return siblings

    def _analyze_cross_disc_plan(
        self,
        current_flacs: List[Path],
        sibling_flacs: List[Path],
        sibling_dirs: List[Path],
        metadata: AlbumMetadata,
        fn_read: Any,
    ) -> Tuple[bool, Set[str], Optional[int]]:
        """
        Determines whether grouping tags must be stripped or reconciled across discs,
        and computes the unified TOTALDISCS value.
        Returns: (strip_all_grouping, extra_keys_to_strip, unified_total_discs)
        """
        all_flacs = current_flacs + sibling_flacs
        strip_all_grouping = bool(getattr(metadata, "clear_musicbrainz_release_id", False))
        extra_keys_to_strip: Set[str] = set()

        if not strip_all_grouping:
            for mb_key in ("MUSICBRAINZ_ALBUMID", "MUSICBRAINZ_RELEASEID"):
                vals = [((fn_read(f, mb_key) or "").strip().lower() or None) for f in all_flacs]
                non_empty = {v for v in vals if v is not None}
                if len(non_empty) > 1 or (len(non_empty) == 1 and any(v is None for v in vals)):
                    strip_all_grouping = True
                    break

        if not strip_all_grouping:
            for grp_key in ("LABELID", "ALBUM_GROUPING_KEY"):
                vals = [((fn_read(f, grp_key) or "").strip() or None) for f in all_flacs]
                non_empty = {v for v in vals if v is not None}
                if len(non_empty) > 1 or (len(non_empty) == 1 and any(v is None for v in vals)):
                    extra_keys_to_strip.add(grp_key)

        disc_numbers: Set[int] = {int(d.name.split()[1]) for d in sibling_dirs}
        if metadata.disc_number is not None:
            disc_numbers.add(metadata.disc_number)

        existing_totals: List[int] = []
        for f in all_flacs:
            t_val = _ascii_int(fn_read(f, "TOTALDISCS") or fn_read(f, "DISCTOTAL"))
            if t_val is not None:
                existing_totals.append(t_val)

        candidates = existing_totals + list(disc_numbers)
        if metadata.total_discs is not None:
            candidates.append(metadata.total_discs)
        unified_total: Optional[int] = max(candidates) if candidates else None
        if unified_total is not None and unified_total <= 1 and len(disc_numbers) <= 1:
            unified_total = metadata.total_discs

        return strip_all_grouping, extra_keys_to_strip, unified_total

    def align_stage_with_published_siblings(
        self,
        stage_flac: Path,
        final_flac_dir: Optional[Path],
        flac_root: Path,
        metadata: AlbumMetadata,
        ui: Optional[UserInteractionPort] = None,
    ) -> bool:
        """
        Inspects already-published sibling Disc directories (read-only) and aligns the staged
        candidate BEFORE Opus transcoding so both FLAC and Opus outputs match Quod Libet invariants.
        """
        sibling_dirs = self._find_sibling_disc_dirs(final_flac_dir, flac_root)
        if not sibling_dirs:
            return False

        sibling_flacs = [
            f
            for d in sibling_dirs
            for f in sorted(d.glob("*.flac"))
            if f.is_file() and not f.is_symlink()
        ]
        if not sibling_flacs:
            return False

        staged_flacs = sorted(stage_flac.glob("*.flac"))
        if not staged_flacs:
            return False

        wf = _wf()
        fn_read = getattr(wf, "read_flac_tag", val_flac.read_flac_tag)
        fn_write = getattr(wf, "write_flac_tags", val_flac.write_flac_tags)
        fn_remove = getattr(wf, "remove_flac_tags", val_flac.remove_flac_tags)

        strip_all, extra_strip, unified_total = self._analyze_cross_disc_plan(
            staged_flacs,
            sibling_flacs,
            sibling_dirs,
            metadata,
            fn_read,
        )

        changed = False
        if strip_all and not metadata.clear_musicbrainz_release_id:
            metadata.clear_musicbrainz_release_id = True
            metadata.musicbrainz_id = None
            metadata.musicbrainz_url = None
            metadata.discogs_url = None
            changed = True
            if ui:
                ui.on_log(
                    LogLevel.INFO,
                    "Multi-disc release IDs differ or are partial across discs; detaching grouping tags for unified album grouping.",
                )

        if unified_total is not None and unified_total != metadata.total_discs:
            metadata.total_discs = unified_total
            changed = True

        keys_to_remove: List[str] = []
        if strip_all:
            keys_to_remove.extend(RELEASE_GROUPING_TAGS_TO_CLEAR)
        elif extra_strip:
            keys_to_remove.extend(sorted(extra_strip))

        for f in staged_flacs:
            tags: Dict[str, str] = {}
            if metadata.album and metadata.album != "Unknown Album" and fn_read(f, "ALBUM") != metadata.album:
                tags["ALBUM"] = metadata.album
            if metadata.artist and metadata.artist != "Unknown Artist" and fn_read(f, "ALBUMARTIST") != metadata.artist:
                tags["ALBUMARTIST"] = metadata.artist
            if unified_total is not None:
                tot_str = str(unified_total)
                if fn_read(f, "DISCTOTAL") != tot_str:
                    tags["DISCTOTAL"] = tot_str
                if fn_read(f, "TOTALDISCS") != tot_str:
                    tags["TOTALDISCS"] = tot_str
            if tags and fn_write(f, tags) is False:
                logger.warning("Failed updating cross-disc aligned tags on %s", f.name)

            file_removals = list(keys_to_remove)
            if metadata.artist != "Unknown Artist" and fn_read(f, "ALBUM ARTIST"):
                file_removals.append("ALBUM ARTIST")
            if file_removals and fn_remove(f, file_removals) is False:
                logger.warning("Failed stripping conflicting cross-disc tags on %s", f.name)

        return changed

    def reconcile_published_siblings(
        self,
        final_flac_dir: Optional[Path],
        final_opus_dir: Optional[Path],
        flac_root: Path,
        opus_root: Optional[Path],
        metadata: AlbumMetadata,
        ui: Optional[UserInteractionPort] = None,
    ) -> None:
        """
        Reconciles already-published sibling Disc directories on disk after publication so
        ALBUM, ALBUMARTIST, TOTALDISCS, and grouping tags match across every disc.
        """
        if final_flac_dir is None:
            return
        sibling_dirs = self._find_sibling_disc_dirs(final_flac_dir, flac_root)
        if not sibling_dirs:
            return

        current_flacs = sorted(
            f for f in final_flac_dir.glob("*.flac") if f.is_file() and not f.is_symlink()
        )
        sibling_flacs = [
            f
            for d in sibling_dirs
            for f in sorted(d.glob("*.flac"))
            if f.is_file() and not f.is_symlink()
        ]
        if not sibling_flacs:
            return

        wf = _wf()
        fn_read = getattr(wf, "read_flac_tag", val_flac.read_flac_tag)
        fn_write = getattr(wf, "write_flac_tags", val_flac.write_flac_tags)
        fn_remove = getattr(wf, "remove_flac_tags", val_flac.remove_flac_tags)
        fn_enc = getattr(wf, "transcode_flacs_to_opus", aud_opus.transcode_flacs_to_opus)
        fn_rg = getattr(wf, "apply_replaygain", aud_rg.apply_replaygain)

        strip_all, extra_strip, unified_total = self._analyze_cross_disc_plan(
            current_flacs,
            sibling_flacs,
            sibling_dirs,
            metadata,
            fn_read,
        )

        keys_to_remove: List[str] = []
        if strip_all:
            keys_to_remove.extend(RELEASE_GROUPING_TAGS_TO_CLEAR)
        elif extra_strip:
            keys_to_remove.extend(sorted(extra_strip))

        reconciled_count = 0
        for sib_dir in sibling_dirs:
            try:
                require_within_root(sib_dir, flac_root)
            except ValueError:
                continue

            sib_flacs = [
                f for f in sorted(sib_dir.glob("*.flac")) if f.is_file() and not f.is_symlink()
            ]
            if not sib_flacs:
                continue

            sib_modified = False
            for f in sib_flacs:
                try:
                    require_within_root(f, flac_root)
                except ValueError:
                    continue

                tags: Dict[str, str] = {}
                if metadata.album and metadata.album != "Unknown Album" and fn_read(f, "ALBUM") != metadata.album:
                    tags["ALBUM"] = metadata.album
                if metadata.artist and metadata.artist != "Unknown Artist" and fn_read(f, "ALBUMARTIST") != metadata.artist:
                    tags["ALBUMARTIST"] = metadata.artist
                if unified_total is not None:
                    tot_str = str(unified_total)
                    if fn_read(f, "DISCTOTAL") != tot_str:
                        tags["DISCTOTAL"] = tot_str
                    if fn_read(f, "TOTALDISCS") != tot_str:
                        tags["TOTALDISCS"] = tot_str

                removals = [k for k in keys_to_remove if fn_read(f, k) is not None]
                if metadata.artist != "Unknown Artist" and fn_read(f, "ALBUM ARTIST") is not None:
                    removals.append("ALBUM ARTIST")

                if tags:
                    if fn_write(f, tags) is False:
                        logger.warning("Failed reconciling tags on sibling track %s", f)
                    else:
                        sib_modified = True
                if removals:
                    if fn_remove(f, removals) is False:
                        logger.warning("Failed removing conflicting tags on sibling track %s", f)
                    else:
                        sib_modified = True

            if sib_modified:
                reconciled_count += 1
                if final_opus_dir is not None and opus_root is not None:
                    sib_opus_dir = final_opus_dir.parent / sib_dir.name
                    if sib_opus_dir.exists() and sib_opus_dir.is_dir() and not sib_opus_dir.is_symlink():
                        try:
                            require_within_root(sib_opus_dir, opus_root)
                            tmp_opus = Path(tempfile.mkdtemp(prefix=".whipit-retag.", dir=sib_opus_dir))
                            try:
                                enc_ok = fn_enc(sib_flacs, sib_dir, tmp_opus, find_cover_art(sib_dir))
                                if enc_ok:
                                    fn_rg(tmp_opus, "opus")
                                    for encoded in tmp_opus.glob("*.opus"):
                                        os.replace(encoded, sib_opus_dir / encoded.name)
                            finally:
                                shutil.rmtree(tmp_opus, ignore_errors=True)
                        except Exception as exc:
                            logger.warning("Could not refresh sibling Opus mirror %s: %s", sib_opus_dir, exc)

        if reconciled_count and ui:
            ui.on_log(
                LogLevel.INFO,
                f"Reconciled multi-disc tags across {reconciled_count} sibling disc folder(s) for unified album grouping.",
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
