import datetime
import json
import multiprocessing
from pathlib import Path
import time
import pytest
from pydantic import ValidationError
from whipit.config import get_cache_dir, get_log_dir
from whipit.inhibit import MediaDialogInhibitor
from whipit.models import DeviceInfo, JobManifest, PathLayout, TransactionJournal, WorkflowState
from whipit.models import require_within_root
from whipit.recovery import RecoveryCoordinator
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager, validate_transaction_paths
from whipit.ui.base import NullInteractionAdapter


def test_contains_flac_bounded_scan(tmp_path, monkeypatch):
    from whipit.recovery import contains_flac

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert contains_flac(empty_dir) is False

    with_flac = tmp_path / "with_flac"
    with_flac.mkdir()
    (with_flac / "track.flac").touch()
    assert contains_flac(with_flac) is True

    # Nonexistent path returns False (no audio)
    assert contains_flac(tmp_path / "nonexistent") is False

    # Fails closed on OSError / unreadable path
    monkeypatch.setattr(Path, "rglob", lambda self, pat: (_ for _ in ()).throw(PermissionError("Permission denied")))
    assert contains_flac(empty_dir) is True

def test_prune_committed_jobs_cleans_stranded_backups_and_manifests(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    cache = get_cache_dir()

    job_id = "test-committed-crash"
    master_dir = out / "OldAlbum"
    bak_dir = master_dir / "OldAlbum (FLAC).replaced.2026-01-01_00-00-00"
    bak_dir.mkdir(parents=True, exist_ok=True)
    (bak_dir / "old_track.flac").write_bytes(b"OLD_AUDIO")

    manifest = JobManifest(
        job_id=job_id,
        updated_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            output_root=out,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=out / f".whipit-stage.{job_id}",
        ),
        transaction=TransactionJournal(
            committed=True,
            flac_backed_up=True,
            flac_backup_path=bak_dir,
        ),
    )
    store.save(manifest)

    store.prune_committed_jobs(max_keep=10, max_age_days=7)
    assert not bak_dir.exists()

    for i in range(15):
        j_id = f"job-history-{i:02d}"
        m = JobManifest(
            job_id=j_id,
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
            paths=PathLayout(
                output_root=out,
                whipper_stage=cache / f"stage_{j_id}",
                candidate_stage=out / f".whipit-stage.{j_id}",
            ),
            transaction=TransactionJournal(committed=True),
        )
        store.save(m)

    pruned = store.prune_committed_jobs(max_keep=10, max_age_days=7)
    assert pruned >= 5
    assert len([j for j in store.list_all_jobs() if j.transaction.committed]) <= 10


def test_prune_committed_jobs_preserves_recent_backups(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    cache = get_cache_dir()

    job_id = "test-recent-backup"
    master_dir = out / "RecentAlbum"
    bak_dir = master_dir / "RecentAlbum (FLAC).replaced.recent"
    bak_dir.mkdir(parents=True, exist_ok=True)
    (bak_dir / "old_track.flac").write_bytes(b"RECENT_OLD_AUDIO")

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            output_root=out,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=out / f".whipit-stage.{job_id}",
        ),
        transaction=TransactionJournal(
            committed=True,
            flac_backed_up=True,
            flac_backup_path=bak_dir,
        ),
    )
    store.save(manifest)

    store.prune_committed_jobs(max_keep=10, max_age_days=7)
    assert bak_dir.exists()
    assert (bak_dir / "old_track.flac").exists()

def test_prune_committed_jobs_cleans_associated_and_orphaned_logs(sample_manifest, temp_env):
    store = ManifestStore()
    store.save(sample_manifest)

    log_dir = get_log_dir()
    job_full = log_dir / f"{sample_manifest.job_id}_full.log"
    job_err = log_dir / f"{sample_manifest.job_id}_error.log"
    orphan_full = log_dir / "old-orphaned-job-999_full.log"
    orphan_err = log_dir / "old-orphaned-job-999_error.log"

    job_full.write_text("FULL_LOG")
    job_err.write_text("ERR_LOG")
    orphan_full.write_text("ORPHAN_FULL")
    orphan_err.write_text("ORPHAN_ERR")

    store.delete(sample_manifest.job_id)
    assert not job_full.exists()
    assert not job_err.exists()

    store.prune_committed_jobs(max_keep=10, max_age_days=7)
    assert not orphan_full.exists()
    assert not orphan_err.exists()


