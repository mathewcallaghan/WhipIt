"""Manifest reconciliation, post-crash state inspection, orphaned stage cleanup, and publication resumption."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from whipit.audio.metadata import (
    create_embedded_cover_art,
    embed_cover_art_flacs,
    find_cover_art,
    replicate_art_to_opus,
)
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.config import get_cache_dir, get_output_root, truncate_to_bytes
from whipit.models import JobManifest, WorkflowState, require_within_root
from whipit.notifications import notify_rip_success
from whipit.state_store import ManifestStore
from whipit.transaction import (
    ArchiveTransactionManager,
    TransactionError,
    validate_commit_preconditions,
    validate_publish_preconditions,
    validate_transaction_paths,
)
from whipit.ui.base import LogLevel, NullInteractionAdapter, UserInteractionPort
from whipit.validation.flac import flac_audio_sha256, verify_flac_integrity
from whipit.validation.integrity import verify_release_candidate


def contains_flac(stage_path: Path) -> bool:
    """Bounded scan returning True as soon as a FLAC file is found. Fails closed on error."""
    try:
        return next(stage_path.rglob("*.flac"), None) is not None
    except OSError:
        # Fail closed: do not treat an unreadable stage as empty.
        return True


class RecoveryCoordinator:
    def __init__(self, store: ManifestStore, ui: Optional[UserInteractionPort] = None):
        self.store = store
        self.ui = ui

    def inspect_uncommitted_jobs(self) -> List[Tuple[JobManifest, str]]:
        jobs = self.store.list_uncommitted_jobs()
        reports = []
        for j in jobs:
            if self.store.is_job_active(j.job_id):
                continue
            try:
                validate_transaction_paths(j)
            except ValueError:
                continue
            status = self._reconcile_job_state(j)
            reports.append((j, status))
        return reports

    def _reconcile_job_state(self, manifest: JobManifest) -> str:
        tx = manifest.transaction
        if tx.committed:
            return "Committed"

        stranded_backups = []
        if tx.flac_backed_up and tx.flac_backup_path and tx.flac_backup_path.exists():
            stranded_backups.append(str(tx.flac_backup_path))
        if tx.opus_backed_up and tx.opus_backup_path and tx.opus_backup_path.exists():
            stranded_backups.append(str(tx.opus_backup_path))

        if stranded_backups:
            return f"Requires Rollback (Stranded backups: {', '.join(stranded_backups)})"

        if manifest.state == WorkflowState.ROLLBACK_FAILED:
            return "Rollback Failed (Preserved for manual inspection)"

        if manifest.paths.candidate_stage.exists():
            stage_flac = manifest.paths.candidate_stage / "flac"
            if stage_flac.exists() and any(stage_flac.glob("*.flac")):
                if manifest.state == WorkflowState.PUBLISH_FAILED:
                    return f"Publication Failed (Rolled back, verified FLAC candidate preserved: {manifest.paths.candidate_stage})"
                return f"Verified FLAC Candidate Preserved ({manifest.paths.candidate_stage})"

            return f"Incomplete Publication (Candidate present: {manifest.paths.candidate_stage})"

        if manifest.paths.whipper_stage.exists():
            if any(manifest.paths.whipper_stage.rglob("*.flac")):
                return f"Extracted FLAC Cache Preserved ({manifest.paths.whipper_stage})"
            return f"Incomplete Extraction (Cache present: {manifest.paths.whipper_stage})"

        return "Aborted prior to publication"

    def cleanup_orphaned_stages(
        self,
        output_root: Optional[Path] = None,
        ui: Optional[UserInteractionPort] = None,
    ) -> List[str]:
        """
        Scans cache_dir and output_root for abandoned staging directories not owned by any active job.
        Reclaims stranded disk space, but never purges staging directories containing .flac files
        without explicit user confirmation.
        """
        actions = []
        effective_ui = ui or self.ui
        all_jobs = self.store.list_all_jobs()
        active_job_ids = {j.job_id for j in all_jobs if self.store.is_job_active(j.job_id)}
        known_uncommitted_ids = {j.job_id for j in all_jobs if not j.transaction.committed}
        quarantined_ids = self.store.list_quarantined_job_ids()
        protected_ids = active_job_ids | known_uncommitted_ids | quarantined_ids

        def _should_purge_stage(stage_path: Path) -> bool:
            try:
                has_flac = contains_flac(stage_path)
            except OSError:
                if effective_ui:
                    effective_ui.on_log(LogLevel.WARN, f"Unreadable staging directory preserved: {stage_path}")
                return False

            if not has_flac:
                return True

            if effective_ui and effective_ui.is_interactive():
                try:
                    flac_count = sum(1 for _ in stage_path.rglob("*.flac"))
                except OSError:
                    flac_count = 1
                return effective_ui.confirm_purge_stage(stage_path, flac_count)

            return False

        # 1. Clean cache_dir stage_* directories
        cache_dir = get_cache_dir()
        if cache_dir.exists():
            for d in cache_dir.glob("stage_*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace("stage_", "")
                if job_id not in protected_ids:
                    if _should_purge_stage(d):
                        shutil.rmtree(d, ignore_errors=True)
                        actions.append(f"Purged abandoned cache stage: {d.name}")
                    else:
                        actions.append(f"Preserved orphaned cache stage with FLAC files: {d.name}")

        # 2. Clean .whipit-stage.* directories across all configured roots
        from whipit.config import load_preferences
        prefs = load_preferences()
        roots_to_scan = set()
        if output_root:
            roots_to_scan.add(output_root.resolve())
        else:
            roots_to_scan.add(get_output_root().resolve())
            roots_to_scan.add(prefs.flac_root.resolve())
            if prefs.opus_root:
                roots_to_scan.add(prefs.opus_root.resolve())

        for root in roots_to_scan:
            if not root.exists():
                continue
            for d in root.glob(".whipit-stage.*"):
                if not d.is_dir():
                    continue
                job_id = d.name.replace(".whipit-stage.", "")
                if job_id not in protected_ids:
                    if _should_purge_stage(d):
                        shutil.rmtree(d, ignore_errors=True)
                        actions.append(f"Purged abandoned candidate stage: {d.name}")
                    else:
                        actions.append(f"Preserved orphaned candidate stage with FLAC files: {d.name}")

        return actions

    def recover_job(self, job_id: str, discard_candidate: bool = False) -> Tuple[bool, List[str]]:
        """
        Safely rolls back partial publications and removes temporary stages only on success.
        If rollback fails, staging files are preserved to prevent data destruction.
        """
        fd = self.store.acquire_job_lock(job_id)
        if fd is None:
            return False, [f"Job {job_id} is actively running in another process and cannot be recovered."]

        try:
            try:
                manifest = self.store.load(job_id)
                validate_transaction_paths(manifest)
            except FileNotFoundError:
                return True, [f"Manifest for job {job_id} not found (already cleared)."]
            except Exception as exc:
                return False, [f"Error loading manifest for job {job_id}: {exc}"]
            mgr = ArchiveTransactionManager(manifest, self.store)
            success = mgr.rollback()
            actions: List[str] = []

            if not success:
                manifest.transition_to(WorkflowState.ROLLBACK_FAILED)
                self.store.save(manifest)
                actions.append("Rollback failed! Staging and cache directories preserved for manual inspection.")
                return False, actions

            actions.append("Rolled back transactional changes")

            try:
                require_within_root(manifest.paths.candidate_stage, manifest.paths.output_root)
                require_within_root(manifest.paths.whipper_stage, get_cache_dir())
            except ValueError as ve:
                return False, [f"Unsafe path detected during recovery: {ve}"]

            stage_flac = manifest.paths.candidate_stage / "flac"
            has_flacs = stage_flac.exists() and any(stage_flac.glob("*.flac"))
            has_cache_flacs = manifest.paths.whipper_stage.exists() and any(manifest.paths.whipper_stage.rglob("*.flac"))
            has_any_flacs = has_flacs or has_cache_flacs

            cleanup_errors = False

            if has_flacs and not discard_candidate:
                actions.append(f"Preserved extracted FLAC candidate: {manifest.paths.candidate_stage}")
            elif manifest.paths.candidate_stage.exists():
                try:
                    if manifest.paths.candidate_stage.is_symlink():
                        raise ValueError(f"Refusing to delete symlinked stage: {manifest.paths.candidate_stage}")
                    shutil.rmtree(manifest.paths.candidate_stage)
                    actions.append(f"Removed staging directory: {manifest.paths.candidate_stage}")
                except Exception as e:
                    cleanup_errors = True
                    actions.append(f"Failed removing staging directory ({e}): {manifest.paths.candidate_stage}")

            if has_cache_flacs and not discard_candidate:
                actions.append(f"Preserved extracted FLAC cache: {manifest.paths.whipper_stage}")
            elif manifest.paths.whipper_stage.exists():
                try:
                    if manifest.paths.whipper_stage.is_symlink():
                        raise ValueError(f"Refusing to delete symlinked cache: {manifest.paths.whipper_stage}")
                    shutil.rmtree(manifest.paths.whipper_stage)
                    actions.append(f"Removed cache directory: {manifest.paths.whipper_stage}")
                except Exception as e:
                    cleanup_errors = True
                    actions.append(f"Failed removing cache directory ({e}): {manifest.paths.whipper_stage}")

            if not (has_any_flacs and not discard_candidate) and not cleanup_errors:
                self.store.delete(job_id)
                actions.append("Cleared job manifest")
            else:
                self.store.save(manifest)

            return not cleanup_errors, actions
        finally:
            self.store.release_job_lock(job_id, fd)

    def _apply_timestamp_suffix(self, manifest: JobManifest, ts_suffix: str) -> None:
        if not ts_suffix or not manifest.paths.final_flac_dir:
            return
        flac_root = (manifest.paths.flac_root or manifest.paths.output_root or get_output_root()).resolve()
        opus_root = (manifest.paths.opus_root or flac_root).resolve()
        raw_stem = f"{manifest.metadata.artist} - {manifest.metadata.album}"
        stem = truncate_to_bytes(raw_stem, max_bytes=180)
        master_name = truncate_to_bytes(f"{stem} ({ts_suffix})", max_bytes=240)

        flac_name = truncate_to_bytes(f"{stem} (FLAC)", max_bytes=220)
        flac_dir = flac_root / master_name / flac_name

        opus_dir: Optional[Path] = None
        if manifest.paths.generate_opus and manifest.paths.final_opus_dir:
            opus_name = truncate_to_bytes(f"{stem} (Opus)", max_bytes=220)
            opus_dir = opus_root / master_name / opus_name

        is_multi = manifest.metadata.disc_number is not None and (
            manifest.metadata.disc_number > 1
            or (manifest.metadata.total_discs is not None and manifest.metadata.total_discs > 1)
        )
        if is_multi and manifest.metadata.disc_number:
            flac_dir = flac_dir / f"Disc {manifest.metadata.disc_number}"
            if opus_dir:
                opus_dir = opus_dir / f"Disc {manifest.metadata.disc_number}"

        manifest.paths.final_flac_dir = flac_dir
        manifest.paths.final_opus_dir = opus_dir
        self.store.save(manifest)

    def resume_publication(
        self,
        job_id: str,
        overwrite: bool = False,
        ui: Optional[UserInteractionPort] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Resumes and finishes publication for a preserved candidate stage or cache directory.
        Completes Opus encoding, ReplayGain tagging, integrity verification, and atomic commit.
        """
        fd = self.store.acquire_job_lock(job_id)
        if fd is None:
            return False, [f"Job {job_id} is actively running in another process."]

        try:
            manifest = self.store.load(job_id)
            validate_transaction_paths(manifest)
            require_within_root(manifest.paths.candidate_stage, manifest.paths.output_root)
            require_within_root(manifest.paths.whipper_stage, get_cache_dir())
            effective_ui = ui or self.ui or NullInteractionAdapter()
            stage_flac = manifest.paths.candidate_stage / "flac"
            stage_opus: Optional[Path] = None
            if manifest.paths.generate_opus:
                candidate_opus = manifest.paths.candidate_stage_opus or manifest.paths.candidate_stage
                stage_opus = candidate_opus / "opus"

            # If candidate stage has no FLACs, but whipper cache has them, re-stage from cache
            if (not stage_flac.exists() or not any(stage_flac.glob("*.flac"))) and manifest.paths.whipper_stage.exists():
                cache_flacs = sorted(list(manifest.paths.whipper_stage.rglob("*.flac")))
                if cache_flacs:
                    content_dir = cache_flacs[0].parent
                    manifest.paths.candidate_stage.mkdir(parents=True, exist_ok=True)
                    if stage_flac.exists():
                        shutil.rmtree(stage_flac, ignore_errors=True)
                    shutil.copytree(content_dir, stage_flac)
                    (stage_flac / "whipper_exec.log").unlink(missing_ok=True)
                    (manifest.paths.candidate_stage / ".staged-ok").write_text(
                        "".join(f"{p.name}\t{flac_audio_sha256(p) or '-'}\n" for p in sorted(stage_flac.glob("*.flac"))),
                        encoding="utf-8",
                    )

            if not stage_flac.exists():
                return False, [f"Staged FLAC directory does not exist: {stage_flac}"]

            c_flacs = sorted(list(stage_flac.glob("*.flac")))
            if not c_flacs:
                return False, [f"No FLAC files found in staging directory: {stage_flac}"]

            marker = manifest.paths.candidate_stage / ".staged-ok"
            entries = (
                dict(l.split("\t", 1) for l in marker.read_text(encoding="utf-8").splitlines() if "\t" in l)
                if marker.exists()
                else None
            )
            if entries is None or set(entries) != {f.name for f in c_flacs} or any(
                h == "-" or flac_audio_sha256(stage_flac / n) != h for n, h in entries.items()
            ):
                return False, ["Candidate stage failed its integrity re-check; refusing to publish."]

            is_partial = bool(manifest.tracks.missing_tracks) or (
                manifest.tracks.expected_count > 0 and len(c_flacs) < manifest.tracks.expected_count
            )
            cand_ok, cand_errs = verify_release_candidate(
                stage_flac,
                expected_count=len(c_flacs),
                total_tracks=manifest.tracks.expected_count or None,
                allow_partial=is_partial,
            )
            if not cand_ok:
                return False, cand_errs

            cover_art = find_cover_art(stage_flac)
            embed_art_temp: Optional[Path] = None
            embed_art: Optional[Path] = None

            if cover_art and cover_art.exists():
                embed_art_temp = get_cache_dir() / f"embed_cover_{uuid.uuid4().hex[:8]}.jpg"
                embed_art = create_embedded_cover_art(cover_art, embed_art_temp, max_dimension=1000)
                embed_cover_art_flacs(c_flacs, embed_art)

            if manifest.paths.generate_opus and stage_opus:
                stage_opus.mkdir(parents=True, exist_ok=True)
                if cover_art and cover_art.exists():
                    replicate_art_to_opus(stage_flac, stage_opus)
                enc_ok = transcode_flacs_to_opus(
                    c_flacs,
                    stage_flac,
                    stage_opus,
                    embed_art or cover_art,
                    progress_callback=effective_ui.on_transcode_progress,
                )
                if embed_art_temp and embed_art_temp.exists():
                    embed_art_temp.unlink(missing_ok=True)
                if not enc_ok:
                    return False, ["Opus transcoding failed during resume."]
            else:
                if embed_art_temp and embed_art_temp.exists():
                    embed_art_temp.unlink(missing_ok=True)

            manifest.transition_to(WorkflowState.OPUS_ENCODED)

            apply_replaygain(stage_flac, "flac")
            if manifest.paths.generate_opus and stage_opus:
                apply_replaygain(stage_opus, "opus")
            manifest.transition_to(WorkflowState.REPLAYGAIN_APPLIED)

            if not verify_flac_integrity(c_flacs):
                return False, ["Candidate FLAC stream verification failed."]

            manifest.verification.flac_integrity_passed = True
            manifest.transition_to(WorkflowState.CANDIDATE_VERIFIED)

            # Ensure destination paths are resolved
            if not manifest.paths.final_flac_dir or (manifest.paths.generate_opus and not manifest.paths.final_opus_dir):
                flac_root = (manifest.paths.flac_root or manifest.paths.output_root or get_output_root()).resolve()
                opus_root = (manifest.paths.opus_root or flac_root).resolve()
                raw_stem = f"{manifest.metadata.artist} - {manifest.metadata.album}"
                stem = truncate_to_bytes(raw_stem, max_bytes=180)
                master_name = truncate_to_bytes(stem, max_bytes=240)
                manifest.paths.final_flac_dir = flac_root / master_name / truncate_to_bytes(f"{stem} (FLAC)", max_bytes=220)
                if manifest.paths.generate_opus:
                    manifest.paths.final_opus_dir = opus_root / master_name / truncate_to_bytes(f"{stem} (Opus)", max_bytes=220)
                self.store.save(manifest)

            tx_mgr = ArchiveTransactionManager(manifest, self.store)
            proceed, eff_overwrite, ts_suffix = tx_mgr.resolve_destination_conflict(
                overwrite=overwrite,
                ui=effective_ui,
            )
            if not proceed:
                return False, ["Publication cancelled or destination collision unresolvable."]

            if ts_suffix:
                self._apply_timestamp_suffix(manifest, ts_suffix)

            validate_publish_preconditions(manifest)
            manifest.transition_to(WorkflowState.PUBLISHING)

            try:
                tx_mgr.execute_publication(stage_flac, stage_opus, overwrite=eff_overwrite)
            except Exception as exc:
                rb_ok = tx_mgr.rollback()
                manifest.transition_to(WorkflowState.PUBLISH_FAILED if rb_ok else WorkflowState.ROLLBACK_FAILED)
                self.store.save(manifest)
                return False, [f"Publication failed: {exc}"]

            validate_commit_preconditions(manifest)
            manifest.transition_to(WorkflowState.COMMITTED)
            shutil.rmtree(manifest.paths.candidate_stage, ignore_errors=True)
            if manifest.paths.candidate_stage_opus and manifest.paths.candidate_stage_opus.exists():
                shutil.rmtree(manifest.paths.candidate_stage_opus, ignore_errors=True)
            if manifest.paths.whipper_stage.exists():
                shutil.rmtree(manifest.paths.whipper_stage, ignore_errors=True)
            self.store.save(manifest)

            dest = manifest.paths.final_flac_dir
            final_dir = dest.parent if dest else (manifest.paths.output_root or get_output_root())
            published_art = find_cover_art(dest) if dest and dest.exists() else cover_art

            notify_rip_success(
                artist=manifest.metadata.artist,
                album=manifest.metadata.album,
                output_dir=final_dir,
                track_count=len(c_flacs),
                is_accurate=manifest.verification.is_accurate,
                cover_art=published_art,
            )

            return True, [f"Successfully published archive to {final_dir}"]
        finally:
            self.store.release_job_lock(job_id, fd)
