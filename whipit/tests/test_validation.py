import json
import os
import threading
import time
from pathlib import Path
import urllib.request
import pytest
import whipit.ui
import whipit.workflow
from whipit.audio.metadata import fetch_cover_art_from_caa, fetch_release_details
from whipit.audio.opus import transcode_flacs_to_opus
from whipit.bootstrap import (
    OPTIONAL_TOOLS,
    REQUIRED_TOOLS,
    UNMANAGED_PACKAGES,
    detect_package_manager,
)
from whipit.config import _is_ripper_holding_device, get_cache_dir, sanitise_string, truncate_to_bytes
from whipit.inhibit import MediaDialogInhibitor
from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout, ReleaseOption, TransactionJournal, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.ripping.calibration import CalibrationResult, CalibrationStatus, calibrate_drive
from whipit.ripping.engine import RippingEngine
from whipit.ripping.identify import inspect_disc
from whipit.ripping.reports import parse_cue_expected_tracks, parse_whipper_report
from whipit.ripping.whipper import WhipperRunner
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    NullInteractionAdapter,
)
from whipit.validation.integrity import compare_trees, verify_release_candidate
from whipit.validation.tracks import validate_track_sequence
from whipit.workflow import RipWorkflowCoordinator


def test_ui_import_without_eager_qt():
    """Importing whipit.ui must expose core base classes without forcing PyQt6 instantiation."""
    assert hasattr(whipit.ui, "UserInteractionPort")
    assert hasattr(whipit.ui, "LogLevel")
    assert hasattr(whipit.ui, "ConflictChoice")


def test_prune_committed_jobs_cleans_stranded_backups_and_manifests(temp_env):
    store = ManifestStore()
    out = temp_env["output"]
    cache = temp_env["cache"]

    job_id = "test-committed-crash"
    bak_dir = out / "OldAlbum (FLAC).replaced.2026-01-01_00-00-00"
    bak_dir.mkdir(parents=True, exist_ok=True)
    (bak_dir / "old_track.flac").write_bytes(b"OLD_AUDIO")

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


def test_truncate_to_bytes_ascii_and_multibyte():
    s_ascii = "A" * 300
    res_ascii = truncate_to_bytes(s_ascii, max_bytes=200)
    assert len(res_ascii.encode("utf-8")) <= 200
    assert len(res_ascii) == 200

    s_kanji = "日本語テスト" * 20
    res_kanji = truncate_to_bytes(s_kanji, max_bytes=50)
    encoded = res_kanji.encode("utf-8")
    assert len(encoded) <= 50
    assert encoded.decode("utf-8") == res_kanji

    s_emoji = "🎵" * 70
    res_emoji = truncate_to_bytes(s_emoji, max_bytes=15)
    assert len(res_emoji.encode("utf-8")) == 12


def test_resolve_paths_long_tags_prevent_errno_36(sample_manifest, temp_env):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    long_meta = AlbumMetadata(
        artist="Super Long Artist Name In Classical Release " * 8,
        album="The Complete Symphonies Serenades Divertimenti Operas and Concertos " * 6,
    )

    coord.resolve_paths(long_meta, partial=True, timestamp_suffix="2026-10-20_15-30-00")
    flac_dir = coord.manifest.paths.final_flac_dir
    assert flac_dir is not None

    master_component = flac_dir.parent.name
    flac_component = flac_dir.name

    assert len(master_component.encode("utf-8")) <= 255
    assert len(flac_component.encode("utf-8")) <= 255

    flac_dir.mkdir(parents=True, exist_ok=True)
    assert flac_dir.exists()


def test_gui_adapter_shutdown_unblocks_waiting_threads():
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.adapter import GuiInteractionAdapter

    adapter = GuiInteractionAdapter()
    unblocked = threading.Event()

    def worker_thread():
        res = adapter.prompt_insert_disc("/dev/sr0", "TEST_HW")
        assert res.action == DiscPromptAction.ABORT
        unblocked.set()

    t = threading.Thread(target=worker_thread)
    t.start()

    adapter.shutdown()
    t.join(timeout=1.0)
    assert not t.is_alive()
    assert unblocked.is_set()


