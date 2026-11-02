"""High-level workflow state machine coordinator and pipeline orchestrator."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional, Tuple

from whipit.audio.metadata import (
    _MAX_ART_BYTES,
    _MBID_RE,
    ART_NAMES,
    _image_ext,
    clean_album_title,
    create_embedded_cover_art,
    embed_cover_art_flacs,
    fetch_discogs_url_from_mbid,
    find_cover_art,
    replicate_art_to_opus,
)
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.config import (
    default_flac_root,
    default_opus_root,
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
    PublicationExecutionResult,
    RipExecutionResult,
    StorageIOError,
    TranscodeResult,
    WorkflowCancelledError,
    WorkflowResult,
    WorkflowState,
)
from whipit.naming import format_album_stem
from whipit.notifications import notify_rip_failed, notify_rip_success
from whipit.ripping.engine import RippingEngine
from whipit.ripping.reports import (
    find_primary_cue,
    find_whipper_log,
    parse_cue_expected_tracks,
    parse_whipper_report,
)
from whipit.services.extraction_service import (
    ExtractionService,
    _ascii_int,
    _cue_escape,
)
from whipit.services.publication_service import PublicationService
from whipit.services.stage_metadata_service import StageMetadataService
from whipit.services.transcode_service import TranscodeService
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

logger = logging.getLogger(__name__)


def _wf() -> Any:
    return sys.modules.get("whipit.workflow", sys.modules[__name__])


class RipWorkflowCoordinator:
    """Finite state machine coordinating extraction, candidate staging, transcoding, and publication."""

    def __init__(
        self,
        device: str = "/dev/cdrom",
        speed: Optional[int] = None,
        manual_offset: Optional[int] = None,
        release_id: Optional[str] = None,
        output_root: Optional[Path] = None,
        flac_root: Optional[Path] = None,
        opus_root: Optional[Path] = None,
        generate_opus: bool = True,
        auto_eject: bool = True,
        ui: Optional[UserInteractionPort] = None,
        cancel_event: Optional[threading.Event] = None,
        initial_metadata: Optional[AlbumMetadata] = None,
    ):
        self.device_path = device
        self.speed = speed
        self.manual_offset = manual_offset
        self.release_id = release_id

        if output_root is not None:
            self.flac_root = (flac_root or output_root).resolve()
            self.opus_root = (opus_root or output_root).resolve() if generate_opus else None
        else:
            self.flac_root = (flac_root or default_flac_root()).resolve()
            self.opus_root = (opus_root or default_opus_root()).resolve() if generate_opus else None
        self.output_root = self.flac_root
        self.generate_opus = generate_opus
        self.auto_eject = auto_eject
        self.ui = ui or NullInteractionAdapter()
        self.cancel_event = cancel_event or threading.Event()

        # Domain Services
        self.extraction_service = ExtractionService()
        self.transcode_service = TranscodeService()
        self.stage_metadata_service = StageMetadataService()
        self.publication_service = PublicationService()

        self.job_id = str(uuid.uuid4())
        self.cache_dir = get_cache_dir()
        self.whipper_stage = self.cache_dir / f"stage_{self.job_id}"
        self.whipper_stage.mkdir(parents=True, exist_ok=True)
        self.candidate_stage = self.flac_root / f".whipit-stage.{self.job_id}"
        self.candidate_stage_opus: Optional[Path] = None
        if self.generate_opus and self.opus_root and self.opus_root != self.flac_root:
            self.candidate_stage_opus = self.opus_root / f".whipit-stage.{self.job_id}"

        self.store = ManifestStore()
        self.job_lock_fd: Optional[int] = self.store.acquire_job_lock(self.job_id)
        if self.job_lock_fd is None:
            shutil.rmtree(self.whipper_stage, ignore_errors=True)
            raise RuntimeError("Could not acquire job lock")
        self.manifest = self._initialize_manifest()
        if initial_metadata:
            self.manifest.metadata = initial_metadata
        self.store.save(self.manifest)

    @property
    def engine(self) -> Optional[RippingEngine]:
        return self.extraction_service.engine

    @engine.setter
    def engine(self, val: Optional[RippingEngine]) -> None:
        self.extraction_service.engine = val

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
            flac_root=self.flac_root,
            opus_root=self.opus_root,
            whipper_stage=self.whipper_stage,
            candidate_stage=self.candidate_stage,
            candidate_stage_opus=self.candidate_stage_opus,
            generate_opus=self.generate_opus,
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
        return self.publication_service.has_staged_flacs(self.candidate_stage)

    def _has_cached_flacs(self) -> bool:
        return self.publication_service.has_cached_flacs(self.whipper_stage)

    def cleanup_aborted_session(self) -> None:
        """Preserves candidate stage or extraction cache if extracted FLACs exist; purges only unrecoverable sessions."""
        try:
            self.publication_service.cleanup_aborted_session(
                manifest=self.manifest,
                store=self.store,
                whipper_stage=self.whipper_stage,
                candidate_stage=self.candidate_stage,
                candidate_stage_opus=self.candidate_stage_opus,
                ui=self.ui,
                job_id=self.job_id,
            )
            if getattr(self, "job_lock_fd", None) is not None:
                self.store.release_job_lock(self.job_id, self.job_lock_fd)
                self.job_lock_fd = None
        except Exception as exc:
            logger.warning("Error during session cleanup for job %s: %s", self.job_id, exc)

    def resolve_paths(self, metadata: AlbumMetadata, partial: bool = False, timestamp_suffix: str = "") -> None:
        self.publication_service.resolve_paths(
            self.manifest,
            metadata,
            self.flac_root,
            self.opus_root,
            self.generate_opus,
            partial=partial,
            timestamp_suffix=timestamp_suffix,
            store=self.store,
        )

    def _normalize_candidate_tracks(self, stage_flac: Path, meta: AlbumMetadata, apply_tags: bool = False) -> None:
        self.stage_metadata_service.normalize_candidate_tracks(stage_flac, meta, apply_tags=apply_tags)

    def _apply_custom_metadata_to_stage(self, stage_flac: Path, meta: AlbumMetadata) -> None:
        self.stage_metadata_service.normalize_candidate_tracks(stage_flac, meta, apply_tags=True)

    def _update_cue_sheet_metadata(
        self,
        stage_flac: Path,
        meta: AlbumMetadata,
        old_to_new_names: dict[str, str],
        tracks: List[Tuple[int, str, Path]],
    ) -> None:
        self.stage_metadata_service.update_cue_sheet_metadata(stage_flac, meta, old_to_new_names, tracks)

    def check_disk_space_preflight(
        self,
        min_cache_bytes: int = 1_000_000_000,
        min_output_bytes: int = 1_500_000_000,
    ) -> Tuple[bool, Optional[str]]:
        return self.publication_service.check_disk_space(
            self.cache_dir,
            self.flac_root,
            self.opus_root,
            self.generate_opus,
            self.ui,
            min_cache_bytes=min_cache_bytes,
            min_output_bytes=min_output_bytes,
        )

    def _ensure_disc_identified(self, unknown_disc: bool) -> bool:
        """Prompts for release identification when multiple matching pressings are detected."""
        if not self.release_id and not unknown_disc and self.ui.is_interactive():
            from whipit.ripping.identify import inspect_disc

            self.ui.on_log(LogLevel.INFO, "Checking MusicBrainz for matching pressings...")
            insp = inspect_disc(self.device_path)
            if insp.matching_releases and len(insp.matching_releases) > 1:
                chosen_mbid = self.ui.prompt_select_release(insp.matching_releases)
                if not chosen_mbid or self.cancel_event.is_set():
                    self.ui.on_log(LogLevel.WARN, "Extraction cancelled at pressing selection.")
                    return False
                self.release_id = chosen_mbid
            elif insp.matching_releases and len(insp.matching_releases) == 1:
                self.release_id = insp.matching_releases[0].mbid

            chosen_rel = None
            if insp.matching_releases:
                chosen_rel = next((r for r in insp.matching_releases if r.mbid == self.release_id), insp.matching_releases[0])

            rel_artist = chosen_rel.artist if chosen_rel and chosen_rel.artist != "Unknown Artist" else insp.artist
            rel_title = chosen_rel.title if chosen_rel and chosen_rel.title != "Unknown Album" else insp.title

            if self.release_id and rel_artist != "Unknown Artist":
                clean_title = clean_album_title(rel_title)
                self.manifest.metadata = AlbumMetadata(
                    artist=sanitise_string(rel_artist),
                    album=sanitise_string(clean_title),
                    album_stem=format_album_stem(rel_artist, clean_title),
                    musicbrainz_id=self.release_id,
                    musicbrainz_url=f"https://musicbrainz.org/release/{self.release_id}",
                    musicbrainz_lookup_url=insp.mb_lookup_url,
                    discogs_url=insp.discogs_url,
                )
                self.store.save(self.manifest)
        return True

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
            _wf().notify_rip_failed(
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
            _wf().notify_rip_failed(
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
            _wf().notify_rip_failed(
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
                    wf = _wf()
                    fn_release = getattr(wf, "release_drive_locks", release_drive_locks)
                    fn_release(self.device_path)
                except Exception as exc:
                    logger.warning("Error releasing drive locks for %s: %s", self.device_path, exc)

    def _run_workflow_impl(self, keep_going: bool = False, unknown_disc: bool = False, overwrite: bool = False) -> WorkflowResult:
        wf = _wf()
        fn_unmount = getattr(wf, "unmount_cdda_device", unmount_cdda_device)
        fn_vrc = getattr(wf, "verify_release_candidate", verify_release_candidate)
        fn_ct = getattr(wf, "compare_trees", compare_trees)
        fn_notify_fail = getattr(wf, "notify_rip_failed", notify_rip_failed)
        fn_notify_ok = getattr(wf, "notify_rip_success", notify_rip_success)

        self.flac_root.mkdir(parents=True, exist_ok=True)
        if self.generate_opus and self.opus_root:
            self.opus_root.mkdir(parents=True, exist_ok=True)
        self.whipper_stage.mkdir(parents=True, exist_ok=True)
        fn_unmount(self.device_path)
        disable_usb_autosuspend(self.device_path)

        space_ok, space_err = self.check_disk_space_preflight()
        if not space_ok:
            msg = space_err or "Insufficient disk space."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            fn_notify_fail(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED

        if not self._ensure_disc_identified(unknown_disc):
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        # 1. Extraction & Track Inspection
        self._transition(WorkflowState.RIPPING)

        rip_res = self.extraction_service.extract_and_validate(
            device_path=self.device_path,
            speed=self.speed,
            effective_offset=self.manifest.device.read_offset,
            release_id=self.release_id,
            whipper_stage=self.whipper_stage,
            cache_dir=self.cache_dir,
            full_log_path=self.manifest.full_log,
            auto_eject=self.auto_eject,
            keep_going=keep_going,
            unknown_disc=unknown_disc,
            current_metadata=self.manifest.metadata,
            ui=self.ui,
            cancel_event=self.cancel_event,
        )

        if not rip_res.success:
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            if not rip_res.cancelled and not self.cancel_event.is_set():
                fn_notify_fail(
                    artist=self.manifest.metadata.artist,
                    album=self.manifest.metadata.album,
                    reason=rip_res.error_message or "Extraction halted.",
                )
            return WorkflowResult.ABORTED

        self.device_path = rip_res.device_path
        self.manifest.device.path = rip_res.device_path
        self.manifest.device.hardware_id = get_drive_hardware_id(rip_res.device_path)
        self.manifest.device.read_offset = rip_res.effective_offset
        self.manifest.device.speed = rip_res.speed
        self.manifest.metadata = rip_res.metadata
        self.manifest.tracks.expected_count = rip_res.expected_tracks
        self.manifest.tracks.extracted_count = len(rip_res.flac_files)
        self.manifest.tracks.missing_tracks = rip_res.missing_tracks
        self.manifest.tracks.rip_elapsed_seconds = rip_res.elapsed_seconds
        self.manifest.tracks.average_speed_x = rip_res.average_speed_x
        self.manifest.verification.health_status = rip_res.health_status
        self.manifest.verification.accuraterip_summary = rip_res.accuraterip_summary
        self.manifest.verification.is_accurate = (rip_res.accuraterip_summary == "All tracks accurately ripped")
        self.manifest.verification.is_unverified = not self.manifest.verification.is_accurate and (rip_res.health_status == "No errors occurred")
        self.store.save(self.manifest)

        self._transition(WorkflowState.RIP_EXTRACTED)
        self.resolve_paths(self.manifest.metadata, partial=rip_res.partial)

        if not rip_res.partial:
            self._transition(WorkflowState.RIP_VALIDATED)

        # 2. Candidate Staging & Metadata Normalization
        self.candidate_stage.mkdir(parents=True, exist_ok=True)
        stage_flac = self.candidate_stage / "flac"
        shutil.copytree(rip_res.content_dir, stage_flac)
        (stage_flac / "whipper_exec.log").unlink(missing_ok=True)

        stage_opus: Optional[Path] = None
        if self.generate_opus:
            if self.candidate_stage_opus:
                self.candidate_stage_opus.mkdir(parents=True, exist_ok=True)
                stage_opus = self.candidate_stage_opus / "opus"
            else:
                stage_opus = self.candidate_stage / "opus"
            stage_opus.mkdir(parents=True, exist_ok=True)

        self._transition(WorkflowState.CANDIDATE_STAGED)

        c_ok, c_errs = fn_vrc(
            stage_flac,
            expected_count=len(rip_res.flac_files) if rip_res.partial else rip_res.expected_tracks,
            total_tracks=rip_res.expected_tracks,
            allow_partial=rip_res.partial,
        )
        if not c_ok or not fn_ct(rip_res.content_dir, stage_flac, ["whipper_exec.log"]):
            msg = "Candidate staging verification failed."
            self._record_error(msg)
            if c_errs:
                for ce in c_errs:
                    self._record_error(f"  • {ce}")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            fn_notify_fail(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return WorkflowResult.ABORTED

        meta = self.manifest.metadata
        has_custom_meta = bool(
            (meta.artist != "Unknown Artist" and meta.artist != sanitise_string(rip_res.raw_tag_artist))
            or (meta.album != "Unknown Album" and meta.album != sanitise_string(rip_res.raw_tag_album))
            or meta.year
            or meta.genre
            or meta.custom_track_titles
        )

        if has_custom_meta:
            self.ui.on_log(LogLevel.INFO, "Applying custom metadata tags to candidate FLAC tracks...")
            self.stage_metadata_service.normalize_candidate_tracks(stage_flac, meta, apply_tags=True)
        else:
            self.ui.on_log(LogLevel.INFO, "Normalizing track filenames for portable cross-platform filesystem compatibility...")
            self.stage_metadata_service.normalize_candidate_tracks(stage_flac, meta, apply_tags=False)

        (self.candidate_stage / ".staged-ok").write_text(
            "".join(f"{p.name}\t{flac_audio_sha256(p) or '-'}\n" for p in sorted(stage_flac.glob("*.flac"))),
            encoding="utf-8",
        )

        for f in rip_res.content_dir.glob("*.flac"):
            f.unlink(missing_ok=True)
        for f in rip_res.content_dir.glob("*.wav"):
            f.unlink(missing_ok=True)

        c_flacs = sorted(list(stage_flac.glob("*.flac")))

        cover_art = self.stage_metadata_service.resolve_and_stage_artwork(
            stage_flac=stage_flac,
            user_art=self.ui.get_cover_art(),
            mbid=self.manifest.metadata.musicbrainz_id,
            ui=self.ui,
        )

        # 3. Transcode, ReplayGain & Bitstream Check
        embed_art_temp, embed_art = self.transcode_service.prepare_embed_art(
            stage_flac,
            stage_opus,
            c_flacs,
            cover_art,
            self.cache_dir,
            self.ui,
        )

        if self.cancel_event.is_set():
            if embed_art_temp and embed_art_temp.exists():
                embed_art_temp.unlink(missing_ok=True)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        if self.generate_opus and stage_opus:
            enc_ok, enc_err = self.transcode_service.transcode_opus(
                c_flacs,
                stage_flac,
                stage_opus,
                embed_art or cover_art,
                self.ui,
                self.cancel_event,
            )
            if embed_art_temp and embed_art_temp.exists():
                embed_art_temp.unlink(missing_ok=True)
            if not enc_ok:
                if self.cancel_event.is_set():
                    self.ui.on_log(LogLevel.WARN, "Opus transcoding cancelled by user.")
                    self._transition(WorkflowState.ABORTED)
                    self.cleanup_aborted_session()
                    return WorkflowResult.ABORTED

                msg = enc_err or "Opus transcoding failed."
                self._record_error(msg)
                self._transition(WorkflowState.ABORTED)
                self.cleanup_aborted_session()
                fn_notify_fail(
                    artist=self.manifest.metadata.artist,
                    album=self.manifest.metadata.album,
                    reason=msg,
                    cover_art=cover_art,
                )
                return WorkflowResult.ABORTED

            self._transition(WorkflowState.OPUS_ENCODED)
        else:
            if embed_art_temp and embed_art_temp.exists():
                embed_art_temp.unlink(missing_ok=True)
            self._transition(WorkflowState.OPUS_ENCODED)

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        rg_ok, rg_err = self.transcode_service.apply_replaygain(
            stage_flac,
            stage_opus,
            self.generate_opus,
            self.ui,
            self.cancel_event,
        )
        if not rg_ok and self.cancel_event.is_set():
            self.ui.on_log(LogLevel.WARN, "ReplayGain cancelled by user.")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        self._transition(WorkflowState.REPLAYGAIN_APPLIED)

        if self.cancel_event.is_set():
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        if not self.transcode_service.verify_flac_integrity(c_flacs, self.ui):
            msg = "Post-tagging FLAC stream integrity check (flac -t -s) failed."
            self._record_error(msg)
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            fn_notify_fail(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return WorkflowResult.ABORTED

        self.manifest.verification.flac_integrity_passed = True
        self.store.save(self.manifest)
        self._transition(WorkflowState.CANDIDATE_VERIFIED)

        # 4. Conflict Resolution & Atomic Publication
        proceed, eff_overwrite, ts_suffix = self.publication_service.resolve_conflict(
            self.manifest, self.store, overwrite=overwrite, ui=self.ui
        )
        if not proceed or self.cancel_event.is_set():
            self.ui.on_log(LogLevel.WARN, "Publication cancelled.")
            self._transition(WorkflowState.ABORTED)
            self.cleanup_aborted_session()
            return WorkflowResult.ABORTED

        if ts_suffix:
            self.resolve_paths(self.manifest.metadata, partial=rip_res.partial, timestamp_suffix=ts_suffix)

        validate_publish_preconditions(self.manifest)
        self._transition(WorkflowState.PUBLISHING)

        pub_res = self.publication_service.publish(
            self.manifest, self.store, stage_flac, stage_opus, overwrite=eff_overwrite
        )
        if not pub_res.success:
            msg = f"Publication failed: {pub_res.error_message}"
            self._record_error(msg)
            new_state = (
                WorkflowState.PUBLISH_FAILED
                if pub_res.rollback_succeeded
                else WorkflowState.ROLLBACK_FAILED
            )
            self._transition(new_state)
            self.cleanup_aborted_session()
            fn_notify_fail(
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
        if self.candidate_stage_opus and self.candidate_stage_opus.exists():
            shutil.rmtree(self.candidate_stage_opus, ignore_errors=True)
        self.store.release_job_lock(self.job_id, self.job_lock_fd)
        self.job_lock_fd = None

        if self.manifest.error_log and self.manifest.error_log.exists() and self.manifest.error_log.stat().st_size == 0:
            self.manifest.error_log.unlink(missing_ok=True)

        self.ui.on_log(LogLevel.HEADER, "════════════════════════════════════════════════════════════")
        dest = self.manifest.paths.final_flac_dir
        is_multi = self.manifest.metadata.disc_number is not None and (
            self.manifest.metadata.disc_number > 1
            or (self.manifest.metadata.total_discs is not None and self.manifest.metadata.total_discs > 1)
        )
        final_dir = (dest.parent if is_multi else dest) if dest else self.output_root
        track_total = len(rip_res.flac_files)

        published_art: Optional[Path] = None
        if dest and dest.exists():
            published_art = find_cover_art(dest)
            if not published_art and is_multi and dest.parent.exists():
                published_art = find_cover_art(dest.parent)
        if not published_art:
            published_art = cover_art

        if is_multi and dest and dest.parent.exists() and published_art and published_art.exists():
            parent_art = dest.parent / published_art.name
            if not parent_art.exists():
                try:
                    shutil.copy2(published_art, parent_art)
                except OSError:
                    pass

        speed_summary = f" [avg speed {rip_res.average_speed_x:.1f}x]" if rip_res.average_speed_x else ""
        if not rip_res.partial and self.manifest.verification.is_accurate:
            self.ui.on_log(LogLevel.OK, f"Complete! Saved to: {final_dir}{speed_summary}")
            fn_notify_ok(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=True,
                cover_art=published_art,
                is_partial=False,
            )
            res = WorkflowResult.SUCCESS
        elif not rip_res.partial:
            self.ui.on_log(LogLevel.WARN, f"Complete (not AccurateRip verified). Saved to: {final_dir}{speed_summary}")
            fn_notify_ok(
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
            fn_notify_ok(
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
