from collections import namedtuple
import errno
import threading
from pathlib import Path
from whipit.models import DeviceInfo, JobManifest, PathLayout, ReleaseOption, WorkflowResult, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.state_store import ManifestStore
from whipit.ui.base import NullInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


def test_disk_space_preflight_detects_insufficient_space(temp_env, monkeypatch):
    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr("shutil.disk_usage", lambda p: Usage(total=10**10, used=9 * 10**9, free=100_000_000))

    coord = RipWorkflowCoordinator(device="/dev/sr0")
    space_ok, err_msg = coord.check_disk_space_preflight()
    assert space_ok is False
    assert "Insufficient" in (err_msg or "")

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED
    assert coord.job_lock_fd is None


def test_disk_space_preflight_fails_closed_on_oserror(temp_env, monkeypatch):
    def failing_disk_usage(p):
        raise PermissionError("EACCES: permission denied to stat mount")

    monkeypatch.setattr("shutil.disk_usage", failing_disk_usage)
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    space_ok, err_msg = coord.check_disk_space_preflight()
    assert space_ok is False
    assert "Cannot verify available disk space" in (err_msg or "")
    assert coord.run_workflow() == WorkflowResult.ABORTED


def test_disk_space_preflight_fails_closed_on_unexpected_exception(temp_env, monkeypatch):
    def crashing_disk_usage(p):
        raise RuntimeError("Simulated unexpected internal fault")

    monkeypatch.setattr("shutil.disk_usage", crashing_disk_usage)
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    space_ok, err_msg = coord.check_disk_space_preflight()
    assert space_ok is False
    assert "Disk-space preflight failed" in (err_msg or "")
    assert coord.run_workflow() == WorkflowResult.ABORTED


def test_coordinator_init_raises_if_job_lock_not_acquired(monkeypatch):
    import pytest
    monkeypatch.setattr("whipit.state_store.ManifestStore.acquire_job_lock", lambda self, jid: None)
    with pytest.raises(RuntimeError, match="Could not acquire job lock"):
        RipWorkflowCoordinator(device="/dev/sr0")


def test_run_workflow_handles_unexpected_copytree_failure_and_releases_lock(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")

    def failing_copytree(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("shutil.copytree", failing_copytree)

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: notified.append(kw))

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.ABORTED
    assert coord.job_lock_fd is None
    assert len(notified) == 1
    assert "ENOSPC" in notified[0]["reason"]


def test_accept_partial_rip_without_cue_or_log_synthesizes_metadata_and_commits(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockPartialRipResult:
        success = True
        cancelled = False
        partial = True
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockPartialRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())

    t1 = coord.whipper_stage / "01.flac"
    t2 = coord.whipper_stage / "02.flac"
    t1.touch()
    t2.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if "01" in f.name else "2")
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    captured_logs = []
    def mock_publish(self, stage_flac, stage_opus, **kw):
        for log in stage_flac.glob("*.log"):
            captured_logs.append((log.name, log.read_text(encoding="utf-8")))
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", mock_publish)

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_success", lambda **kw: notified.append(kw))

    res = coord.run_workflow()
    assert res == WorkflowResult.COMMITTED_PARTIAL
    assert bool(res) is True
    assert len(notified) == 1
    assert notified[0]["is_partial"] is True
    assert coord.manifest.state == WorkflowState.COMMITTED
    assert len(captured_logs) == 1
    log_name, log_content = captured_logs[0]
    assert "(WhipIt partial stub).log" in log_name
    assert coord.job_id not in log_name
    assert "NOT produced by whipper; no verification was performed" in log_content
    assert "Status: Not verified" in log_content
    assert "Status: Copy OK" not in log_content


