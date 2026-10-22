from pathlib import Path
import pytest
from whipit.models import WorkflowState
from whipit.transaction import validate_commit_preconditions, validate_publish_preconditions


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