def test_calibration_manual_offset_skips_drive_analyze(monkeypatch):
    analyzed = []
    monkeypatch.setattr("whipit.ripping.calibration.store_drive_offset", lambda dev, off: None)
    monkeypatch.setattr("whipit.ripping.calibration._run_whipper_streaming", lambda cmd, *a, **kw: analyzed.append(cmd))

    class MockAdapter(NullInteractionAdapter):
        def prompt_calibration_start(self, dev):
            return CalibrationChoice(CalibrationAction.MANUAL_OFFSET, manual_offset=12)

    res = calibrate_drive("/dev/sr0", ui=MockAdapter())
    assert res.status == CalibrationStatus.SUCCESS
    assert res.offset == 12
    assert len(analyzed) == 0


def test_calibration_cancellation_aborts_immediately(monkeypatch):
    cancel_evt = threading.Event()
    cancel_evt.set()

    executed = []
    monkeypatch.setattr("whipit.ripping.calibration._run_whipper_streaming", lambda cmd, *a, **kw: executed.append(cmd))

    class MockAdapter(NullInteractionAdapter):
        def prompt_calibration_start(self, dev):
            return CalibrationChoice(CalibrationAction.AUTO_DETECT)

    res = calibrate_drive("/dev/sr0", ui=MockAdapter(), cancel_event=cancel_evt)
    assert res.status == CalibrationStatus.ABORTED
    assert res.offset is None
    assert len(executed) == 0


def test_inspect_disc_timeout_enforced(monkeypatch):
    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)

    class BlockingStdout:
        def readline(self):
            time.sleep(10.0)
            return ""
        def close(self):
            pass

    class HangingProcess:
        def __init__(self, *args, **kwargs):
            self.args = args[0] if args else kwargs.get("args", ["whipper"])
            self.stdin = None
            self.stderr = None
            self.stdout = BlockingStdout()
            self.returncode = None
            self.pid = 99999

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            return ("", "")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", HangingProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)

    t0 = time.time()
    res = inspect_disc("/dev/sr0", timeout=0.15)
    elapsed = time.time() - t0
    assert res.success is False
    assert "Timeout" in (res.error_message or "")
    assert elapsed < 1.0


def test_musicbrainz_user_agent_format(monkeypatch):
    captured_headers = {}

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"{}"

    def mock_urlopen(req, timeout=8.0):
        if hasattr(req, "headers"):
            captured_headers.update(req.headers)
        return MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)

    fetch_release_details("dummy-mbid")
    ua = captured_headers.get("User-agent") or captured_headers.get("User-Agent")
    assert ua is not None
    assert "https://github.com/mathewcallaghan/WhipIt" in ua


def test_opus_transcode_catches_future_exceptions(tmp_path, monkeypatch):
    flac = tmp_path / "01.flac"
    flac.touch()

    def mock_encode(*args, **kwargs):
        raise RuntimeError("Simulated unexpected disk error")

    monkeypatch.setattr("whipit.audio.opus._encode_single_track", mock_encode)

    ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", cover_art=None)
    assert ok is False


def test_opus_transcode_immediate_cancellation(tmp_path, monkeypatch):
    flac = tmp_path / "01.flac"
    flac.touch()
    cancel_evt = threading.Event()
    cancel_evt.set()

    ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", cover_art=None, cancel_event=cancel_evt)
    assert ok is False


def test_inhibit_state_crash_recovery(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))

    mock_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {"org.gnome.desktop.media-handling": {"automount": "true"}}
    }
    state_file.write_text(json.dumps(mock_payload))

    restored_cmds = []
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/" + cmd)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: restored_cmds.append(cmd))

    did_restore = MediaDialogInhibitor.restore_persisted_state()
    assert did_restore is True
    assert not state_file.exists()
    assert any("xfconf-query" in c and "true" in c for c in restored_cmds)
    assert any("gsettings" in c and "true" in c for c in restored_cmds)


def test_get_configured_repo_from_git_remote(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import get_configured_repo

    fake_git = tmp_path / "repo"
    fake_git.mkdir()
    (fake_git / ".git").mkdir()

    class MockRun:
        returncode = 0
        stdout = "https://github.com/my-custom-org/CustomWhipIt.git\n"

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MockRun())

    repo = get_configured_repo(source_dir=fake_git)
    assert repo == "my-custom-org/CustomWhipIt"


