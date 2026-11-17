"""Regression tests for Phase 1 state, transaction, recovery, and storage fixes."""

from __future__ import annotations

import datetime
import errno
import os
from collections import namedtuple
from pathlib import Path

import pytest

from whipit.config import get_cache_dir, get_log_dir
from whipit.models import (
    AlbumMetadata,
    DeviceInfo,
    JobManifest,
    PathLayout,
    TrackValidationInfo,
    TransactionJournal,
    WorkflowState,
)
from whipit.recovery import RecoveryCoordinator
from whipit.services.publication_service import PublicationService
from whipit.services.storage_service import StorageService
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager, TransactionError
from whipit.ui.base import ConflictChoice, NullInteractionAdapter


def test_shared_root_long_album_with_suffix_preserves_format_tag(temp_env: dict[str, Path]) -> None:
    shared_root = temp_env["root"] / "SHARED_LONG"
    shared_root.mkdir()
    job_id = "test-long-shared-root"

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=shared_root,
            opus_root=shared_root,
            generate_opus=True,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=shared_root / f".whipit-stage.{job_id}",
        ),
    )

    long_meta = AlbumMetadata(
        artist="Extremely Long Classical Composer And Ensemble Name " * 3,
        album="Complete Symphonies Concertos Sonatas And Chamber Works Edition " * 3,
    )
    long_suffix = "1998, AU, Cat# LONG-CATALOG-NUMBER-99999 2026-11-17_14-30-00"

    PublicationService.resolve_paths(
        manifest=manifest,
        metadata=long_meta,
        flac_root=shared_root,
        opus_root=shared_root,
        generate_opus=True,
        partial=True,
        timestamp_suffix=long_suffix,
    )

    assert manifest.paths.final_flac_dir is not None
    assert manifest.paths.final_opus_dir is not None
    assert manifest.paths.final_flac_dir.name.endswith(" (FLAC)")
    assert manifest.paths.final_opus_dir.name.endswith(" (Opus)")
    assert manifest.paths.final_flac_dir != manifest.paths.final_opus_dir
    assert len(manifest.paths.final_flac_dir.name.encode("utf-8")) <= 240
    assert len(manifest.paths.final_opus_dir.name.encode("utf-8")) <= 240
    manifest.validate_paths()


def test_unmounted_output_root_does_not_quarantine_manifest_on_startup(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManifestStore()
    offline_root = temp_env["root"] / "external_usb_drive" / "FLAC"
    job_id = "test-unmounted-drive-job"

    manifest = JobManifest(
        job_id=job_id,
        state=WorkflowState.CANDIDATE_STAGED,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=offline_root,
            opus_root=offline_root,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=offline_root / f".whipit-stage.{job_id}",
        ),
    )
    store.save(manifest)

    orig_mkdir = Path.mkdir

    def failing_mkdir(self: Path, *args, **kwargs) -> None:
        if self == offline_root or offline_root in self.parents:
            raise PermissionError(errno.EACCES, "Mount point unavailable")
        orig_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    loaded_all = store.list_all_jobs()
    loaded_uncommitted = store.list_uncommitted_jobs()
    assert len(loaded_all) == 1
    assert len(loaded_uncommitted) == 1
    assert not (store.quarantine_directory / f"{job_id}.json").exists()
    assert RecoveryCoordinator(store).inspect_uncommitted_jobs() == []

    monkeypatch.undo()
    offline_root.mkdir(parents=True)
    reports = RecoveryCoordinator(store).inspect_uncommitted_jobs()
    assert len(reports) == 1
    assert reports[0][0].job_id == job_id


def test_resume_publication_preserves_partial_suffix_and_cleans_stale_tmp_opus(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ManifestStore()
    flac_root = temp_env["output"]
    sample_manifest.tracks = TrackValidationInfo(expected_count=3, extracted_count=2, missing_tracks=[3])

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_1")
    (stage_flac / "02.flac").write_bytes(b"FLAC_2")
    (stage_opus / ".tmp.99999_1.opus").write_bytes(b"CRASHED_PARTIAL_OPUS")
    (sample_manifest.paths.candidate_stage / ".staged-ok").write_text(
        "01.flac\thash1\n02.flac\thash2\n",
        encoding="utf-8",
    )

    PublicationService.resolve_paths(
        manifest=sample_manifest,
        metadata=sample_manifest.metadata,
        flac_root=flac_root,
        opus_root=flac_root,
        generate_opus=True,
        partial=True,
        store=store,
    )
    assert sample_manifest.paths.final_flac_dir is not None
    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)

    class SuffixAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None, suggested_suffix=None):
            return ConflictChoice.TIMESTAMP_SUFFIX

    hashes = {"01.flac": "hash1", "02.flac": "hash2"}
    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda p: hashes.get(p.name))
    monkeypatch.setattr("whipit.recovery.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.recovery.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.recovery.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.recovery.verify_flac_integrity", lambda files: True)
    notified: list[dict] = []
    monkeypatch.setattr("whipit.recovery.notify_rip_success", lambda **kw: notified.append(kw))

    coordinator = RecoveryCoordinator(store, ui=SuffixAdapter())
    ok, actions = coordinator.resume_publication(sample_manifest.job_id)

    assert ok is True, actions
    reloaded = store.load(sample_manifest.job_id)
    assert reloaded.paths.final_flac_dir is not None
    assert "(Partial " in reloaded.paths.final_flac_dir.name
    assert reloaded.paths.final_opus_dir is not None
    assert not list(reloaded.paths.final_opus_dir.glob(".tmp.*.opus"))
    assert len(notified) == 1
    assert notified[0]["is_partial"] is True


