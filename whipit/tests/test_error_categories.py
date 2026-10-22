import errno
import pytest
from whipit.models import (
    InvariantViolationError,
    OptionalFeatureError,
    StorageIOError,
    WhipItError,
    WorkflowCancelledError,
    WorkflowResult,
    WorkflowState,
)
from whipit.workflow import RipWorkflowCoordinator


def test_error_category_inheritance():
    # InvariantViolationError must satisfy WhipItError, RuntimeError, and ValueError
    inv = InvariantViolationError("test invariant")
    assert isinstance(inv, WhipItError)
    assert isinstance(inv, RuntimeError)
    assert isinstance(inv, ValueError)

    # StorageIOError must satisfy WhipItError and OSError
    storage = StorageIOError(errno.ENOSPC, "No space left on device")
    assert isinstance(storage, WhipItError)
    assert isinstance(storage, OSError)
    assert storage.errno == errno.ENOSPC

    # WorkflowCancelledError must satisfy WhipItError
    cancel = WorkflowCancelledError("user cancelled")
    assert isinstance(cancel, WhipItError)


def test_workflow_handles_user_cancellation_distinctly(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: notified.append(kw))
    monkeypatch.setattr(coord, "_run_workflow_impl", lambda **kw: (_ for _ in ()).throw(WorkflowCancelledError("User pressed cancel")))

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED
    # Invariant: cancellation does not trigger a failure alert
    assert len(notified) == 0


def test_workflow_handles_storage_io_error(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: notified.append(kw))
    monkeypatch.setattr(coord, "_run_workflow_impl", lambda **kw: (_ for _ in ()).throw(StorageIOError(errno.ENOSPC, "Disk full during candidate stage")))

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED
    assert len(notified) == 1
    assert "ENOSPC" in notified[0]["reason"]


def test_workflow_handles_invariant_violation_error(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: notified.append(kw))
    monkeypatch.setattr(coord, "_run_workflow_impl", lambda **kw: (_ for _ in ()).throw(InvariantViolationError("Attempted publish without validation")))

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED
    assert len(notified) == 1
    assert "Invariant violation" in notified[0]["reason"]