def test_update_via_git_pip_failure_reports_error(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda success, msg, restart: emitted.append((success, msg, restart)))

    def mock_run(cmd, *a, **kw):
        class Res:
            returncode = 0
            stdout = ""
            stderr = ""

        if "status" in cmd:
            Res.stdout = "Your branch is behind 'origin/main' by 1 commit\n"
        elif "pull" in cmd:
            Res.returncode = 0
        elif "pip" in cmd:
            Res.returncode = 1
            Res.stderr = "ERROR: Failed building wheel for whipit\n"
        return Res()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, restart = emitted[0]
    assert success is False
    assert "Package installation failed" in msg
    assert restart is False


def test_required_tools_specification():
    assert "diff" not in REQUIRED_TOOLS
    assert "fuser" not in REQUIRED_TOOLS
    assert "notify-send" not in REQUIRED_TOOLS
    assert "notify-send" in OPTIONAL_TOOLS
    assert "pkill" in REQUIRED_TOOLS
    assert "udisksctl" in REQUIRED_TOOLS
    assert "gio" in REQUIRED_TOOLS


def test_package_manager_maps(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/apt-get" if cmd == "apt-get" else None)
    info = detect_package_manager()
    assert info is not None
    assert info["manager"] == "apt-get"
    pkg_map = info["pkg_map"]
    assert pkg_map["cd-paranoia"] == "libcdio-utils"
    assert pkg_map["pkill"] == "procps"
    assert pkg_map["udisksctl"] == "udisks2"
    assert pkg_map["gio"] == "libglib2.0-bin"


def test_ripper_locking_filters_media_players(tmp_path):
    proc_root = tmp_path / "proc"
    fake_proc = proc_root / "12345"
    fake_proc.mkdir(parents=True)
    (fake_proc / "comm").write_text("vlc\n")
    (fake_proc / "cmdline").write_bytes(b"vlc\x00cdda:///dev/sr0\x00")

    target_devs = {"/dev/sr0"}
    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}

    is_target = _is_ripper_holding_device(12345, target_devs, target_binaries, proc_root=proc_root)
    assert is_target is False


def test_ripper_locking_exact_device_matching(tmp_path):
    proc_root = tmp_path / "proc"
    fake_proc = proc_root / "9999"
    fake_proc.mkdir(parents=True)
    (fake_proc / "comm").write_text("cd-paranoia\n")
    (fake_proc / "cmdline").write_bytes(b"cd-paranoia\x00-d\x00/dev/sr10\x00-S\x004\x00")

    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}

    assert _is_ripper_holding_device(9999, {"/dev/sr1"}, target_binaries, proc_root=proc_root) is False
    assert _is_ripper_holding_device(9999, {"/dev/sr10"}, target_binaries, proc_root=proc_root) is True


def test_cue_track_count_parsing(tmp_path: Path):
    cue = tmp_path / "album.cue"
    cue.write_text("""
TITLE "Sample Album"
PERFORMER "Sample Artist"
FILE "track01.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
FILE "track02.flac" WAVE
  TRACK 02 AUDIO
    INDEX 01 03:00:00
FILE "track03.flac" WAVE
  TRACK 03 AUDIO
    INDEX 01 07:00:00
""")
    assert parse_cue_expected_tracks(cue) == 3


def test_whipper_report_parsing(tmp_path: Path):
    report = tmp_path / "report.log"
    report.write_text("""
Whipper report
Health status: No errors occurred
AccurateRip summary: All tracks accurately ripped
Filename: track01.flac
Status: Copy OK
Filename: track02.flac
Status: Copy OK
""")
    health, ar, ok_tracks, listed = parse_whipper_report(report)
    assert health == "No errors occurred"
    assert ar == "All tracks accurately ripped"
    assert ok_tracks == 2
    assert listed == 2


def test_track_sequence_detects_gap(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "03.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = "TRACKNUMBER=01\n" if "01.flac" in cmd[-1] else "TRACKNUMBER=03\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 3)
    assert not valid
    assert any("found 2, expected exactly 3" in e for e in errors)


