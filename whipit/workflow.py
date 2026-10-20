"""High-level workflow state machine coordinator and pipeline orchestrator."""

from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Optional
from whipit.audio.metadata import (
    ART_NAMES,
    embed_cover_art_flacs,
    fetch_discogs_url_from_mbid,
    find_cover_art,
    replicate_art_to_opus,
)
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.config import (
    get_cache_dir,
    get_drive_hardware_id,
    get_log_dir,
    get_output_root,
    get_stored_drive_offset,
    release_drive_locks,
    sanitise_string,
)
from whipit.inhibit import unmount_cdda_device
from whipit.models import (
    AlbumMetadata,
    DeviceInfo,
    JobManifest,
    PathLayout,
    WorkflowState,
)
from whipit.notifications import notify_rip_failed, notify_rip_success
from whipit.ripping.engine import RippingEngine
from whipit.ripping.reports import (
    find_primary_cue,
    find_whipper_log,
    parse_cue_expected_tracks,
    parse_whipper_report,
)
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager
from whipit.ui.base import LogLevel, UserInteractionPort
from whipit.ui.base import LogLevel, NullInteractionAdapter, UserInteractionPort
from whipit.validation.flac import read_flac_tag
from whipit.validation.integrity import compare_trees, verify_release_candidate
from whipit.validation.tracks import validate_track_sequence


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
        self.candidate_stage = self.output_root / f".whipit-stage.{self.job_id}"

        self.store = ManifestStore()
        self.manifest = self._initialize_manifest()
        self.store.save(self.manifest)

    def _initialize_manifest(self) -> JobManifest:
        dev_info = DeviceInfo(
            path=self.device_path,
            hardware_id=get_drive_hardware_id(self.device_path),
            read_offset=self.manual_offset or get_stored_drive_offset(self.device_path),
            speed=self.speed,
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
                with open(self.manifest.error_log, "a", encoding="utf-8") as f:
                    f.write(f"{message}\n")
            except Exception:
                pass

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.engine:
            self.engine.cancel()
        release_drive_locks(self.device_path)

    def cleanup_aborted_session(self) -> None:
        if self.whipper_stage.exists():
            shutil.rmtree(self.whipper_stage, ignore_errors=True)
        if self.candidate_stage.exists():
            shutil.rmtree(self.candidate_stage, ignore_errors=True)
        self.store.delete(self.job_id)

    def resolve_paths(self, metadata: AlbumMetadata, partial: bool = False, timestamp_suffix: str = "") -> None:
        stem = f"{metadata.artist} - {metadata.album}"
        suffix = f" (Partial {timestamp_suffix})" if partial else (f" ({timestamp_suffix})" if timestamp_suffix else "")
        master_dir = self.output_root / f"{stem}{suffix}"

        flac_dir = master_dir / f"{stem} (FLAC)"
        opus_dir = master_dir / f"{stem} (Opus)"

        is_multi = metadata.disc_number is not None and (
            metadata.disc_number > 1 or (metadata.total_discs is not None and metadata.total_discs > 1)
        )
        if is_multi and metadata.disc_number:
            flac_dir = flac_dir / f"Disc {metadata.disc_number}"
            opus_dir = opus_dir / f"Disc {metadata.disc_number}"

        self.manifest.paths.final_flac_dir = flac_dir
        self.manifest.paths.final_opus_dir = opus_dir
        self.store.save(self.manifest)

    def run_workflow(self, keep_going: bool = False, unknown_disc: bool = False, overwrite: bool = False) -> bool:
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.whipper_stage.mkdir(parents=True, exist_ok=True)
        unmount_cdda_device(self.device_path)

        # 0. Pre-extraction MusicBrainz pressing resolution if multiple editions match
        if not self.release_id and not unknown_disc and self.ui.is_interactive():
            from whipit.ripping.identify import inspect_disc
            self.ui.on_log(LogLevel.INFO, "Checking MusicBrainz for matching pressings...")
            insp = inspect_disc(self.device_path)
            if insp.matching_releases and len(insp.matching_releases) > 1:
                chosen_mbid = self.ui.prompt_select_release(insp.matching_releases)
                if not chosen_mbid:
                    self.ui.on_log(LogLevel.WARN, "Extraction cancelled.")
                    return False
                self.release_id = chosen_mbid

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
        )
        self.engine = engine

        try:
            rip_result = engine.run_extraction(keep_going=keep_going, unknown_disc=unknown_disc)
        except KeyboardInterrupt:
            if not any(self.whipper_stage.rglob("*.flac")):
                self.cleanup_aborted_session()
            raise

        exec_log = self.whipper_stage / "whipper_exec.log"
        if exec_log.exists() and self.manifest.full_log:
            try:
                shutil.copy2(exec_log, self.manifest.full_log)
            except Exception:
                pass

        if not rip_result.success:
            if not any(self.whipper_stage.rglob("*.flac")):
                self.cleanup_aborted_session()
            return False

        self.device_path = rip_result.device_path
        self.manifest.device.path = rip_result.device_path
        self.manifest.device.hardware_id = get_drive_hardware_id(rip_result.device_path)
        self.manifest.device.read_offset = rip_result.effective_offset
        self.manifest.device.speed = rip_result.speed

        self._transition(WorkflowState.RIP_EXTRACTED)

        cue_path = find_primary_cue(self.whipper_stage)
        log_path = find_whipper_log(self.whipper_stage)
        if not cue_path or not log_path:
            msg = "Required CUE sheet or Whipper log missing from extraction."
            self._record_error(msg)
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return False

        expected_tracks = parse_cue_expected_tracks(cue_path)
        health, ar_summary, ok_tracks, listed_tracks = parse_whipper_report(log_path)

        content_dir = self.whipper_stage
        while True:
            subdirs = [d for d in content_dir.iterdir() if d.is_dir()]
            subfiles = [f for f in content_dir.iterdir() if f.is_file() and f.name != "whipper_exec.log"]
            if len(subdirs) == 1 and len(subfiles) == 0:
                content_dir = subdirs[0]
            else:
                break

        flac_files = sorted(list(content_dir.glob("*.flac")))
        self.manifest.tracks.expected_count = expected_tracks
        self.manifest.tracks.extracted_count = len(flac_files)

        seq_ok, errors = validate_track_sequence(content_dir, expected_tracks)
        self.manifest.tracks.sequence_valid = seq_ok
        if errors and self.manifest.error_log:
            for err in errors:
                self._record_error(err)

        self.manifest.verification.health_status = health
        self.manifest.verification.accuraterip_summary = ar_summary
        self.manifest.verification.is_accurate = (ar_summary == "All tracks accurately ripped")
        self.manifest.verification.is_unverified = not self.manifest.verification.is_accurate and (health == "No errors occurred")

        failed = rip_result.partial or not seq_ok or (expected_tracks != len(flac_files)) or (health != "No errors occurred")

        sample_flac = flac_files[0] if flac_files else None
        if sample_flac:
            artist = read_flac_tag(sample_flac, "ALBUMARTIST") or read_flac_tag(sample_flac, "ARTIST") or "Unknown Artist"
            album = read_flac_tag(sample_flac, "ALBUM") or "Unknown Album"
            disc_str = (read_flac_tag(sample_flac, "DISCNUMBER") or read_flac_tag(sample_flac, "DISC") or "").split("/")[0]
            total_disc_str = (read_flac_tag(sample_flac, "DISCTOTAL") or read_flac_tag(sample_flac, "TOTALDISCS") or "").split("/")[0]

            disc_no = int(disc_str) if disc_str.isdigit() else None
            tot_discs = int(total_disc_str) if total_disc_str.isdigit() else None

            mbid = self.release_id or read_flac_tag(sample_flac, "MUSICBRAINZ_ALBUMID") or read_flac_tag(sample_flac, "MUSICBRAINZ_RELEASEID")
            if not mbid and exec_log.exists():
                m_mb = re.search(r"https?://musicbrainz\.org/release/([a-f0-9\-]{36})", exec_log.read_text(errors="ignore"))
                if m_mb:
                    mbid = m_mb.group(1)

            lookup_url = None
            if exec_log.exists():
                m_look = re.search(r"MusicBrainz lookup URL\s*[:\s]\s*(https?://musicbrainz\.org/cdtoc/attach\S+)", exec_log.read_text(errors="ignore"))
                if m_look:
                    lookup_url = m_look.group(1).strip()

            mb_url = f"https://musicbrainz.org/release/{mbid}" if mbid else None
            discogs_url = fetch_discogs_url_from_mbid(mbid, artist=artist, title=album) if mbid else None

            if mb_url:
                self.ui.on_log(LogLevel.INFO, f"MusicBrainz Release: {mb_url}")
            if discogs_url:
                self.ui.on_log(LogLevel.INFO, f"Discogs Entry: {discogs_url}")
            if lookup_url:
                self.ui.on_log(LogLevel.INFO, f"MusicBrainz Submit/Attach URL: {lookup_url}")

            self.manifest.metadata = AlbumMetadata(
                artist=sanitise_string(artist),
                album=sanitise_string(album),
                disc_number=disc_no,
                total_discs=tot_discs,
                album_stem=f"{sanitise_string(artist)} - {sanitise_string(album)}",
                musicbrainz_id=mbid,
                musicbrainz_url=mb_url,
                musicbrainz_lookup_url=lookup_url,
                discogs_url=discogs_url,
            )
            self.store.save(self.manifest)

        self.resolve_paths(self.manifest.metadata, partial=failed)

        if not failed:
            self._transition(WorkflowState.RIP_VALIDATED)

        self.candidate_stage.mkdir(parents=True, exist_ok=True)
        stage_flac = self.candidate_stage / "flac"
        stage_opus = self.candidate_stage / "opus"
        shutil.copytree(content_dir, stage_flac)
        (stage_flac / "whipper_exec.log").unlink(missing_ok=True)
        stage_opus.mkdir(parents=True, exist_ok=True)

        self._transition(WorkflowState.CANDIDATE_STAGED)

        c_ok, c_errs = verify_release_candidate(stage_flac, len(flac_files) if failed else expected_tracks)
        if not c_ok or not compare_trees(content_dir, stage_flac, ["whipper_exec.log"]):
            msg = "Candidate staging verification failed."
            self._record_error(msg)
            shutil.rmtree(self.candidate_stage, ignore_errors=True)
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
            )
            return False

        self.manifest.verification.flac_integrity_passed = True
        self.store.save(self.manifest)

        c_flacs = sorted(list(stage_flac.glob("*.flac")))

        # Cover art processing: user-replaced/CAA art or fallback to stage discovery
        active_art = self.ui.get_cover_art()
        cover_art: Optional[Path] = None

        if active_art and active_art.exists():
            art_ext = active_art.suffix.lower()
            if art_ext not in (".jpg", ".jpeg", ".png"):
                art_ext = ".jpg"
            target_art = stage_flac / f"cover{art_ext}"

            for old_name in ART_NAMES:
                for old_f in stage_flac.glob(old_name):
                    if old_f.resolve() != target_art.resolve():
                        old_f.unlink(missing_ok=True)

            shutil.copy2(active_art, target_art)
            cover_art = target_art
            self.ui.on_log(LogLevel.INFO, f"Applied album cover art: {cover_art.name}")
        else:
            cover_art = find_cover_art(stage_flac)

        if cover_art:
            embed_cover_art_flacs(c_flacs, cover_art)
            replicate_art_to_opus(stage_flac, stage_opus)

        self.ui.on_log(LogLevel.INFO, f"Transcoding {len(c_flacs)} track(s) to Opus 192k...")
        enc_ok = transcode_flacs_to_opus(
            c_flacs,
            stage_flac,
            stage_opus,
            cover_art,
            progress_callback=self.ui.on_transcode_progress,
            cancel_event=self.cancel_event,
        )
        if not enc_ok:
            msg = "Opus transcoding failed."
            self._record_error(msg)
            shutil.rmtree(self.candidate_stage, ignore_errors=True)
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return False

        self._transition(WorkflowState.OPUS_ENCODED)

        self.ui.on_log(LogLevel.HEADER, "Applying ReplayGain (EBU R128)...")
        rg_flac = apply_replaygain(stage_flac, "flac")
        rg_opus = apply_replaygain(stage_opus, "opus")
        if not (rg_flac and rg_opus):
            msg = "ReplayGain calculation failed."
            self._record_error(msg)
            shutil.rmtree(self.candidate_stage, ignore_errors=True)
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return False

        self._transition(WorkflowState.REPLAYGAIN_APPLIED)

        tx_mgr = ArchiveTransactionManager(self.manifest, self.store)
        proceed, eff_overwrite, ts_suffix = tx_mgr.resolve_destination_conflict(
            overwrite=overwrite,
            ui=self.ui,
        )
        if not proceed:
            self.ui.on_log(LogLevel.WARN, "Publication aborted.")
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason="Publication aborted: destination directory already exists.",
            )
            return False

        if ts_suffix:
            self.resolve_paths(self.manifest.metadata, partial=failed, timestamp_suffix=ts_suffix)

        self._transition(WorkflowState.PUBLISHING)

        try:
            tx_mgr.execute_publication(stage_flac, stage_opus, overwrite=eff_overwrite)
        except Exception as e:
            msg = f"Publication failed: {e}"
            self._record_error(msg)
            self._transition(WorkflowState.ROLLBACK_FAILED)
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason=msg,
                cover_art=cover_art,
            )
            return False

        self._transition(WorkflowState.COMMITTED)

        shutil.rmtree(self.whipper_stage, ignore_errors=True)
        shutil.rmtree(self.candidate_stage, ignore_errors=True)

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

        if not failed and self.manifest.verification.is_accurate:
            self.ui.on_log(LogLevel.OK, f"Complete! Saved to: {final_dir}")
            notify_rip_success(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=True,
                cover_art=published_art,
            )
        elif not failed:
            self.ui.on_log(LogLevel.WARN, f"Complete (not AccurateRip verified). Saved to: {final_dir}")
            notify_rip_success(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                output_dir=final_dir,
                track_count=track_total,
                is_accurate=False,
                cover_art=published_art,
            )
        else:
            self.ui.on_log(LogLevel.WARN, f"Finished with partial errors. Saved to: {final_dir}")
            notify_rip_failed(
                artist=self.manifest.metadata.artist,
                album=self.manifest.metadata.album,
                reason="Finished with partial errors.",
                cover_art=published_art,
            )
        self.ui.on_log(LogLevel.HEADER, "════════════════════════════════════════════════════════════")
        return not failed
