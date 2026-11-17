"""Regression tests verifying .cue, .log, and .toc sidecar filenames are sanitized for cross-platform/SMB safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from whipit.models import WorkflowResult
from whipit.services.stage_metadata_service import StageMetadataService
from whipit.workflow import RipWorkflowCoordinator


def test_normalize_sidecar_filenames_converts_colons_and_quotes(tmp_path: Path) -> None:
    stage_flac = tmp_path / "flac"
    stage_flac.mkdir()

    cue = stage_flac / "Pink Floyd - Pulse: Live in Europe.cue"
    log = stage_flac / "Pink Floyd - Pulse: Live in Europe.log"
    toc = stage_flac / 'Pink Floyd - "Pulse": Live in Europe.toc'
    cue.write_text("CUE_DATA", encoding="utf-8")
    log.write_text("LOG_DATA", encoding="utf-8")
    toc.write_text("TOC_DATA", encoding="utf-8")

    StageMetadataService().normalize_sidecar_filenames(stage_flac)

    filenames = sorted(p.name for p in stage_flac.iterdir())
    assert "Pink Floyd - Pulse - Live in Europe.cue" in filenames
    assert "Pink Floyd - Pulse - Live in Europe.log" in filenames
    assert "Pink Floyd - 'Pulse' - Live in Europe.toc" in filenames
    assert not any(":" in name or '"' in name for name in filenames)


def test_normalize_sidecar_filenames_preserves_clean_names(tmp_path: Path) -> None:
    stage_flac = tmp_path / "flac"
    stage_flac.mkdir()

    clean_cue = stage_flac / "album.cue"
    clean_log = stage_flac / "album.log"
    clean_cue.write_text("CUE_DATA", encoding="utf-8")
    clean_log.write_text("LOG_DATA", encoding="utf-8")

    StageMetadataService().normalize_sidecar_filenames(stage_flac)

    assert (stage_flac / "album.cue").exists()
    assert (stage_flac / "album.log").exists()


def test_workflow_publishes_sanitized_cue_and_log_filenames(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = RipWorkflowCoordinator(device="/dev/sr0")
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

    colon_cue = coord.whipper_stage / "Artist - Album: Live.cue"
    colon_log = coord.whipper_stage / "Artist - Album: Live.log"
    colon_cue.write_text(
        'FILE "01. Artist - Track.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n',
        encoding="utf-8",
    )
    colon_log.write_text(
        "Health status: No errors occurred\nAccurateRip summary: All tracks accurately ripped\n",
        encoding="utf-8",
    )
    (coord.whipper_stage / "01.flac").touch()

    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: next(d.rglob("*.cue"), None))
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: next((p for p in d.rglob("*.log") if p.name != "whipper_exec.log"), None))
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr(
        "whipit.workflow.parse_whipper_report",
        lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1),
    )
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS

    final_dir = coord.manifest.paths.final_flac_dir
    assert final_dir is not None
    published_files = [p.name for p in final_dir.iterdir()]
    assert "Artist - Album - Live.cue" in published_files
    assert "Artist - Album - Live.log" in published_files
    assert not any(":" in name for name in published_files)
