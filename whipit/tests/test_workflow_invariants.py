from pathlib import Path
import pytest
from whipit.models import DeviceInfo, JobManifest, PathLayout, WorkflowState
from whipit.transaction import validate_commit_preconditions, validate_publish_preconditions, validate_transaction_paths


def test_validate_publish_preconditions_rejects_unverified_flac(sample_manifest):
    sample_manifest.state = WorkflowState.CANDIDATE_VERIFIED
    sample_manifest.verification.flac_integrity_passed = False

    with pytest.raises(RuntimeError, match="FLAC integrity verification did not pass"):
        validate_publish_preconditions(sample_manifest)


def test_validate_publish_preconditions_rejects_invalid_state(sample_manifest):
    sample_manifest.state = WorkflowState.RIPPING
    sample_manifest.verification.flac_integrity_passed = True

    with pytest.raises(RuntimeError, match=r"Cannot publish from state.*RIPPING"):
        validate_publish_preconditions(sample_manifest)


def test_validate_publish_preconditions_rejects_missing_destinations(sample_manifest):
    sample_manifest.state = WorkflowState.CANDIDATE_VERIFIED
    sample_manifest.verification.flac_integrity_passed = True
    sample_manifest.paths.final_flac_dir = None

    with pytest.raises(RuntimeError, match="Missing FLAC destination"):
        validate_publish_preconditions(sample_manifest)

    sample_manifest.paths.final_flac_dir = Path("/tmp/flac")
    sample_manifest.paths.final_opus_dir = None

    with pytest.raises(RuntimeError, match="Missing Opus destination"):
        validate_publish_preconditions(sample_manifest)


def test_validate_commit_preconditions_rejects_uncommitted_journal(sample_manifest):
    sample_manifest.transaction.flac_published = True
    sample_manifest.transaction.opus_published = True
    sample_manifest.transaction.committed = False

    with pytest.raises(RuntimeError, match="transaction journal does not reflect published state"):
        validate_commit_preconditions(sample_manifest)


def test_validate_commit_preconditions_rejects_missing_destinations(sample_manifest):
    sample_manifest.transaction.flac_published = True
    sample_manifest.transaction.opus_published = True
    sample_manifest.transaction.committed = True

    with pytest.raises(RuntimeError, match="FLAC destination does not exist"):
        validate_commit_preconditions(sample_manifest)


def test_validate_commit_preconditions_succeeds_when_journal_and_outputs_agree(sample_manifest):
    sample_manifest.transaction.flac_published = True
    sample_manifest.transaction.opus_published = True
    sample_manifest.transaction.committed = True

    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)
    sample_manifest.paths.final_opus_dir.mkdir(parents=True, exist_ok=True)

    validate_commit_preconditions(sample_manifest)


def test_validate_publish_preconditions_allows_missing_opus_when_disabled(sample_manifest):
    sample_manifest.state = WorkflowState.CANDIDATE_VERIFIED
    sample_manifest.verification.flac_integrity_passed = True
    sample_manifest.paths.generate_opus = False
    sample_manifest.paths.final_opus_dir = None

    validate_publish_preconditions(sample_manifest)


def test_validate_commit_preconditions_allows_omitted_opus_when_disabled(sample_manifest):
    sample_manifest.paths.generate_opus = False
    sample_manifest.paths.final_opus_dir = None
    sample_manifest.transaction.flac_published = True
    sample_manifest.transaction.opus_published = False
    sample_manifest.transaction.committed = True

    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)

    validate_commit_preconditions(sample_manifest)


def test_independent_roots_layout_and_containment_validation(tmp_path):
    from whipit.config import get_cache_dir

    flac_root = tmp_path / "FLAC_STORAGE"
    opus_root = tmp_path / "OPUS_STORAGE"
    flac_root.mkdir()
    opus_root.mkdir()

    cache = get_cache_dir()
    job_id = "test-job-independent-roots"

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=opus_root,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=flac_root / f".whipit-stage.{job_id}",
            candidate_stage_opus=opus_root / f".whipit-stage.{job_id}",
            final_flac_dir=flac_root / "Artist - Album",
            final_opus_dir=opus_root / "Artist - Album",
        ),
    )

    validate_transaction_paths(manifest)

    # Verifies that an escape from opus_root is rejected even if inside flac_root
    manifest.paths.final_opus_dir = flac_root / "Artist - Album" / "Artist - Album (Opus)"
    with pytest.raises(Exception, match=r"outside the Opus root|escapes output_root"):
        validate_transaction_paths(manifest)
