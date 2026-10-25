"""High-level workflow state machine coordinator and pipeline orchestrator."""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional, Tuple
from whipit.audio.metadata import (
    _MAX_ART_BYTES,
    _MBID_RE,
    ART_NAMES,
    _image_ext,
    create_embedded_cover_art,
    embed_cover_art_flacs,
    fetch_discogs_url_from_mbid,
    find_cover_art,
    replicate_art_to_opus,
)
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.config import (
    disable_usb_autosuspend,
    format_time_span,
    get_cache_dir,
    get_drive_hardware_id,
    get_drive_max_speed,
    get_log_dir,
    get_output_root,
    get_stored_drive_offset,
    release_drive_locks,
    sanitise_string,
    truncate_to_bytes,
)
from whipit.inhibit import unmount_cdda_device
from whipit.models import (
    AlbumMetadata,
    DeviceInfo,
    InvariantViolationError,
    JobManifest,
    PathLayout,
    StorageIOError,
    WorkflowCancelledError,
    WorkflowResult,
    WorkflowState,
)
from whipit.notifications import notify_rip_failed, notify_rip_success
from whipit.ripping.engine import RippingEngine
from whipit.ripping.identify import _MB_REL_RE
from whipit.ripping.reports import (
    find_primary_cue,
    find_whipper_log,
    parse_cue_expected_tracks,
    parse_whipper_report,
)
from whipit.state_store import ManifestStore
from whipit.transaction import (
    ArchiveTransactionManager,
    TransactionError,
    validate_commit_preconditions,
    validate_publish_preconditions,
)
from whipit.ui.base import LogLevel, NullInteractionAdapter, UserInteractionPort
from whipit.validation.flac import (
    flac_audio_sha256,
    format_track_filename,
    get_flac_duration_seconds,
    read_flac_tag,
    verify_flac_integrity,
    write_flac_tags,
)
from whipit.validation.integrity import compare_trees, verify_release_candidate
from whipit.validation.tracks import validate_track_sequence


def _cue_escape(val: str) -> str:
    val = re.sub(r"[\x00-\x1f\x7f]", " ", val or "")
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _ascii_int(s: Optional[str], max_digits: int = 4) -> Optional[int]:
    clean = (s or "").split("/")[0].strip()
    return int(clean) if clean.isascii() and clean.isdecimal() and 0 < len(clean) <= max_digits else None

logger = logging.getLogger(__name__)


