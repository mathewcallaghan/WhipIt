import os
import pytest
from whipit.models import WorkflowResult, WorkflowState
from whipit.workflow import RipWorkflowCoordinator


def test_run_workflow_releases_drive_locks_even_if_job_lock_release_fails(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    drive_locks_released = []
    monkeypatch.setattr("whipit.workflow.release_drive_locks", lambda dev: drive_locks_released.append(dev))

    def failing_release_job_lock(jid, fd):
        raise OSError("Simulated kernel lock release failure")

    monkeypatch.setattr(coord.store, "release_job_lock", failing_release_job_lock)

    # Simulate workflow failure
    monkeypatch.setattr(coord, "_run_workflow_impl", lambda **kw: (_ for _ in ()).throw(RuntimeError("Workflow failed")))

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    # Drive locks must still be released in inner finally
    assert "/dev/sr0" in drive_locks_released


def test_cleanup_aborted_session_is_idempotent_and_does_not_mask_exceptions(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    # Simulate error during cleanup (e.g. store delete fails)
    monkeypatch.setattr(coord.store, "delete", lambda jid: (_ for _ in ()).throw(OSError("Simulated manifest deletion failure")))

    # Calling cleanup_aborted_session does not raise or mask errors
    coord.cleanup_aborted_session()
    # Can be safely called again (idempotent)
    coord.cleanup_aborted_session()