def test_cleanup_orphaned_stages_reclaims_disk(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    coordinator = RecoveryCoordinator(store)

    orphan_cache = get_cache_dir() / "stage_orphan-uuid-999"
    orphan_cache.mkdir(parents=True, exist_ok=True)
    orphan_stage = out / ".whipit-stage.orphan-uuid-999"
    orphan_stage.mkdir(parents=True, exist_ok=True)

    actions = coordinator.cleanup_orphaned_stages(output_root=out)
    assert len(actions) == 2
    assert not orphan_cache.exists()
    assert not orphan_stage.exists()


def test_unparseable_manifest_quarantined_and_not_silently_skipped(temp_env):
    store = ManifestStore()
    bad_file = store.directory / "corrupt-job.json"
    bad_file.write_text("{ this is not valid json", encoding="utf-8")

    jobs = store.list_all_jobs()
    assert len(jobs) == 0
    assert not bad_file.exists()
    assert (store.quarantine_directory / "corrupt-job.json").exists()
    assert "corrupt-job" in store.list_quarantined_job_ids()


def test_future_schema_version_quarantined(temp_env):
    store = ManifestStore()
    future_file = store.directory / "future-job.json"
    future_file.write_text('{"schema_version": 99, "job_id": "future-job"}', encoding="utf-8")

    jobs = store.list_all_jobs()
    assert len(jobs) == 0
    assert not future_file.exists()
    assert (store.quarantine_directory / "future-job.json").exists()
    assert "future-job" in store.list_quarantined_job_ids()


def test_cleanup_orphaned_stages_preserves_stage_with_flacs_without_confirmation(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    coordinator = RecoveryCoordinator(store)

    orphan_stage = out / ".whipit-stage.orphan-with-flacs"
    flac_dir = orphan_stage / "flac"
    flac_dir.mkdir(parents=True, exist_ok=True)
    (flac_dir / "01.flac").write_bytes(b"VALUABLE_AUDIO")

    actions = coordinator.cleanup_orphaned_stages(output_root=out)
    assert orphan_stage.exists()
    assert (flac_dir / "01.flac").exists()
    assert any("Preserved" in a and "orphan-with-flacs" in a for a in actions)


def test_cleanup_orphaned_stages_protects_quarantined_job_stage(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    coordinator = RecoveryCoordinator(store)

    job_id = "quarantined-uuid-777"
    bad_manifest = store.directory / f"{job_id}.json"
    bad_manifest.write_text("invalid json", encoding="utf-8")
    store.list_all_jobs()

    orphan_stage = out / f".whipit-stage.{job_id}"
    orphan_stage.mkdir(parents=True, exist_ok=True)
    (orphan_stage / "salvaged.wav").write_bytes(b"WAV_DATA")

    actions = coordinator.cleanup_orphaned_stages(output_root=out)
    assert orphan_stage.exists()
    assert not any("Purged" in a and job_id in a for a in actions)


def test_cleanup_orphaned_stages_purges_flacs_when_user_confirms(temp_env):
    class ConfirmingAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True
        def confirm_purge_stage(self, stage_dir: Path, flac_count: int) -> bool:
            return True

    store = ManifestStore()
    out = temp_env["output"]
    coordinator = RecoveryCoordinator(store, ui=ConfirmingAdapter())

    orphan_stage = out / ".whipit-stage.user-confirmed-purge"
    flac_dir = orphan_stage / "flac"
    flac_dir.mkdir(parents=True, exist_ok=True)
    (flac_dir / "01.flac").write_bytes(b"AUDIO")

    actions = coordinator.cleanup_orphaned_stages(output_root=out)
    assert not orphan_stage.exists()
    assert any("Purged" in a and "user-confirmed-purge" in a for a in actions)


def test_recover_job_preserves_manifest_when_stage_cleanup_fails(sample_manifest, temp_env, monkeypatch):
    import shutil
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    store.save(sample_manifest)

    orig_rmtree = shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if path == sample_manifest.paths.candidate_stage:
            raise PermissionError("Directory locked or permission denied")
        orig_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("whipit.recovery.shutil.rmtree", failing_rmtree)

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.recover_job(sample_manifest.job_id, discard_candidate=True)

    assert ok is False
    assert any("Failed removing staging directory" in a for a in actions)
    assert (store.manifest_path(sample_manifest.job_id)).exists()

def test_recover_job_discard_candidate_removes_stage_and_manifest(sample_manifest, temp_env):
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"STAGED_AUDIO")
    store.save(sample_manifest)

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.recover_job(sample_manifest.job_id, discard_candidate=True)

    assert ok is True
    assert not sample_manifest.paths.candidate_stage.exists()
    assert not (store.manifest_path(sample_manifest.job_id)).exists()
    assert len(store.list_all_jobs()) == 0


def test_recover_job_discard_candidate_removes_candidate_stage_opus(temp_env):
    store = ManifestStore()
    flac_root = temp_env["root"] / "FLAC_ROOT"
    opus_root = temp_env["root"] / "OPUS_ROOT"
    flac_root.mkdir()
    opus_root.mkdir()

    job_id = "test-recover-opus-stage"
    cand_flac = flac_root / f".whipit-stage.{job_id}"
    cand_opus = opus_root / f".whipit-stage.{job_id}"
    cand_flac.mkdir(parents=True, exist_ok=True)
    cand_opus.mkdir(parents=True, exist_ok=True)
    (cand_flac / "flac").mkdir()
    (cand_flac / "flac" / "01.flac").write_bytes(b"FLAC")
    (cand_opus / "opus").mkdir()
    (cand_opus / "opus" / "01.opus").write_bytes(b"OPUS")

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=opus_root,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=cand_flac,
            candidate_stage_opus=cand_opus,
        ),
    )
    store.save(manifest)

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.recover_job(job_id, discard_candidate=True)
    assert ok is True
    assert not cand_flac.exists()
    assert not cand_opus.exists()
    assert any("Removed Opus staging directory" in a for a in actions)