def test_health_errors_do_not_mark_complete_album_as_partial(temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 2)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("Minor C2 errors occurred", "All tracks accurately ripped", 2, 2))

    t1 = coord.whipper_stage / "01.flac"
    t2 = coord.whipper_stage / "02.flac"
    t1.touch()
    t2.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if "01" in f.name else "2")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_success", lambda **kw: notified.append(kw))

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert "(Partial)" not in str(coord.manifest.paths.final_flac_dir)
    assert len(notified) == 1
    assert notified[0]["is_partial"] is False


def test_replaygain_failure_does_not_abort_workflow(sample_manifest, temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: False)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    ok = coord.run_workflow()
    assert ok == WorkflowResult.SUCCESS
    assert coord.manifest.state == WorkflowState.COMMITTED


def test_post_tagging_flac_verification_failure_aborts(sample_manifest, temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: False)

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED
    assert coord.candidate_stage.exists()
    assert not coord.whipper_stage.exists()


def test_pressing_cancel_cleans_up_manifest_and_stage(temp_env, monkeypatch):
    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True
        def prompt_select_release(self, releases):
            return None

    class MockInspect:
        matching_releases = [
            ReleaseOption(mbid="mbid-1", title="Release 1"),
            ReleaseOption(mbid="mbid-2", title="Release 2"),
        ]

    monkeypatch.setattr("whipit.ripping.identify.inspect_disc", lambda dev: MockInspect())
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    coord = RipWorkflowCoordinator(device="/dev/sr0", ui=MockAdapter())
    job_id = coord.job_id
    stage_path = coord.whipper_stage

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED

    assert not stage_path.exists()
    store = ManifestStore()
    assert not store.manifest_path(job_id).exists()
    assert len(store.list_uncommitted_jobs()) == 0


def test_record_error_writes_to_error_log(temp_env):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord._record_error("Test error message 12345")
    assert coord.manifest.error_log is not None
    assert coord.manifest.error_log.exists()
    assert "Test error message 12345" in coord.manifest.error_log.read_text(encoding="utf-8")


def test_transcode_cancel_suppresses_failure_notification(temp_env, monkeypatch):
    cancel_evt = threading.Event()
    coord = RipWorkflowCoordinator(device="/dev/sr0", cancel_event=cancel_evt)
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)

    notified = []
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: notified.append(kw))
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)

    def mock_transcode(*args, **kwargs):
        cancel_evt.set()
        return False

    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", mock_transcode)

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED
    assert len(notified) == 0


def test_workflow_uses_real_extraction_result_dataclass(sample_manifest, temp_env, monkeypatch):
    from whipit.ripping.engine import ExtractionResult

    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    real_result = ExtractionResult(
        success=True,
        partial=False,
        device_path="/dev/sr0",
        effective_offset=6,
        speed=None,
        cancelled=False,
    )

    class MockRealEngine:
        def run_extraction(self, **kw):
            return real_result

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockRealEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    ok = coord.run_workflow()
    assert ok == WorkflowResult.SUCCESS
    assert coord.manifest.state == WorkflowState.COMMITTED


def test_workflow_rejects_tampered_custom_art_at_publish(temp_env, monkeypatch):
    bad_art = temp_env["root"] / "tampered.jpg"
    bad_art.write_bytes(b"NOT_AN_IMAGE_PAYLOAD")

    class ArtAdapter(NullInteractionAdapter):
        def get_cover_art(self):
            return bad_art

    coord = RipWorkflowCoordinator(device="/dev/sr0", ui=ArtAdapter())
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    (coord.whipper_stage / "01.flac").touch()
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert not (coord.candidate_stage / "flac" / "cover.jpg").exists()


def test_publish_failure_distinguishes_publish_failed_from_rollback_failed(sample_manifest, temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)

    def failing_publication(*args, **kwargs):
        raise RuntimeError("Simulated permission denied publishing")

    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", failing_publication)

    ok = coord.run_workflow()
    assert ok == WorkflowResult.ABORTED
    assert coord.manifest.state == WorkflowState.PUBLISH_FAILED
    assert coord.candidate_stage.exists()
