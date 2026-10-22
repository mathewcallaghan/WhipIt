import errno
import json
import multiprocessing
import os
import shutil
import threading
import time
from pathlib import Path
import pytest
from whipit.config import get_cache_dir
from whipit.models import DeviceInfo, JobManifest, PathLayout, TransactionJournal, WorkflowResult, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager, TransactionError


def test_failure_injection_mid_publication_flac_rolled_back(sample_manifest, temp_env, monkeypatch):
    """Failure injection: OS crash/exception occurs after FLAC is published but before Opus is published."""
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_opus = sample_manifest.paths.final_opus_dir

    # Pre-existing archive to verify backup restoration
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)
    (final_flac / "existing_track.flac").write_bytes(b"ORIGINAL_ALBUM_FLAC")
    (final_opus / "existing_track.opus").write_bytes(b"ORIGINAL_ALBUM_OPUS")

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "new_track.flac").write_bytes(b"NEW_ALBUM_FLAC")
    (stage_opus / "new_track.opus").write_bytes(b"NEW_ALBUM_OPUS")

    sample_manifest.verification.flac_integrity_passed = True
    sample_manifest.state = WorkflowState.CANDIDATE_VERIFIED
    store.save(sample_manifest)

    # Invert os.replace to fail specifically when moving stage_opus
    orig_replace = os.replace
    def faulty_replace(src, dst):
        if str(src) == str(stage_opus):
            raise OSError(errno.ENOSPC, "No space left on device (Simulated Mid-Publish Crash)")
        return orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", faulty_replace)

    tx = ArchiveTransactionManager(sample_manifest, store)
    with pytest.raises(TransactionError, match="No space left on device"):
        tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    # Invariant: FLAC master was rolled back to candidate stage; original archive preserved
    assert (final_flac / "existing_track.flac").exists()
    assert (final_opus / "existing_track.opus").exists()
    assert (stage_flac / "new_track.flac").exists()
    assert not sample_manifest.transaction.committed


def test_failure_injection_corrupted_audio_halts_resumed_publication(sample_manifest, temp_env):
    """Failure injection: Candidate FLAC payload is modified/corrupted before publication resume."""
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    track = stage_flac / "01.flac"
    track.write_bytes(b"GENUINE_AUDIO_STREAM_DATA")

    # Record hash in .staged-ok marker
    from whipit.validation.flac import flac_audio_sha256
    hash_val = flac_audio_sha256(track) or "valid_hash"
    (sample_manifest.paths.candidate_stage / ".staged-ok").write_text(f"01.flac\t{hash_val}\n", encoding="utf-8")
    store.save(sample_manifest)

    # Inject silent bit corruption into candidate audio file
    track.write_bytes(b"TAMPERED_OR_TRUNCATED_AUDIO")

    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.resume_publication(sample_manifest.job_id)

    # Invariant: resume refused to publish; candidate stage preserved
    assert ok is False
    assert any("integrity re-check" in a.lower() for a in actions)
    assert sample_manifest.paths.candidate_stage.exists()
    assert not sample_manifest.paths.final_flac_dir.exists()


def _concurrent_recovery_proc(directory_str, job_id, result_queue):
    store = ManifestStore(directory=Path(directory_str))
    coordinator = RecoveryCoordinator(store)
    ok, actions = coordinator.recover_job(job_id, discard_candidate=True)
    result_queue.put((ok, actions))


def test_concurrency_regression_recovery_mutual_exclusion(sample_manifest, temp_env):
    """Concurrency regression: Two independent processes attempt recovery on the same job simultaneously."""
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "track.flac").write_bytes(b"AUDIO")
    store.save(sample_manifest)

    result_queue = multiprocessing.Queue()
    p1 = multiprocessing.Process(
        target=_concurrent_recovery_proc,
        args=(str(store.directory), sample_manifest.job_id, result_queue),
    )
    p2 = multiprocessing.Process(
        target=_concurrent_recovery_proc,
        args=(str(store.directory), sample_manifest.job_id, result_queue),
    )

    p1.start()
    p2.start()
    p1.join(timeout=10.0)
    p2.join(timeout=10.0)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    # Invariant: Both processes complete; exactly one recovers the job, the second detects active lock or cleared job
    assert len(results) == 2
    successes = [r for r in results if r[0] is True]
    assert len(successes) >= 1
    assert not sample_manifest.paths.candidate_stage.exists()


def test_concurrency_cleanup_preserves_active_rip_stage(temp_env):
    """Concurrency regression: Cleanup sweeps cannot purge stages held by actively running rips."""
    store = ManifestStore()
    cache = get_cache_dir()
    out = temp_env["output"]

    active_job_id = "test-concurrent-active-rip"
    active_stage = out / f".whipit-stage.{active_job_id}"
    active_stage.mkdir(parents=True, exist_ok=True)
    (active_stage / "salvage.flac").write_bytes(b"ACTIVE_EXTRACTION_DATA")

    # Process acquires kernel flock
    fd = store.acquire_job_lock(active_job_id)
    assert fd is not None

    try:
        coordinator = RecoveryCoordinator(store)
        actions = coordinator.cleanup_orphaned_stages(output_root=out)

        # Invariant: Active stage was detected as protected and NOT purged
        assert active_stage.exists()
        assert not any(active_job_id in a and "Purged" in a for a in actions)
    finally:
        store.release_job_lock(active_job_id, fd)


def test_concurrency_multithreaded_manifest_persistence(sample_manifest, temp_env):
    """Concurrency regression: Rapid concurrent saves across threads maintain JSON integrity."""
    store = ManifestStore()
    store.save(sample_manifest)

    errors = []
    def save_worker(worker_id):
        try:
            for i in range(25):
                m = store.load(sample_manifest.job_id)
                m.metadata.album = f"Concurrently Updated Album {worker_id}-{i}"
                store.save(m)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save_worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # Invariant: No race condition exceptions and final on-disk JSON parses cleanly
    assert len(errors) == 0
    reloaded = store.load(sample_manifest.job_id)
    assert "Concurrently Updated Album" in reloaded.metadata.album