def test_track_sequence_detects_duplicate_and_out_of_bounds(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "02.flac").touch()
    (tmp_path / "03.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = ""
        target = str(cmd[-1])
        if "01.flac" in target:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "02.flac" in target:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "03.flac" in target:
            Res.stdout = "TRACKNUMBER=99\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 3)
    assert not valid
    assert any("Duplicate" in e for e in errors)
    assert any("out of bounds" in e for e in errors)


def test_partial_rip_candidate_verification(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "02.flac").touch()
    (tmp_path / "04.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            returncode = 0
            stdout = ""
        last_arg = str(cmd[-1])
        if "01.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "02.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=02\n"
        elif "04.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=04\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.validation.integrity.verify_flac_integrity", lambda files: True)

    valid_full, _ = validate_track_sequence(tmp_path, expected_count=4)
    assert not valid_full

    valid_partial, errors_partial = validate_track_sequence(
        tmp_path,
        expected_count=3,
        total_tracks=4,
        allow_partial=True,
    )
    assert valid_partial
    assert len(errors_partial) == 0

    ok, errors = verify_release_candidate(
        tmp_path,
        expected_count=3,
        total_tracks=4,
        allow_partial=True,
    )
    assert ok
    assert len(errors) == 0


def test_engine_prompt_insert_disc_retry_does_not_eject(tmp_path: Path, monkeypatch):
    ejected = []
    closed = []
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.get_drives_with_hardware_ids", lambda: [("/dev/sr0", "TEST_DRIVE")])
    monkeypatch.setattr("whipit.ripping.engine.get_drive_hardware_id", lambda dev: "TEST_DRIVE")

    class MockAdapter(NullInteractionAdapter):
        def prompt_insert_disc(self, dev, hw, **kw):
            return DiscPromptResult(DiscPromptAction.RETRY)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=MockAdapter())
    runner = WhipperRunner(tmp_path, "/dev/sr0")

    should_cont, offset = engine._handle_insert_disc_prompt(runner, effective_offset=6)
    assert should_cont is True
    assert len(ejected) == 0
    assert len(closed) == 1