def test_resume_publication_publishes_candidate_and_commits(sample_manifest, temp_env, monkeypatch):
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_DATA")
    (sample_manifest.paths.candidate_stage / ".staged-ok").write_text("01.flac\tmock_hash\n", encoding="utf-8")
    store.save(sample_manifest)

    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda path: "mock_hash")
    monkeypatch.setattr("whipit.recovery.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.recovery.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.recovery.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.recovery.verify_flac_integrity", lambda files: True)
    notified = []
    monkeypatch.setattr("whipit.recovery.notify_rip_success", lambda **kw: notified.append(kw))

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.resume_publication(sample_manifest.job_id)

    assert ok is True
    final_flac = sample_manifest.paths.final_flac_dir
    assert final_flac.exists()
    assert (final_flac / "01.flac").exists()
    assert not sample_manifest.paths.candidate_stage.exists()

    reloaded = store.load(sample_manifest.job_id)
    assert reloaded.state == WorkflowState.COMMITTED
    assert reloaded.transaction.committed is True
    assert len(notified) == 1


def test_resume_publication_fails_when_staged_ok_missing_or_mismatched(sample_manifest, temp_env, monkeypatch):
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_DATA")
    store.save(sample_manifest)

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.resume_publication(sample_manifest.job_id)
    assert ok is False
    assert any("integrity re-check" in a for a in actions)

    # Missing expected track in candidate
    (sample_manifest.paths.candidate_stage / ".staged-ok").write_text("01.flac\thashA\n02.flac\thashB\n", encoding="utf-8")
    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda path: "hashA")
    ok2, actions2 = coordinator.resume_publication(sample_manifest.job_id)
    assert ok2 is False
    assert any("integrity re-check" in a for a in actions2)

    # Tampered audio hash
    (sample_manifest.paths.candidate_stage / ".staged-ok").write_text("01.flac\toriginal_hash\n", encoding="utf-8")
    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda path: "tampered_hash")
    ok3, actions3 = coordinator.resume_publication(sample_manifest.job_id)
    assert ok3 is False
    assert any("integrity re-check" in a for a in actions3)