class RipWorkflowCoordinator:
    def __init__(
        self,
        device: str = "/dev/cdrom",
        speed: Optional[int] = None,
        manual_offset: Optional[int] = None,
        release_id: Optional[str] = None,
        output_root: Optional[Path] = None,
        ui: Optional[UserInteractionPort] = None,
        cancel_event: Optional[threading.Event] = None,
        initial_metadata: Optional[AlbumMetadata] = None,
    ):
        self.device_path = device
        self.speed = speed
        self.manual_offset = manual_offset
        self.release_id = release_id
        self.output_root = output_root or get_output_root()
        self.ui = ui or NullInteractionAdapter()
        self.cancel_event = cancel_event or threading.Event()
        self.engine: Optional[RippingEngine] = None

        self.job_id = str(uuid.uuid4())
        self.cache_dir = get_cache_dir()
        self.whipper_stage = self.cache_dir / f"stage_{self.job_id}"
        self.whipper_stage.mkdir(parents=True, exist_ok=True)
        self.candidate_stage = self.output_root / f".whipit-stage.{self.job_id}"

        self.store = ManifestStore()
        self.job_lock_fd: Optional[int] = self.store.acquire_job_lock(self.job_id)
        if self.job_lock_fd is None:
            shutil.rmtree(self.whipper_stage, ignore_errors=True)
            raise RuntimeError("Could not acquire job lock")
        self.manifest = self._initialize_manifest()
        if initial_metadata:
            self.manifest.metadata = initial_metadata
        self.store.save(self.manifest)

    def __del__(self) -> None:
        if getattr(self, "job_lock_fd", None) is not None:
            self.store.release_job_lock(self.job_id, self.job_lock_fd)
            self.job_lock_fd = None

    def _initialize_manifest(self) -> JobManifest:
        effective_offset = (
            self.manual_offset
            if self.manual_offset is not None
            else get_stored_drive_offset(self.device_path)
        )
        dev_info = DeviceInfo(
            path=self.device_path,
            hardware_id=get_drive_hardware_id(self.device_path),
            read_offset=effective_offset,
            speed=self.speed,
            max_speed=get_drive_max_speed(self.device_path),
        )
        layout = PathLayout(
            output_root=self.output_root,
            whipper_stage=self.whipper_stage,
            candidate_stage=self.candidate_stage,
        )
        return JobManifest(
            job_id=self.job_id,
            device=dev_info,
            paths=layout,
            full_log=get_log_dir() / f"{self.job_id}_full.log",
            error_log=get_log_dir() / f"{self.job_id}_error.log",
        )

    def _transition(self, new_state: WorkflowState) -> None:
        self.manifest.transition_to(new_state)
        self.store.save(self.manifest)
        self.ui.on_state_changed(new_state, self.manifest)

    def _record_error(self, message: str) -> None:
        self.ui.on_log(LogLevel.ERROR, message)
        if self.manifest.error_log:
            try:
                fd = os.open(self.manifest.error_log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
                with open(fd, "a", encoding="utf-8") as f:
                    f.write(f"{message}\n")
            except OSError as exc:
                self.ui.on_log(LogLevel.WARN, f"Could not write error log: {exc}")

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.engine:
            self.engine.cancel()
        release_drive_locks(self.device_path)

    def _has_staged_flacs(self) -> bool:
        stage_flac = self.candidate_stage / "flac"
        return stage_flac.exists() and any(stage_flac.glob("*.flac"))

    def _has_cached_flacs(self) -> bool:
        return self.whipper_stage.exists() and any(self.whipper_stage.rglob("*.flac"))

    def cleanup_aborted_session(self) -> None:
        """Preserves candidate stage or extraction cache if extracted FLACs exist; purges only unrecoverable sessions."""
        try:
            if self._has_staged_flacs():
                if self.whipper_stage.exists():
                    shutil.rmtree(self.whipper_stage, ignore_errors=True)
                self.ui.on_log(
                    LogLevel.WARN,
                    f"Preserving extracted FLAC candidate for recovery at: {self.candidate_stage}",
                )
                if getattr(self, "job_lock_fd", None) is not None:
                    self.store.release_job_lock(self.job_id, self.job_lock_fd)
                    self.job_lock_fd = None
                self.store.save(self.manifest)
                return

            if self._has_cached_flacs():
                if self.candidate_stage.exists():
                    shutil.rmtree(self.candidate_stage, ignore_errors=True)
                self.ui.on_log(
                    LogLevel.WARN,
                    f"Preserving extracted FLAC cache for recovery at: {self.whipper_stage}",
                )
                if getattr(self, "job_lock_fd", None) is not None:
                    self.store.release_job_lock(self.job_id, self.job_lock_fd)
                    self.job_lock_fd = None
                self.store.save(self.manifest)
                return

            if self.whipper_stage.exists():
                shutil.rmtree(self.whipper_stage, ignore_errors=True)
            if self.candidate_stage.exists():
                shutil.rmtree(self.candidate_stage, ignore_errors=True)
            if getattr(self, "job_lock_fd", None) is not None:
                self.store.release_job_lock(self.job_id, self.job_lock_fd)
                self.job_lock_fd = None
            self.store.delete(self.job_id, delete_logs=False)
        except Exception as exc:
            logger.warning("Error during session cleanup for job %s: %s", self.job_id, exc)

    def resolve_paths(self, metadata: AlbumMetadata, partial: bool = False, timestamp_suffix: str = "") -> None:
        raw_stem = f"{metadata.artist} - {metadata.album}"
        stem = truncate_to_bytes(raw_stem, max_bytes=180)

        if partial:
            suffix = f" (Partial {timestamp_suffix})" if timestamp_suffix else " (Partial)"
        elif timestamp_suffix:
            suffix = f" ({timestamp_suffix})"
        else:
            suffix = ""

        master_name = truncate_to_bytes(f"{stem}{suffix}", max_bytes=240)
        master_dir = self.output_root / master_name

        flac_name = truncate_to_bytes(f"{stem} (FLAC)", max_bytes=220)
        opus_name = truncate_to_bytes(f"{stem} (Opus)", max_bytes=220)

        flac_dir = master_dir / flac_name
        opus_dir = master_dir / opus_name

        is_multi = metadata.disc_number is not None and (
            metadata.disc_number > 1 or (metadata.total_discs is not None and metadata.total_discs > 1)
        )
        if is_multi and metadata.disc_number:
            flac_dir = flac_dir / f"Disc {metadata.disc_number}"
            opus_dir = opus_dir / f"Disc {metadata.disc_number}"

        self.manifest.paths.final_flac_dir = flac_dir
        self.manifest.paths.final_opus_dir = opus_dir
        self.store.save(self.manifest)

    def _apply_custom_metadata_to_stage(self, stage_flac: Path, meta: AlbumMetadata) -> None:
        flacs = sorted(list(stage_flac.glob("*.flac")))
        renamed_mapping: List[Tuple[int, str, Path]] = []
        old_to_new_names: dict[str, str] = {}

        for f in flacs:
            tn_tag = read_flac_tag(f, "TRACKNUMBER") or "1"
            t_num = _ascii_int(tn_tag) or 1
            custom_title = meta.custom_track_titles.get(t_num)
            if not custom_title:
                custom_title = read_flac_tag(f, "TITLE") or f"Track {t_num}"

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

            write_flac_tags(f, tags)

            track_artist = meta.artist if meta.artist != "Unknown Artist" else (read_flac_tag(f, "ARTIST") or "Unknown Artist")
            new_fname = format_track_filename(t_num, track_artist, custom_title)
            new_path = stage_flac / new_fname
            old_name = f.name
            if new_path != f:
                if new_path.exists():
                    new_path.unlink(missing_ok=True)
                f.rename(new_path)
                old_to_new_names[old_name] = new_fname
                renamed_mapping.append((t_num, custom_title, new_path))
            else:
                renamed_mapping.append((t_num, custom_title, f))

        self._update_cue_sheet_metadata(stage_flac, meta, old_to_new_names, renamed_mapping)

    def _update_cue_sheet_metadata(
        self,
        stage_flac: Path,
        meta: AlbumMetadata,
        old_to_new_names: dict[str, str],
        tracks: List[Tuple[int, str, Path]],
    ) -> None:
        cue_path = find_primary_cue(stage_flac)
        if not cue_path:
            return
        try:
            content = cue_path.read_text(encoding="utf-8", errors="replace")
            for old_n, new_n in old_to_new_names.items():
                content = content.replace(f'FILE "{old_n}"', f'FILE "{new_n}"')
            if meta.artist and meta.artist != "Unknown Artist":
                content = re.sub(r'^PERFORMER\s+".*?"', f'PERFORMER "{_cue_escape(meta.artist)}"', content, flags=re.M)
            if meta.album and meta.album != "Unknown Album":
                content = re.sub(r'^TITLE\s+".*?"', f'TITLE "{_cue_escape(meta.album)}"', content, flags=re.M)
            cue_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not update CUE sheet with custom metadata: %s", exc)

    def check_disk_space_preflight(
        self,
        min_cache_bytes: int = 1_000_000_000,
        min_output_bytes: int = 1_500_000_000,
    ) -> Tuple[bool, Optional[str]]:
        """
        Preflight check ensuring sufficient free disk space exists on
        cache_dir and output_root before optical extraction begins.
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self.output_root.mkdir(parents=True, exist_ok=True)

            cache_usage = shutil.disk_usage(self.cache_dir)
            output_usage = shutil.disk_usage(self.output_root)

            same_fs = False
            try:
                same_fs = self.cache_dir.stat().st_dev == self.output_root.stat().st_dev
            except Exception:
                pass

            if same_fs:
                required = min_cache_bytes + min_output_bytes
                if cache_usage.free < required:
                    free_gb = cache_usage.free / (1024 ** 3)
                    req_gb = required / (1024 ** 3)
                    return False, (
                        f"Insufficient disk space on {self.output_root}. "
                        f"Available: {free_gb:.2f} GB, Required peak: {req_gb:.2f} GB."
                    )
            else:
                if cache_usage.free < min_cache_bytes:
                    free_gb = cache_usage.free / (1024 ** 3)
                    req_gb = min_cache_bytes / (1024 ** 3)
                    return False, (
                        f"Insufficient cache disk space on {self.cache_dir}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
                if output_usage.free < min_output_bytes:
                    free_gb = output_usage.free / (1024 ** 3)
                    req_gb = min_output_bytes / (1024 ** 3)
                    return False, (
                        f"Insufficient output disk space on {self.output_root}. "
                        f"Available: {free_gb:.2f} GB, Required: {req_gb:.2f} GB."
                    )
            return True, None
        except OSError as exc:
            self.ui.on_log(
                LogLevel.ERROR,
                f"Disk-space preflight failed for {self.output_root}: {exc}",
            )
            return False, f"Cannot verify available disk space on {self.output_root}: {exc}"
        except Exception as exc:
            self.ui.on_log(
                LogLevel.ERROR,
                f"Unexpected disk-space preflight error: {exc!r}",
            )
            return False, "Disk-space preflight failed; refusing to start extraction."

    def run_workflow(self, keep_going: bool = False, unknown_disc: bool = False, overwrite: bool = False) -> WorkflowResult:
        try:
            return self._run_workflow_impl(
                keep_going=keep_going,
                unknown_disc=unknown_disc,
                overwrite=overwrite,
            )
        except (KeyboardInterrupt, WorkflowCancelledError):
            self.ui.on_log(LogLevel.WARN, "Workflow cancelled by user.")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED
        except InvariantViolationError as exc:
            msg = f"Invariant violation: {exc}"
            logger.error("Invariant violation for job %s: %s", self.job_id, exc)
            self._record_error(msg)
            if self.manifest.state not in (
                WorkflowState.COMMITTED,
                WorkflowState.PUBLISH_FAILED,
                WorkflowState.ROLLBACK_FAILED,
            ):
                self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED
        except (StorageIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                msg = f"Disk full (ENOSPC): {exc}"
            else:
                msg = f"Storage I/O failure: {exc}"
            logger.error("Storage I/O failure for job %s: %s", self.job_id, exc)
            self._record_error(msg)
            if self.manifest.state not in (
                WorkflowState.COMMITTED,
                WorkflowState.PUBLISH_FAILED,
                WorkflowState.ROLLBACK_FAILED,
            ):
                self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED
        except Exception as exc:
            msg = f"Unexpected workflow failure: {exc}"
            logger.exception("Unexpected workflow failure for job %s: %s", self.job_id, exc)
            self._record_error(msg)
            if self.manifest.state not in (
                WorkflowState.COMMITTED,
                WorkflowState.PUBLISH_FAILED,
                WorkflowState.ROLLBACK_FAILED,
            ):
                self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED
        finally:
            try:
                if getattr(self, "job_lock_fd", None) is not None:
                    try:
                        self.store.release_job_lock(self.job_id, self.job_lock_fd)
                    except Exception as exc:
                        logger.warning("Error releasing job lock for %s: %s", self.job_id, exc)
                    self.job_lock_fd = None
            finally:
                try:
                    release_drive_locks(self.device_path)
                except Exception as exc:
                    logger.warning("Error releasing drive locks for %s: %s", self.device_path, exc)

    def _run_workflow_impl(self, keep_going: bool = False, unknown_disc: bool = False, overwrite: bool = False) -> WorkflowResult:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.whipper_stage.mkdir(parents=True, exist_ok=True)
        unmount_cdda_device(self.device_path)
        disable_usb_autosuspend(self.device_path)

        space_ok, space_err = self.check_disk_space_preflight()
        if not space_ok:
            msg = space_err or "Insufficient disk space."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED

        if not self.release_id and not unknown_disc and self.ui.is_interactive():
            from whipit.ripping.identify import inspect_disc
            self.ui.on_log(LogLevel.INFO, "Checking MusicBrainz for matching pressings...")
            insp = inspect_disc(self.device_path)
            if insp.matching_releases and len(insp.matching_releases) > 1:
                chosen_mbid = self.ui.prompt_select_release(insp.matching_releases)
                if not chosen_mbid or self.cancel_event.is_set():
                    self.ui.on_log(LogLevel.WARN, "Extraction cancelled at pressing selection.")
                    self._transition(WorkflowState.ABORTED)
                    self.cleanup_aborted_session()
                    return WorkflowResult.ABORTED
                self.release_id = chosen_mbid
            elif insp.matching_releases and len(insp.matching_releases) == 1:
                self.release_id = insp.matching_releases[0].mbid

            if self.release_id and insp.artist != "Unknown Artist":
                self.manifest.metadata = AlbumMetadata(
                    artist=sanitise_string(insp.artist),
                    album=sanitise_string(insp.title),
                    album_stem=f"{sanitise_string(insp.artist)} - {sanitise_string(insp.title)}",
                    musicbrainz_id=self.release_id,
                    musicbrainz_url=f"https://musicbrainz.org/release/{self.release_id}",
                    musicbrainz_lookup_url=insp.mb_lookup_url,
                    discogs_url=insp.discogs_url,
                )
                self.store.save(self.manifest)

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        self._transition(WorkflowState.RIPPING)

        engine = RippingEngine(
            cache_dir=self.cache_dir,
            stage_dir=self.whipper_stage,
            device_path=self.device_path,
            speed=self.speed,
            offset=self.manifest.device.read_offset,
            release_id=self.release_id,
            ui=self.ui,
            cancel_event=self.cancel_event,
            full_log_path=self.manifest.full_log,
        )
        self.engine = engine

        try:
            rip_result = engine.run_extraction(keep_going=keep_going, unknown_disc=unknown_disc)
        except KeyboardInterrupt:
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            raise

        exec_log = self.whipper_stage / "whipper_exec.log"
        if exec_log.exists() and self.manifest.full_log:
            try:
                shutil.copy2(exec_log, self.manifest.full_log)
                try:
                    os.chmod(self.manifest.full_log, 0o600)
                except OSError:
                    pass
            except Exception:
                pass

        if not rip_result.success or rip_result.cancelled or self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            if not rip_result.cancelled and not self.cancel_event.is_set():
                notify_rip_failed(
                    artist=self.manifest.metadata.artist,
                    album=self.manifest.metadata.album,
                    reason="Extraction halted or aborted by user.",
                )
            return WorkflowResult.ABORTED

        self.device_path = rip_result.device_path
        self.manifest.device.path = rip_result.device_path
        self.manifest.device.hardware_id = get_drive_hardware_id(rip_result.device_path)
        self.manifest.device.read_offset = rip_result.effective_offset
        self.manifest.device.speed = rip_result.speed

        self._transition(WorkflowState.RIP_EXTRACTED)

        content_dir = self.whipper_stage
        while True:
            subdirs = [d for d in content_dir.iterdir() if d.is_dir()]
            subfiles = [f for f in content_dir.iterdir() if f.is_file() and f.name != "whipper_exec.log"]
            if len(subdirs) == 1 and len(subfiles) == 0:
                content_dir = subdirs[0]
            else:
                break

        flac_files = sorted(list(content_dir.glob("*.flac")))
        if not flac_files:
            msg = "No audio tracks were extracted."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED

        sample_flac = flac_files[0] if flac_files else None
        tag_artist = "Unknown Artist"
        tag_album = "Unknown Album"
        if sample_flac:
            tag_artist = read_flac_tag(sample_flac, "ALBUMARTIST") or read_flac_tag(sample_flac, "ARTIST") or "Unknown Artist"
            tag_album = read_flac_tag(sample_flac, "ALBUM") or "Unknown Album"
        stub_stem = truncate_to_bytes(f"{sanitise_string(tag_artist)} - {sanitise_string(tag_album)}", max_bytes=180)

        cue_path = find_primary_cue(self.whipper_stage)
        log_path = find_whipper_log(self.whipper_stage)

        # Handle partial rip where Whipper aborted before writing CUE/log
        if not cue_path or not log_path:
            self.ui.on_log(LogLevel.WARN, "Missing CUE/log from partial rip; synthesizing metadata...")

        if not cue_path:
            cue_path = content_dir / f"{stub_stem} (WhipIt partial stub).cue"
            cue_artist = _cue_escape(tag_artist)
            cue_album = _cue_escape(tag_album)
            cue_lines = [f'PERFORMER "{cue_artist}"', f'TITLE "{cue_album} (Partial)"']
            for f in flac_files:
                tn = read_flac_tag(f, "TRACKNUMBER") or "1"
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

        expected_tracks = parse_cue_expected_tracks(cue_path)
        if expected_tracks == 0:
            if exec_log.exists():
                m_tot = re.findall(r"(?:Reading|Copying|Ripping)\s+track\s+\d+\s+of\s+(\d+)", exec_log.read_text(errors="ignore"), re.I)
                if m_tot:
                    expected_tracks = max(int(t) for t in m_tot)
            if expected_tracks == 0:
                expected_tracks = len(flac_files)

        health, ar_summary, _, _ = parse_whipper_report(log_path)

        self.manifest.tracks.expected_count = expected_tracks
        self.manifest.tracks.extracted_count = len(flac_files)

        rip_elapsed = getattr(rip_result, "elapsed_seconds", None)
        total_audio_sec = sum(get_flac_duration_seconds(f) or 0.0 for f in flac_files)
        self.manifest.tracks.total_audio_seconds = total_audio_sec if total_audio_sec > 0 else None
        self.manifest.tracks.rip_elapsed_seconds = rip_elapsed

        avg_speed_x: Optional[float] = None
        if rip_elapsed and rip_elapsed > 0 and total_audio_sec > 0:
            avg_speed_x = total_audio_sec / rip_elapsed
            self.manifest.tracks.average_speed_x = round(avg_speed_x, 2)
            try:
                rip_result.average_speed_x = avg_speed_x
            except (AttributeError, TypeError):
                pass
            self.ui.on_log(
                LogLevel.INFO,
                f"Extraction throughput: {len(flac_files)} tracks [{format_time_span(total_audio_sec)} audio] in {format_time_span(rip_elapsed)} (Average speed: {avg_speed_x:.1f}x).",
            )

        seq_ok, errors = validate_track_sequence(content_dir, expected_tracks)
        self.manifest.tracks.sequence_valid = seq_ok
        if errors and self.manifest.error_log:
            for err in errors:
                self._record_error(err)

        extracted_numbers = set()
        for f in flac_files:
            tag = read_flac_tag(f, "TRACKNUMBER")
            parsed_num = _ascii_int(tag)
            if parsed_num is not None:
                extracted_numbers.add(parsed_num)
        missing = [i for i in range(1, expected_tracks + 1) if i not in extracted_numbers]
        self.manifest.tracks.missing_tracks = missing

        self.manifest.verification.health_status = health
        self.manifest.verification.accuraterip_summary = ar_summary
        self.manifest.verification.is_accurate = (ar_summary == "All tracks accurately ripped")
        self.manifest.verification.is_unverified = not self.manifest.verification.is_accurate and (health == "No errors occurred")

        is_partial = rip_result.partial or not seq_ok or (expected_tracks > len(flac_files)) or bool(missing)
        has_health_errors = (health != "No errors occurred" and health != "Unknown")

        if is_partial and missing:
            self.ui.on_log(LogLevel.WARN, f"Partial extraction: {len(flac_files)} of {expected_tracks} tracks readable. Missing tracks: {missing}")
        elif has_health_errors:
            self.ui.on_log(LogLevel.WARN, f"Extraction health note: {health}")

        sample_flac = flac_files[0] if flac_files else None
        if sample_flac:
            artist = read_flac_tag(sample_flac, "ALBUMARTIST") or read_flac_tag(sample_flac, "ARTIST") or "Unknown Artist"
            album = read_flac_tag(sample_flac, "ALBUM") or "Unknown Album"
            disc_str = read_flac_tag(sample_flac, "DISCNUMBER") or read_flac_tag(sample_flac, "DISC")
            total_disc_str = read_flac_tag(sample_flac, "DISCTOTAL") or read_flac_tag(sample_flac, "TOTALDISCS")

            disc_no = _ascii_int(disc_str)
            tot_discs = _ascii_int(total_disc_str)

            mbid = self.release_id or read_flac_tag(sample_flac, "MUSICBRAINZ_ALBUMID") or read_flac_tag(sample_flac, "MUSICBRAINZ_RELEASEID")
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
                self.ui.on_log(LogLevel.WARN, f"Metadata enrichment skipped: {exc}")
                discogs_url = None

            if mb_url:
                self.ui.on_log(LogLevel.INFO, f"MusicBrainz Release: {mb_url}")
            if discogs_url:
                self.ui.on_log(LogLevel.INFO, f"Discogs Entry: {discogs_url}")
            if lookup_url:
                self.ui.on_log(LogLevel.INFO, f"MusicBrainz Submit/Attach URL: {lookup_url}")

            curr_meta = self.manifest.metadata
            final_artist = curr_meta.artist if curr_meta.artist != "Unknown Artist" else sanitise_string(artist)
            final_album = curr_meta.album if curr_meta.album != "Unknown Album" else sanitise_string(album)
            final_disc_no = curr_meta.disc_number if curr_meta.disc_number is not None else disc_no
            final_tot_discs = curr_meta.total_discs if curr_meta.total_discs is not None else tot_discs

            self.manifest.metadata = AlbumMetadata(
                artist=final_artist,
                album=final_album,
                disc_number=final_disc_no,
                total_discs=final_tot_discs,
                album_stem=f"{final_artist} - {final_album}",
                musicbrainz_id=mbid,
                musicbrainz_url=mb_url,
                musicbrainz_lookup_url=lookup_url,
                discogs_url=discogs_url,
                year=curr_meta.year,
                genre=curr_meta.genre,
                custom_track_titles=curr_meta.custom_track_titles,
            )
            self.store.save(self.manifest)

        self.resolve_paths(self.manifest.metadata, partial=is_partial)

        if not is_partial:
            self._transition(WorkflowState.RIP_VALIDATED)

        self.candidate_stage.mkdir(parents=True, exist_ok=True)
        stage_flac = self.candidate_stage / "flac"
        stage_opus = self.candidate_stage / "opus"
        shutil.copytree(content_dir, stage_flac)
        (stage_flac / "whipper_exec.log").unlink(missing_ok=True)
        stage_opus.mkdir(parents=True, exist_ok=True)

        self._transition(WorkflowState.CANDIDATE_STAGED)

        c_ok, c_errs = verify_release_candidate(
            stage_flac,
            expected_count=len(flac_files) if is_partial else expected_tracks,
            total_tracks=expected_tracks,
            allow_partial=is_partial,
        )
        if not c_ok or not compare_trees(content_dir, stage_flac, ["whipper_exec.log"]):
            msg = "Candidate staging verification failed."
            self._record_error(msg)
            if c_errs:
                for ce in c_errs:
                    self._record_error(f"  • {ce}")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED

        meta = self.manifest.metadata
        has_custom_meta = bool(
            (meta.artist != "Unknown Artist" and meta.artist != sanitise_string(tag_artist))
            or (meta.album != "Unknown Album" and meta.album != sanitise_string(tag_album))
            or meta.year
            or meta.genre
            or meta.custom_track_titles
        )

        if has_custom_meta:
            self.ui.on_log(LogLevel.INFO, "Applying custom metadata tags to candidate FLAC tracks...")
            self._apply_custom_metadata_to_stage(stage_flac, meta)

        (self.candidate_stage / ".staged-ok").write_text(
            "".join(f"{p.name}\t{flac_audio_sha256(p) or '-'}\n" for p in sorted(stage_flac.glob("*.flac"))),
            encoding="utf-8",
        )

        for f in content_dir.glob("*.flac"):
            f.unlink(missing_ok=True)
        for f in content_dir.glob("*.wav"):
            f.unlink(missing_ok=True)

        c_flacs = sorted(list(stage_flac.glob("*.flac")))

        active_art = self.ui.get_cover_art()
        cover_art: Optional[Path] = None

        if active_art and active_art.is_file():
            data = b""
            try:
                with open(active_art, "rb") as fh:
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
                self.ui.on_log(LogLevel.INFO, f"Applied album cover art: {cover_art.name}")
            else:
                self.ui.on_log(LogLevel.WARN, "Selected cover art is invalid; ignoring it.")
                cover_art = find_cover_art(stage_flac)
        else:
            cover_art = find_cover_art(stage_flac)

        embed_art_temp: Optional[Path] = None
        embed_art: Optional[Path] = None
        if cover_art:
            replicate_art_to_opus(stage_flac, stage_opus)
            embed_art_temp = self.cache_dir / f"embed_cover_{uuid.uuid4().hex[:8]}.jpg"
            embed_art = create_embedded_cover_art(cover_art, embed_art_temp, max_dimension=1000)
            if not embed_cover_art_flacs(c_flacs, embed_art):
                self.ui.on_log(LogLevel.WARN, "Could not embed cover art into all FLAC tracks.")

        if self.cancel_event.is_set():
            if embed_art_temp and embed_art_temp.exists():
                embed_art_temp.unlink(missing_ok=True)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        self.ui.on_log(LogLevel.INFO, f"Transcoding {len(c_flacs)} track(s) to Opus 192k...")
        enc_ok = transcode_flacs_to_opus(
            c_flacs,
            stage_flac,
            stage_opus,
            embed_art or cover_art,
            progress_callback=self.ui.on_transcode_progress,
            cancel_event=self.cancel_event,
        )
        if embed_art_temp and embed_art_temp.exists():
            embed_art_temp.unlink(missing_ok=True)
        if not enc_ok:
            if self.cancel_event.is_set():
                self.ui.on_log(LogLevel.WARN, "Opus transcoding cancelled by user.")
                self._transition(WorkflowState.ABORTED)
                self.cleanup_aborted_session()
                return WorkflowResult.ABORTED

            msg = "Opus transcoding failed."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return WorkflowResult.ABORTED

        self._transition(WorkflowState.OPUS_ENCODED)

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        self.ui.on_log(LogLevel.HEADER, "Applying ReplayGain (EBU R128)...")
        rg_flac = apply_replaygain(stage_flac, "flac")
        rg_opus = apply_replaygain(stage_opus, "opus")
        if not (rg_flac and rg_opus):
            if self.cancel_event.is_set():
                self.ui.on_log(LogLevel.WARN, "ReplayGain cancelled by user.")
                self._transition(WorkflowState.ABORTED)
                self.cleanup_aborted_session()
                return WorkflowResult.ABORTED
            self.ui.on_log(LogLevel.WARN, "ReplayGain tagging could not be applied; continuing publication.")

        self._transition(WorkflowState.REPLAYGAIN_APPLIED)

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        self.ui.on_log(LogLevel.INFO, "Verifying candidate FLAC audio stream integrity...")
        if not verify_flac_integrity(c_flacs):
            msg = "Post-tagging FLAC stream integrity check (flac -t -s) failed."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return WorkflowResult.ABORTED

        self.manifest.verification.flac_integrity_passed = True
        self.store.save(self.manifest)
        self._transition(WorkflowState.CANDIDATE_VERIFIED)

        tx_mgr = ArchiveTransactionManager(self.manifest, self.store)
        proceed, eff_overwrite, ts_suffix = tx_mgr.resolve_destination_conflict(
            overwrite=overwrite,
            ui=self.ui,
        )
        if not proceed or self.cancel_event.is_set():
            self.ui.on_log(LogLevel.WARN, "Publication cancelled.")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        if ts_suffix:
            self.resolve_paths(self.manifest.metadata, partial=is_partial, timestamp_suffix=ts_suffix)

        validate_publish_preconditions(self.manifest)
        self._transition(WorkflowState.PUBLISHING)

        try:
            tx_mgr.execute_publication(stage_flac, stage_opus, overwrite=eff_overwrite)
        except TransactionError as e:
            msg = f"Publication failed: {e}"
            self._record_error(msg)
            new_state = (
                WorkflowState.PUBLISH_FAILED
                if e.rollback_succeeded
                else WorkflowState.ROLLBACK_FAILED
            )
            self._transition(new_state)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return WorkflowResult.ABORTED
        except Exception as e:
            msg = f"Publication failed: {e}"
            self._record_error(msg)
            rb_ok = tx_mgr.rollback()
            self._transition(WorkflowState.PUBLISH_FAILED if rb_ok else WorkflowState.ROLLBACK_FAILED)
            self.cleanup_aborted_session()
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return WorkflowResult.ABORTED

        if self.manifest.transaction.committed:
            validate_commit_preconditions(self.manifest)
        self._transition(WorkflowState.COMMITTED)

        shutil.rmtree(self.whipper_stage, ignore_errors=True)
        shutil.rmtree(self.candidate_stage, ignore_errors=True)
        self.store.release_job_lock(self.job_id, self.job_lock_fd)
        self.job_lock_fd = None

        if self.manifest.error_log and self.manifest.error_log.exists() and self.manifest.error_log.stat().st_size == 0:
            self.manifest.error_log.unlink(missing_ok=True)

        self.ui.on_log(LogLevel.HEADER, "════════════════════════════════════════════════════════════")
        dest = self.manifest.paths.final_flac_dir
        final_dir = dest.parent if dest else self.output_root
        track_total = len(flac_files)

        published_art: Optional[Path] = None
        if dest and dest.exists():
            published_art = find_cover_art(dest)
            if not published_art and dest.parent != self.output_root:
                published_art = find_cover_art(dest.parent)
        if not published_art:
            published_art = cover_art

        speed_summary = f" [avg speed {avg_speed_x:.1f}x]" if avg_speed_x else ""
        if not is_partial and self.manifest.verification.is_accurate:
            self.ui.on_log(LogLevel.OK, f"Complete! Saved to: {final_dir}{speed_summary}")
            notify_rip_success(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=True,
                cover_art=published_art,
                is_partial=False,
            )
            res = WorkflowResult.SUCCESS
        elif not is_partial:
            self.ui.on_log(LogLevel.WARN, f"Complete (not AccurateRip verified). Saved to: {final_dir}{speed_summary}")
            notify_rip_success(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=False,
                cover_art=published_art,
                is_partial=False,
            )
            res = WorkflowResult.SUCCESS
        else:
            self.ui.on_log(LogLevel.WARN, f"Partial archive committed. Saved to: {final_dir}{speed_summary}")
            notify_rip_success(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=False,
                cover_art=published_art,
                is_partial=True,
            )
            res = WorkflowResult.COMMITTED_PARTIAL
        self.ui.on_log(LogLevel.HEADER, "════════════════════════════════════════════════════════════")
        return res
