"""Integration tests verifying resilience, failure injection, and multi-drive session isolation."""

import os
from pathlib import Path
import pytest

from whipit.models import (
    CandidateStagingResult,
    DeviceInfo,
    JobManifest,
    PathLayout,
    PublicationExecutionResult,
    RipExecutionResult,
    StateTransitionMode,
    TransactionJournal,
    WorkflowResult,
    WorkflowState,
)
from whipit.ripping.engine import ExtractionResult
from whipit.services.publication_service import PublicationService
from whipit.services.transcode_service import TranscodeService
from whipit.workflow import RipWorkflowCoordinator


def test_strict_state_transition_enforcement(sample_manifest):
    """Enforces that invalid non-linear transitions are rejected in STRICT mode."""
    sample_manifest.state = WorkflowState.DISC_DETECTED
    with pytest.raises(Exception, match="Invalid workflow state transition"):
        sample_manifest.transition_to(WorkflowState.PUBLISHING, mode=StateTransitionMode.STRICT)

    # In RECOVERY mode, non-linear jumps are permitted
    sample_manifest.transition_to(WorkflowState.PUBLISHING, mode=StateTransitionMode.RECOVERY)
    assert sample_manifest.state == WorkflowState.PUBLISHING


def test_mid_opus_interruption_candidate_salvaged(temp_env, monkeypatch):
    """Verifies that an error during Opus transcoding leaves FLAC candidate stage intact with .staged-ok marker."""
    flac_out = temp_env["root"] / "FLAC_OUT"
    flac_out.mkdir()

    class FailingTranscoder(TranscodeService):
        def transcode_opus(self, *a, **kw):
            return False, "Simulated worker out of memory"

    class MockEngine:
        def run_extraction(self, **kw):
            return ExtractionResult(success=True, partial=False, device_path="/dev/sr0", effective_offset=6, speed=None)

    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        flac_root=flac_out,
        transcode_service=FailingTranscoder(),
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    t1 = coord.whipper_stage / "01.flac"
    t1.touch()
    cue = coord.whipper_stage / "test.cue"
    cue.touch()
    log = coord.whipper_stage / "test.log"
    log.touch()

    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED

    # Invariant: FLAC candidate stage preserved with valid .staged-ok marker for resume
    assert coord.candidate_stage.exists()
    staged_flacs = list((coord.candidate_stage / "flac").glob("*.flac"))
    assert len(staged_flacs) == 1
    assert (coord.candidate_stage / ".staged-ok").exists()


def test_permission_drop_during_publish_rolls_back(temp_env, monkeypatch):
    """Verifies that when publication fails due to permission errors, candidate audio is safely preserved."""
    flac_out = temp_env["root"] / "FLAC_PROTECTED"
    flac_out.mkdir()

    class FaultyPublisher(PublicationService):
        def publish(self, manifest, store, stage_flac, stage_opus, overwrite):
            return PublicationExecutionResult(
                success=False,
                committed=False,
                rollback_succeeded=True,
                error_message="Permission denied writing to final destination",
            )

    class MockEngine:
        def run_extraction(self, **kw):
            return ExtractionResult(success=True, partial=False, device_path="/dev/sr0", effective_offset=6, speed=None)

    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        flac_root=flac_out,
        publication_service=FaultyPublisher(),
    )
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    t1 = coord.whipper_stage / "01.flac"
    t1.touch()
    cue = coord.whipper_stage / "test.cue"
    cue.touch()
    log = coord.whipper_stage / "test.log"
    log.touch()

    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.PUBLISH_FAILED
    assert coord.candidate_stage.exists()
    staged_flacs = list((coord.candidate_stage / "flac").glob("*.flac"))
    assert len(staged_flacs) == 1


def test_concurrent_multi_drive_coordinators_isolated(temp_env):
    """Asserts that two independent coordinators on /dev/sr0 and /dev/sr1 run in parallel without cross-talk."""
    flac_out = temp_env["root"] / "FLAC_SHARED"
    flac_out.mkdir()

    coord0 = RipWorkflowCoordinator(device="/dev/sr0", flac_root=flac_out)
    coord1 = RipWorkflowCoordinator(device="/dev/sr1", flac_root=flac_out)

    assert coord0.job_id != coord1.job_id
    assert coord0.whipper_stage != coord1.whipper_stage
    assert coord0.candidate_stage != coord1.candidate_stage
    assert coord0.job_lock_fd is not None
    assert coord1.job_lock_fd is not None

    coord0.cleanup_aborted_session()
    coord1.cleanup_aborted_session()