def test_resume_publication_restages_from_cache_if_candidate_missing(sample_manifest, temp_env, monkeypatch):
    store = ManifestStore()
    cache_flac_dir = sample_manifest.paths.whipper_stage / "disc"
    cache_flac_dir.mkdir(parents=True, exist_ok=True)
    (cache_flac_dir / "01.flac").write_bytes(b"CACHE_FLAC_DATA")
    store.save(sample_manifest)

    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda path: "mock_hash")
    monkeypatch.setattr("whipit.recovery.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.recovery.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.recovery.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.recovery.verify_flac_integrity", lambda files: True)
    notified = []
    monkeypatch.setattr("whipit.recovery.notify_rip_success", lambda **kw: notified.append(kw))

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.resume_publication(sample_manifest.job_id)

    assert ok is True
    final_flac = sample_manifest.paths.final_flac_dir
    assert final_flac.exists()
    assert (final_flac / "01.flac").exists()

    reloaded = store.load(sample_manifest.job_id)
    assert reloaded.state == WorkflowState.COMMITTED


def test_abort_before_staged_ok_preserves_whipper_stage_and_allows_resume(sample_manifest, temp_env, monkeypatch):
    """
    Asserts that if workflow is interrupted before .staged-ok is written,
    cleanup_aborted_session preserves whipper_stage and deletes incomplete candidate_stage,
    allowing resume_publication to recover from cache and succeed.
    """
    store = ManifestStore()
    cache_disc = sample_manifest.paths.whipper_stage / "disc"
    cache_disc.mkdir(parents=True, exist_ok=True)
    (cache_disc / "01.flac").write_bytes(b"GENUINE_CACHE_FLAC")

    # Candidate stage has partial files without .staged-ok
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"UNVERIFIED_CANDIDATE_FLAC")

    store.save(sample_manifest)

    coordinator = RecoveryCoordinator(store)
    report = coordinator._reconcile_job_state(sample_manifest)
    assert "Extracted FLAC Cache Preserved" in report

    monkeypatch.setattr("whipit.recovery.flac_audio_sha256", lambda path: "mock_hash")
    monkeypatch.setattr("whipit.recovery.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.recovery.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.recovery.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.recovery.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.recovery.notify_rip_success", lambda **kw: None)

    ok, actions = coordinator.resume_publication(sample_manifest.job_id)
    assert ok is True
    final_flac = sample_manifest.paths.final_flac_dir
    assert final_flac.exists()
    assert (final_flac / "01.flac").read_bytes() == b"GENUINE_CACHE_FLAC"


def test_active_job_lock_prevents_recovery(sample_manifest, temp_env):
    store = ManifestStore()
    store.save(sample_manifest)
    coordinator = RecoveryCoordinator(store)

    lock_fd = store.acquire_job_lock(sample_manifest.job_id)
    assert lock_fd is not None
    assert store.is_job_active(sample_manifest.job_id) is True

    reports = coordinator.inspect_uncommitted_jobs()
    assert len(reports) == 0

    success, actions = coordinator.recover_job(sample_manifest.job_id)
    assert success is False
    assert any("actively running" in a for a in actions)

    res_ok, res_actions = coordinator.resume_publication(sample_manifest.job_id)
    assert res_ok is False
    assert any("actively running" in a for a in res_actions)

    store.release_job_lock(sample_manifest.job_id, lock_fd)
    assert store.is_job_active(sample_manifest.job_id) is False
    reports_after = coordinator.inspect_uncommitted_jobs()
    assert len(reports_after) == 1