def test_engine_prompt_insert_disc_eject_ejects_tray(tmp_path: Path, monkeypatch):
    ejected = []
    closed = []
    reloaded = []
    monkeypatch.setattr("whipit.ripping.engine.eject_drive", lambda dev: ejected.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.close_tray", lambda dev: closed.append(dev))
    monkeypatch.setattr("whipit.ripping.engine.get_drives_with_hardware_ids", lambda: [("/dev/sr0", "TEST_DRIVE")])
    monkeypatch.setattr("whipit.ripping.engine.get_drive_hardware_id", lambda dev: "TEST_DRIVE")

    class MockAdapter(NullInteractionAdapter):
        def prompt_insert_disc(self, dev, hw, **kw):
            return DiscPromptResult(DiscPromptAction.EJECT)
        def wait_for_user_reload(self, dev, msg):
            reloaded.append(dev)
            return True

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0", ui=MockAdapter())
    runner = WhipperRunner(tmp_path, "/dev/sr0")

    should_cont, offset = engine._handle_insert_disc_prompt(runner, effective_offset=6)
    assert should_cont is True
    assert len(ejected) == 1
    assert len(reloaded) == 1
    assert len(closed) == 1


def test_resolve_paths_partial_naming(sample_manifest):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    coord.resolve_paths(sample_manifest.metadata, partial=True, timestamp_suffix="")
    flac_dir = coord.manifest.paths.final_flac_dir
    assert flac_dir is not None
    assert flac_dir.parent.name == "Pink Floyd - The Wall (Partial)"

    coord.resolve_paths(sample_manifest.metadata, partial=True, timestamp_suffix="2026-10-20")
    flac_dir_ts = coord.manifest.paths.final_flac_dir
    assert flac_dir_ts is not None
    assert flac_dir_ts.parent.name == "Pink Floyd - The Wall (Partial 2026-10-20)"

    coord.resolve_paths(sample_manifest.metadata, partial=False, timestamp_suffix="")
    flac_dir_full = coord.manifest.paths.final_flac_dir
    assert flac_dir_full is not None
    assert flac_dir_full.parent.name == "Pink Floyd - The Wall"


def test_compare_trees_detects_byte_difference_with_same_mtime(tmp_path: Path):
    src = tmp_path / "source"
    tgt = tmp_path / "target"
    src.mkdir()
    tgt.mkdir()

    f1 = src / "01.flac"
    f2 = tgt / "01.flac"
    f1.write_bytes(b"ORIGINAL_AUDIO_PAYLOAD_12345678")
    f2.write_bytes(b"CORRUPTED_AUDIO_PAYLOAD_12345678")

    t = 1600000000.0
    os.utime(f1, (t, t))
    os.utime(f2, (t, t))

    assert not compare_trees(src, tgt)

    f2.write_bytes(b"ORIGINAL_AUDIO_PAYLOAD_12345678")
    os.utime(f2, (t, t))
    assert compare_trees(src, tgt)


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

    store.release_job_lock(sample_manifest.job_id, lock_fd)
    assert store.is_job_active(sample_manifest.job_id) is False
    reports_after = coordinator.inspect_uncommitted_jobs()
    assert len(reports_after) == 1


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


def test_transaction_rollback_restores_replaced_flac(sample_manifest, temp_env):
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    (final_flac / "current.flac").write_bytes(b"NEW_VERSION")

    bak_flac = final_flac.parent / "backup.flac"
    bak_flac.mkdir(parents=True, exist_ok=True)
    (bak_flac / "original.flac").write_bytes(b"ORIGINAL_VERSION")

    sample_manifest.transaction.flac_backed_up = True
    sample_manifest.transaction.flac_backup_path = bak_flac
    sample_manifest.transaction.flac_published = True

    tx = ArchiveTransactionManager(sample_manifest, store)
    ok = tx.rollback()
    assert ok is True
    assert (final_flac / "original.flac").exists()
    assert not bak_flac.exists()


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
    assert ok is True
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
    assert ok is False
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
    assert ok is False

    assert not stage_path.exists()
    store = ManifestStore()
    assert not store.manifest_path(job_id).exists()
    assert len(store.list_uncommitted_jobs()) == 0


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
    assert ok is False

    assert len(notified) == 0

    store = ManifestStore()
    assert coord.candidate_stage.exists()
    assert (coord.candidate_stage / "flac" / "01.flac").exists()
    assert not coord.whipper_stage.exists()
    assert len(store.list_uncommitted_jobs()) == 1


def test_manual_offset_zero_preserved(sample_manifest, temp_env, monkeypatch):
    monkeypatch.setattr(whipit.workflow, "get_stored_drive_offset", lambda dev: 6)
    coord = RipWorkflowCoordinator(
        device="/dev/sr0",
        manual_offset=0,
    )
    assert coord.manifest.device.read_offset == 0


def test_ui_port_conflict_resolution(sample_manifest, temp_env):
    from whipit.state_store import ManifestStore

    class MockGuiAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            return ConflictChoice.TIMESTAMP_SUFFIX

    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=False,
        ui=MockGuiAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None


def test_caa_cover_fetch_prefers_full_resolution(temp_env, monkeypatch):
    requested_urls = []

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"\xff\xd8\xff\xe0\x00\x10JFIF"

    def mock_urlopen(req, timeout=12.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        requested_urls.append(url)
        return MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    art_path = fetch_cover_art_from_caa("dummy-mbid-12345")
    assert art_path is not None
    assert art_path.exists()
    assert art_path.suffix == ".jpg"
    assert "_front" in art_path.name
    assert requested_urls[0] == "https://coverartarchive.org/release/dummy-mbid-12345/front"


def test_ui_adapter_cover_art_storage(tmp_path):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.adapter import GuiInteractionAdapter

    adapter = GuiInteractionAdapter()
    assert adapter.get_cover_art() is None
    adapter.set_cover_art(tmp_path / "custom.jpg")
    assert adapter.get_cover_art() == tmp_path / "custom.jpg"


def test_ripper_locking_detects_python_whipper(tmp_path):
    proc_root = tmp_path / "proc"
    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}
    target_devs = {"/dev/sr0"}

    fake_whipper = proc_root / "2001"
    fake_whipper.mkdir(parents=True)
    (fake_whipper / "comm").write_text("python3\n")
    (fake_whipper / "cmdline").write_bytes(b"python3\x00/usr/bin/whipper\x00cd\x00-d\x00/dev/sr0\x00rip\x00")

    fake_other_py = proc_root / "2002"
    fake_other_py.mkdir(parents=True)
    (fake_other_py / "comm").write_text("python3\n")
    (fake_other_py / "cmdline").write_bytes(b"python3\x00/usr/bin/someapp\x00-d\x00/dev/sr0\x00")

    assert _is_ripper_holding_device(2001, target_devs, target_binaries, proc_root=proc_root) is True
    assert _is_ripper_holding_device(2002, target_devs, target_binaries, proc_root=proc_root) is False


def test_rollback_cleans_nonempty_destination_targets(sample_manifest, temp_env):
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    (final_flac / "published_track.flac").write_bytes(b"PUBLISHED_AUDIO")

    stage_undo = sample_manifest.paths.candidate_stage / "flac"
    stage_undo.mkdir(parents=True, exist_ok=True)
    (stage_undo / "stale_leftover.flac").write_bytes(b"STALE_STAGE_AUDIO")

    sample_manifest.transaction.flac_published = True
    store.save(sample_manifest)

    tx = ArchiveTransactionManager(sample_manifest, store)
    ok = tx.rollback()
    assert ok is True
    assert not final_flac.exists()
    assert stage_undo.exists()
    assert (stage_undo / "published_track.flac").exists()
    assert not (stage_undo / "stale_leftover.flac").exists()


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
    assert ok is True
    assert coord.manifest.state == WorkflowState.COMMITTED


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
    assert ok is False
    assert coord.manifest.state == WorkflowState.PUBLISH_FAILED
    assert coord.candidate_stage.exists()
    assert (coord.candidate_stage / "flac" / "01.flac").exists()

    coordinator = RecoveryCoordinator(ManifestStore())
    reports = coordinator.inspect_uncommitted_jobs()
    assert len(reports) == 1
    manifest, status_str = reports[0]
    assert "verified FLAC candidate preserved" in status_str
    recovered_ok, actions = coordinator.recover_job(manifest.job_id)
    assert recovered_ok is True
    assert coord.candidate_stage.exists()


def test_main_window_reset_disc_state_clears_cover_art(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        art_file = tmp_path / "disc1.jpg"
        art_file.touch()
        window._current_art_path = art_file
        window._downloaded_art_path = art_file
        window._art_is_user_selected = True
        window.adapter.set_cover_art(art_file)

        window._reset_disc_state(clear_user_art=True)
        assert window._current_art_path is None
        assert window._downloaded_art_path is None
        assert window._art_is_user_selected is False
        assert window.adapter.get_cover_art() is None
    finally:
        window.close()


def test_cover_failed_clears_stale_art(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        art_file = tmp_path / "stale.jpg"
        art_file.touch()
        window._current_art_path = art_file
        window.adapter.set_cover_art(art_file)

        window._on_cover_art_failed("dummy-mbid")
        assert window._current_art_path is None
        assert window.adapter.get_cover_art() is None
    finally:
        window.close()


def test_conflict_resolution_warns_on_accurate_archive_downgrade(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")

    sample_manifest.verification.is_accurate = False
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    captured_warnings = []

    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            captured_warnings.append(warning)
            return ConflictChoice.TIMESTAMP_SUFFIX

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=False,
        ui=MockAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None
    assert len(captured_warnings) == 1
    assert "AccurateRip verified" in (captured_warnings[0] or "")


def test_conflict_resolution_defaults_to_timestamp_suffix(sample_manifest, temp_env):
    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    proceed, eff_overwrite, ts = tx.resolve_destination_conflict(overwrite=False, ui=NullInteractionAdapter())
    assert proceed is False


def test_create_embedded_cover_art_downscales_large_image(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage
    from whipit.audio.metadata import create_embedded_cover_art

    large_scan = tmp_path / "huge_scan.jpg"
    img = QImage(2400, 1800, QImage.Format.Format_RGB32)
    img.fill(QColor("blue"))
    img.save(str(large_scan), "JPEG", 95)

    out_scaled = tmp_path / "embedded.jpg"
    result_path = create_embedded_cover_art(large_scan, out_scaled, max_dimension=1000)
    assert result_path == out_scaled
    assert out_scaled.exists()

    scaled_img = QImage(str(out_scaled))
    assert max(scaled_img.width(), scaled_img.height()) <= 1000


def test_create_embedded_cover_art_preserves_small_image(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage
    from whipit.audio.metadata import create_embedded_cover_art

    small_scan = tmp_path / "small_scan.jpg"
    img = QImage(600, 600, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    img.save(str(small_scan), "JPEG", 85)

    out_scaled = tmp_path / "embedded.jpg"
    result_path = create_embedded_cover_art(small_scan, out_scaled, max_dimension=1000)
    assert result_path == small_scan
    assert not out_scaled.exists()


def test_embed_cover_art_flacs_rejects_oversized_file(tmp_path):
    from whipit.audio.metadata import embed_cover_art_flacs

    fake_huge = tmp_path / "huge.jpg"
    with open(fake_huge, "wb") as f:
        f.truncate(20 * 1024 * 1024)

    ok = embed_cover_art_flacs([tmp_path / "track.flac"], fake_huge)
    assert ok is False


def test_calibration_streaming_silent_hang_cancellation(monkeypatch):
    from whipit.ripping.calibration import _run_whipper_streaming

    class BlockingStdout:
        def readline(self):
            time.sleep(10.0)
            return ""
        def close(self):
            pass

    class HangingProcess:
        def __init__(self, *args, **kwargs):
            self.args = args[0] if args else kwargs.get("args", ["whipper"])
            self.stdin = None
            self.stderr = None
            self.stdout = BlockingStdout()
            self.returncode = None
            self.pid = 88888

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            return ("", "")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", HangingProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)

    cancel_evt = threading.Event()
    cancel_evt.set()

    rc = _run_whipper_streaming(["whipper", "offset", "find"], ui=NullInteractionAdapter(), cancel_event=cancel_evt)
    assert rc == -1


def test_engine_unconfigured_offset_aborts_without_infinite_loop(tmp_path, monkeypatch):
    from whipit.ripping.whipper import WhipperResult

    monkeypatch.setattr("whipit.ripping.engine.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.engine.unmount_cdda_device", lambda *a, **kw: None)

    call_count = 0
    def mock_calibrate(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return CalibrationResult(CalibrationStatus.ABORTED)

    monkeypatch.setattr("whipit.ripping.engine.calibrate_drive", mock_calibrate)

    class MockWhipperRunner:
        def __init__(self, *args, **kwargs):
            self.target_device = "/dev/sr0"
        def cleanup_wrapper(self):
            pass
        def run_rip(self, *args, **kwargs):
            return WhipperResult(
                returncode=1,
                stdout="",
                stderr="offset unconfigured",
                log_file=tmp_path / "whipper.log",
                offset_unconfigured=True,
                is_cdr=False,
                network_error=False,
                metadata_missing=False,
                no_cd=False,
                cancelled=False,
            )

    monkeypatch.setattr("whipit.ripping.engine.WhipperRunner", MockWhipperRunner)

    engine = RippingEngine(cache_dir=tmp_path, stage_dir=tmp_path, device_path="/dev/sr0")
    result = engine.run_extraction()

    assert result.success is False
    assert result.cancelled is True
    assert call_count == 1


def test_gui_prompt_select_release_cancel_returns_none(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QInputDialog
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("", False))

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        releases = [
            ReleaseOption(mbid="mbid-1", title="Release 1"),
            ReleaseOption(mbid="mbid-2", title="Release 2"),
        ]
        assert window._dispatch_modal_prompt("prompt_select_release", releases) is None
    finally:
        window.close()


def test_gui_select_loaded_drive_cancel_returns_none(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QInputDialog
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **kw: ("", False))

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        drives = [("/dev/sr0", "Drive 1"), ("/dev/sr1", "Drive 2")]
        assert window._dispatch_modal_prompt("select_loaded_drive", drives) is None
    finally:
        window.close()


def test_main_window_close_event_gracefully_handles_running_workers(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QThread
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    class SlowWorker(QThread):
        def run(self):
            time.sleep(0.3)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    slow_worker = SlowWorker(parent=None)
    slow_worker.start()
    window.details_worker = slow_worker

    window.close()
    assert not slow_worker.isRunning()


def test_unmanaged_packages_filtered_for_pacman_and_dnf():
    assert "loudgain" in UNMANAGED_PACKAGES["pacman"]
    assert "loudgain" in UNMANAGED_PACKAGES["dnf"]


def test_refresh_action_buttons_disables_start_when_dependencies_missing(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)

    _ = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        monkeypatch.setattr("whipit.ui.gui.main_window.get_missing_dependencies", lambda: ["whipper", "loudgain"])
        window._refresh_action_buttons()
        assert not window.start_btn.isEnabled()
        assert not window.identify_btn.isEnabled()

        monkeypatch.setattr("whipit.ui.gui.main_window.get_missing_dependencies", lambda: [])
        window._refresh_action_buttons()
        assert window.start_btn.isEnabled()
        assert window.identify_btn.isEnabled()
    finally:
        window.close()