def test_resolve_destination_conflict_detects_opus_root_collision(temp_env: dict[str, Path]) -> None:
    flac_root = temp_env["root"] / "FLAC_DEC"
    opus_root = temp_env["root"] / "OPUS_DEC"
    flac_root.mkdir()
    opus_root.mkdir()

    job_id = "test-opus-collision"
    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        metadata=AlbumMetadata(artist="Pink Floyd", album="The Wall", year="1979"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=opus_root,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=flac_root / f".whipit-stage.{job_id}",
            candidate_stage_opus=opus_root / f".whipit-stage.{job_id}",
            final_flac_dir=flac_root / "Pink Floyd - The Wall",
            final_opus_dir=opus_root / "Pink Floyd - The Wall",
        ),
    )
    (flac_root / "Pink Floyd - The Wall").mkdir()
    (opus_root / "Pink Floyd - The Wall (1979)").mkdir()

    class SuffixAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None, suggested_suffix=None):
            return ConflictChoice.TIMESTAMP_SUFFIX

    tx = ArchiveTransactionManager(manifest, ManifestStore())
    proceed, overwrite, suffix = tx.resolve_destination_conflict(overwrite=False, ui=SuffixAdapter())

    assert proceed is True
    assert overwrite is False
    assert suffix is not None and suffix.startswith("1979 ") and len(suffix) > len("1979")


def test_execute_publication_rejects_missing_stage_opus_when_generate_opus_true(
    sample_manifest: JobManifest,
) -> None:
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"AUDIO")

    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)
    with pytest.raises(TransactionError, match="not resolved"):
        tx.execute_publication(stage_flac, stage_opus=None, overwrite=False)
    assert sample_manifest.transaction.committed is False


def test_storage_preflight_sums_opus_on_same_filesystem(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = temp_env["cache"]
    flac_root = temp_env["root"] / "FLAC"
    opus_root = temp_env["root"] / "Opus"
    flac_root.mkdir()
    opus_root.mkdir()

    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(total=10**11, used=9 * 10**10, free=2_600_000_000))
    res_low = StorageService.evaluate_disk_space(
        cache_dir=cache_dir,
        flac_root=flac_root,
        opus_root=opus_root,
        generate_opus=True,
        ui=NullInteractionAdapter(),
    )
    assert res_low.success is False
    assert "Insufficient" in (res_low.error_message or "")

    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(total=10**11, used=9 * 10**10, free=3_000_000_000))
    res_ok = StorageService.evaluate_disk_space(
        cache_dir=cache_dir,
        flac_root=flac_root,
        opus_root=opus_root,
        generate_opus=True,
        ui=NullInteractionAdapter(),
    )
    assert res_ok.success is True


def test_prune_committed_jobs_preserves_logs_of_old_uncommitted_and_active_jobs(temp_env: dict[str, Path]) -> None:
    store = ManifestStore()
    out = temp_env["output"]
    cache = get_cache_dir()
    log_dir = get_log_dir()

    old_dt = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    old_ts = old_dt.timestamp()

    uncommitted_id = "uncommitted-old-job"
    m_uncommitted = JobManifest(
        job_id=uncommitted_id,
        created_at=old_dt,
        updated_at=old_dt,
        state=WorkflowState.CANDIDATE_STAGED,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            output_root=out,
            whipper_stage=cache / f"stage_{uncommitted_id}",
            candidate_stage=out / f".whipit-stage.{uncommitted_id}",
        ),
        transaction=TransactionJournal(committed=False),
    )
    store.save(m_uncommitted)

    uncommitted_log = log_dir / f"{uncommitted_id}_full.log"
    uncommitted_log.write_text("UNCOMMITTED_LOG", encoding="utf-8")
    os.utime(uncommitted_log, (old_ts, old_ts))

    active_id = "active-unsaved-job"
    lock_fd = store.acquire_job_lock(active_id)
    assert lock_fd is not None
    active_log = log_dir / f"{active_id}_full.log"
    active_log.write_text("ACTIVE_LOG", encoding="utf-8")

    orphan_log = log_dir / "truly-orphaned-job_full.log"
    orphan_log.write_text("ORPHAN_LOG", encoding="utf-8")

    try:
        store.prune_committed_jobs(max_keep=10, max_age_days=7)
        assert uncommitted_log.exists()
        assert active_log.exists()
        assert not orphan_log.exists()
    finally:
        store.release_job_lock(active_id, lock_fd)