def _lock_contention_worker(directory_str: str, job_id: str, counter, conflict_flag, iterations: int):
    store = ManifestStore(directory=Path(directory_str))
    for _ in range(iterations):
        fd = store.acquire_job_lock(job_id)
        if fd is not None:
            with counter.get_lock():
                counter.value += 1
                if counter.value > 1:
                    conflict_flag.value = 1
            time.sleep(0.001)
            with counter.get_lock():
                counter.value -= 1
            store.release_job_lock(job_id, fd)
        else:
            time.sleep(0.0005)


def test_job_lock_multiprocess_mutual_exclusion(temp_env):
    store = ManifestStore()
    job_id = "test-contention-job"
    counter = multiprocessing.Value("i", 0)
    conflict_flag = multiprocessing.Value("i", 0)
    iterations = 40
    procs = []

    for _ in range(4):
        p = multiprocessing.Process(
            target=_lock_contention_worker,
            args=(str(store.directory), job_id, counter, conflict_flag, iterations),
        )
        procs.append(p)
        p.start()

    for p in procs:
        p.join(timeout=10.0)
        assert not p.is_alive()

    assert conflict_flag.value == 0
    assert counter.value == 0
    assert store.is_job_active(job_id) is False


def _lock_deleter_worker(directory_str: str, job_id: str, stop_event):
    store = ManifestStore(directory=Path(directory_str))
    while not stop_event.is_set():
        store.delete(job_id)
        time.sleep(0.001)


def test_lock_acquisition_and_deletion_multiprocess_race(temp_env):
    store = ManifestStore()
    job_id = "test-race-job"
    manifest_path = store.manifest_path(job_id)
    manifest_path.touch()

    counter = multiprocessing.Value("i", 0)
    conflict_flag = multiprocessing.Value("i", 0)
    stop_event = multiprocessing.Event()

    workers = [
        multiprocessing.Process(
            target=_lock_contention_worker,
            args=(str(store.directory), job_id, counter, conflict_flag, 40),
        )
        for _ in range(3)
    ]
    deleter = multiprocessing.Process(
        target=_lock_deleter_worker,
        args=(str(store.directory), job_id, stop_event),
    )

    for w in workers:
        w.start()
    deleter.start()

    for w in workers:
        w.join(timeout=10.0)
        assert not w.is_alive()

    stop_event.set()
    deleter.join(timeout=5.0)
    assert not deleter.is_alive()

    assert conflict_flag.value == 0
    assert counter.value == 0


def test_delete_does_not_unlink_lock_file(temp_env):
    store = ManifestStore()
    job_id = "test-del-stable-lock"
    manifest_path = store.manifest_path(job_id)
    manifest_path.touch()

    fd = store.acquire_job_lock(job_id)
    assert fd is not None
    assert store.is_job_active(job_id) is True

    store.delete(job_id)
    lock_path = store.lock_path(job_id)
    assert lock_path.exists()
    assert not manifest_path.exists()
    assert store.is_job_active(job_id) is True

    store.release_job_lock(job_id, fd)
    assert store.is_job_active(job_id) is False
    store.delete(job_id)
    # Lock file remains at stable path to prevent inode race
    assert lock_path.exists()


def test_save_skips_unchanged_payload(sample_manifest, monkeypatch):
    store = ManifestStore()
    saved_count = 0
    orig_atomic = store._atomic_save_payload

    def counting_atomic(job_id, payload):
        nonlocal saved_count
        saved_count += 1
        orig_atomic(job_id, payload)

    monkeypatch.setattr(store, "_atomic_save_payload", counting_atomic)

    # First save: writes to disk
    store.save(sample_manifest)
    assert saved_count == 1

    # Second save with no change: skips redundant write
    store.save(sample_manifest)
    assert saved_count == 1

    # save_if_changed with no change: skips redundant write
    store.save_if_changed(sample_manifest)
    assert saved_count == 1

    # Modify state or field: writes to disk
    sample_manifest.transition_to(WorkflowState.RIPPING)
    store.save(sample_manifest)
    assert saved_count == 2

    # Force save: writes to disk even if unchanged
    store.save(sample_manifest, force=True)
    assert saved_count == 3

    store.delete(sample_manifest.job_id)
    assert sample_manifest.job_id not in store._last_saved_payload

