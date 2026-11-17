"""Regression tests for final polish items across notifications, startup logging, and drive speed."""

from __future__ import annotations

from pathlib import Path

import pytest

from whipit.models import WorkflowResult
from whipit.notifications import notify_rip_success
from whipit.services.drive_service import get_drive_max_speed
from whipit.workflow import RipWorkflowCoordinator


def test_notify_rip_success_respects_generate_opus_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("whipit.notifications.send_notification", lambda s, b, **kw: sent.append((s, b)))
    monkeypatch.setattr("whipit.notifications.play_alert", lambda **kw: None)

    notify_rip_success(
        artist="Artist",
        album="Album",
        output_dir=tmp_path,
        track_count=5,
        generate_opus=False,
    )
    assert len(sent) == 1
    assert "(FLAC)" in sent[0][1]
    assert "Opus" not in sent[0][1]


def test_workflow_passes_generate_opus_false_to_notification(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = RipWorkflowCoordinator(device="/dev/sr0", generate_opus=False)
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockEngine:
        def run_extraction(self, **kw: object) -> object:
            return type(
                "R",
                (),
                {
                    "success": True,
                    "cancelled": False,
                    "partial": False,
                    "device_path": "/dev/sr0",
                    "effective_offset": 6,
                    "speed": None,
                },
            )()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()
    (coord.whipper_stage / "01.flac").touch()
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr(
        "whipit.workflow.parse_whipper_report",
        lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1),
    )
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)
    monkeypatch.setattr("whipit.workflow.ArchiveTransactionManager.execute_publication", lambda *a, **kw: None)

    notified: list[dict[str, object]] = []
    monkeypatch.setattr("whipit.workflow.notify_rip_success", lambda **kw: notified.append(kw))

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert len(notified) == 1
    assert notified[0]["generate_opus"] is False


def test_startup_checks_logs_missing_dependencies_to_console(win) -> None:
    win._on_startup_checks_completed(False, ["whipper", "flac"], [], [])
    log_text = win.console_box.toPlainText()
    assert "Missing required system dependencies: whipper, flac" in log_text


def test_get_drive_max_speed_returns_none_for_zero_speed(tmp_path: Path) -> None:
    info_file = tmp_path / "cdrom_info"
    info_file.write_text("drive name:\t\tsr0\ndrive speed:\t\t0\n", encoding="utf-8")
    assert get_drive_max_speed("/dev/sr0", proc_info_path=info_file) is None