def test_job_manifest_rejects_path_traversal_job_id(temp_env):
    out = temp_env["output"]
    cache = get_cache_dir()
    with pytest.raises(ValidationError):
        JobManifest(
            job_id="../../etc/passwd",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
            paths=PathLayout(
                output_root=out,
                whipper_stage=cache / "stage_x",
                candidate_stage=out / ".whipit-stage.x",
            ),
        )


def test_job_manifest_rejects_layout_tampering(temp_env):
    out = temp_env["output"]
    cache = get_cache_dir()
    with pytest.raises(ValidationError):
        JobManifest(
            job_id="valid-job-id",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
            paths=PathLayout(
                output_root=out,
                whipper_stage=Path("/etc/stage_valid-job-id"),
                candidate_stage=Path("/var/log/.whipit-stage.valid-job-id"),
            ),
        )

    with pytest.raises(ValidationError):
        JobManifest(
            job_id="valid-job-id",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
            paths=PathLayout(
                output_root=out,
                whipper_stage=cache / "stage_valid-job-id",
                candidate_stage=out / ".whipit-stage.valid-job-id",
            ),
            transaction=TransactionJournal(flac_backup_path=Path("/etc/passwd")),
        )


def test_job_manifest_rejects_broad_output_root():
    cache = get_cache_dir()
    with pytest.raises(ValidationError, match="output_root is too broad"):
        JobManifest(
            job_id="valid-job-id",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
            paths=PathLayout(
                output_root=Path("/"),
                whipper_stage=cache / "stage_valid-job-id",
                candidate_stage=Path("/.whipit-stage.valid-job-id"),
            ),
        )


def test_job_manifest_rejects_invalid_final_path_shape(temp_env):
    out = temp_env["output"]
    cache = get_cache_dir()
    with pytest.raises(ValidationError, match="does not look like a WhipIt archive directory"):
        JobManifest(
            job_id="valid-job-id",
            device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
            paths=PathLayout(
                output_root=out,
                whipper_stage=cache / "stage_valid-job-id",
                candidate_stage=out / ".whipit-stage.valid-job-id",
                final_flac_dir=out / "arbitrary_dir",
            ),
        )


def test_manifest_store_rejects_path_traversal_id():
    store = ManifestStore()
    with pytest.raises(ValueError, match="Invalid job id"):
        store.manifest_path("../../etc/passwd")
    with pytest.raises(ValueError, match="Invalid job id"):
        store.delete("../../etc/passwd")


def test_manifest_store_quarantines_mismatched_filename_and_job_id(temp_env):
    store = ManifestStore()
    bad_file = store.directory / "tampered-file.json"
    out = temp_env["output"]
    cache = get_cache_dir()
    m = JobManifest(
        job_id="legit-job-id",
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
        paths=PathLayout(
            output_root=out,
            whipper_stage=cache / "stage_legit-job-id",
            candidate_stage=out / ".whipit-stage.legit-job-id",
        ),
    )
    bad_file.write_text(m.model_dump_json(), encoding="utf-8")

    jobs = store.list_all_jobs()
    assert len(jobs) == 0
    assert not bad_file.exists()
    assert (store.quarantine_directory / "tampered-file.json").exists()


def test_inhibit_restore_rejects_untrusted_schemas_and_keys(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    state_file.write_text(json.dumps({
        "xfconf": {"/malicious/prop": "true", "/automount-media/enabled": "rm -rf"},
        "gsettings": {"org.gnome.shell": {"enabled-extensions": "true"}},
    }))
    ran_cmds = []
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/" + cmd)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: ran_cmds.append(cmd))

    assert MediaDialogInhibitor.restore_persisted_state() is False
    assert len(ran_cmds) == 0


def test_failed_rollback_preserves_staging_data(sample_manifest, temp_env, monkeypatch):
    store = ManifestStore()
    sample_manifest.paths.candidate_stage.mkdir(parents=True, exist_ok=True)
    sample_manifest.paths.whipper_stage.mkdir(parents=True, exist_ok=True)
    (sample_manifest.paths.candidate_stage / "salvaged.flac").write_bytes(b"DATA")
    store.save(sample_manifest)

    monkeypatch.setattr(ArchiveTransactionManager, "rollback", lambda self: False)

    coordinator = RecoveryCoordinator(store)
    success, actions = coordinator.recover_job(sample_manifest.job_id)

    assert success is False
    assert any("preserved" in a.lower() for a in actions)

    assert sample_manifest.paths.candidate_stage.exists()
    assert (sample_manifest.paths.candidate_stage / "salvaged.flac").exists()
    assert sample_manifest.paths.whipper_stage.exists()

    reloaded = store.load(sample_manifest.job_id)
    assert reloaded.state == WorkflowState.ROLLBACK_FAILED


def test_validate_transaction_paths_independent_containment(sample_manifest, tmp_path):
    validate_transaction_paths(sample_manifest)

    outside = tmp_path / "outside_dir"
    outside.mkdir()

    sample_manifest.paths.candidate_stage = outside
    with pytest.raises(ValueError, match="outside the output root"):
        validate_transaction_paths(sample_manifest)


def test_recover_job_aborts_on_unauthorized_paths(sample_manifest, temp_env, tmp_path):
    store = ManifestStore()
    unauthorized_stage = tmp_path / "unauthorized_stage"
    unauthorized_stage.mkdir()
    (unauthorized_stage / "important.txt").write_text("DO_NOT_DELETE")

    # Create on-disk manifest with tampered path escaping output root
    manifest_data = sample_manifest.model_dump(mode="json")
    manifest_data["paths"]["candidate_stage"] = str(unauthorized_stage)

    raw_file = store.manifest_path(sample_manifest.job_id)
    raw_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    coordinator = RecoveryCoordinator(store)
    success, actions = coordinator.recover_job(sample_manifest.job_id, discard_candidate=True)

    assert success is False
    assert unauthorized_stage.exists()
    assert (unauthorized_stage / "important.txt").exists()


def test_require_within_root_rejects_escapes_and_symlinks(tmp_path):
    root = tmp_path / "approved_root"
    root.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()

    child = root / "valid_child" / "sub"
    assert require_within_root(child, root) == child.resolve()

    with pytest.raises(ValueError, match="outside approved root"):
        require_within_root(outside / "file.txt", root)

    with pytest.raises(ValueError, match="outside approved root"):
        require_within_root(root / ".." / "outside_dir", root)

    symlink_outside = root / "symlink_out"
    symlink_outside.symlink_to(outside)
    with pytest.raises(ValueError, match="Refusing symlink path"):
        require_within_root(symlink_outside, root)

    inside_target = root / "inside_target"
    inside_target.mkdir()
    symlink_inside = root / "symlink_in"
    symlink_inside.symlink_to(inside_target)
    with pytest.raises(ValueError, match="Refusing symlink path"):
        require_within_root(symlink_inside, root)


def test_apply_timestamp_suffix_pressing_format_preserves_brackets(sample_manifest, temp_env):
    store = ManifestStore()
    coordinator = RecoveryCoordinator(store)

    coordinator._apply_timestamp_suffix(sample_manifest, "1998, AU, Cat# 532 943-2")
    flac_name = sample_manifest.paths.final_flac_dir.name
    assert flac_name == "Pink Floyd - The Wall (1998, AU, Cat# 532 943-2) (FLAC)"

    # Verify already-bracketed strings don't become nested ([ ... ])
    coordinator._apply_timestamp_suffix(sample_manifest, "[1998, AU]")
    flac_name_bracketed = sample_manifest.paths.final_flac_dir.name
    assert "([1998, AU])" not in flac_name_bracketed
    assert "[1998, AU]" in flac_name_bracketed
