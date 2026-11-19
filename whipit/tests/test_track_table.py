"""Tests for Stage 2 TrackTableWidget micro-indicators and tabular typography."""

from __future__ import annotations

import pytest

from whipit.models import DeviceInfo, JobManifest, PathLayout, TrackInfo, WorkflowState
from whipit.ripping.whipper import (
    AccurateRipSummaryEvent,
    ChecksumMatchEvent,
    DriveStallEvent,
    TrackProgressEvent,
)


def test_track_table_columns_and_monospace_typography() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.widgets.track_table import STATUS_PENDING, TrackTableWidget

    _ = QApplication.instance() or QApplication([])
    table = TrackTableWidget()

    tracks = [
        TrackInfo(number=1, title="One", duration="03:12"),
        TrackInfo(number=2, title="Two", duration="04:05"),
    ]
    table.populate_tracks(tracks, album_artist="Artist")
    assert table.columnCount() == 4
    assert table.item(0, 0).text() == "01"
    assert table.item(0, 1).text() == "One"
    assert table.item(0, 2).text() == "03:12"
    assert table.item(0, 3).text() == STATUS_PENDING
    assert table.item(0, 0).font().family() == "Monospace"
    assert table.item(0, 2).font().family() == "Monospace"
    assert table.item(0, 3).font().family() == "Monospace"

    comp_tracks = [
        TrackInfo(number=1, title="One More Time", artist="Daft Punk", duration="05:20"),
    ]
    table.populate_tracks(comp_tracks, album_artist="Various Artists")
    assert table.columnCount() == 5
    assert table.item(0, 1).text() == "Daft Punk"
    assert table.item(0, 2).text() == "One More Time"
    assert table.item(0, 3).text() == "05:20"
    assert table.item(0, 4).text() == STATUS_PENDING


def test_drive_panel_track_table_live_status_lifecycle(temp_env) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.config import get_cache_dir
    from whipit.ui.gui.drive_panel import DrivePanel
    from whipit.ui.gui.widgets.track_table import (
        STATUS_ACCURIP,
        STATUS_ACTIVE,
        STATUS_CRC_MATCH,
        STATUS_DONE,
        STATUS_RETRY,
        STATUS_SKIPPED,
        STATUS_STALL,
    )

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    tracks = [
        TrackInfo(number=1, title="Track 1", duration="03:00"),
        TrackInfo(number=2, title="Track 2", duration="04:00"),
        TrackInfo(number=3, title="Track 3", duration="05:00"),
    ]
    panel._populate_track_table(tracks)

    panel._on_whipper_event(TrackProgressEvent(current_track=1, total_tracks=3, percent=25.0, phase="Reading"))
    assert panel.track_table.item(0, 3).text() == STATUS_ACTIVE

    panel._on_whipper_event(ChecksumMatchEvent(track=1, matched=True))
    assert panel.track_table.item(0, 3).text() == STATUS_CRC_MATCH

    panel._on_whipper_event(
        TrackProgressEvent(
            current_track=2,
            total_tracks=3,
            percent=40.0,
            phase="Reading",
            try_number=2,
            retry_reason="Checksums do not match",
        )
    )
    assert panel.track_table.item(1, 3).text() == STATUS_RETRY

    panel._on_whipper_event(DriveStallEvent(seconds_stalled=30.0, track_number=3))
    assert panel.track_table.item(2, 3).text() == STATUS_STALL

    panel._on_whipper_event(AccurateRipSummaryEvent(summary="All tracks accurately ripped", is_accurate=True))
    assert panel.track_table.item(0, 3).text() == STATUS_ACCURIP
    assert panel.track_table.item(1, 3).text() == STATUS_ACCURIP

    # Missing track marker overrides skipped tracks on partial validation
    manifest = JobManifest(
        job_id="track-table-test",
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST"),
        paths=PathLayout(
            output_root=temp_env["output"],
            whipper_stage=get_cache_dir() / "stage_track-table-test",
            candidate_stage=temp_env["output"] / ".whipit-stage.track-table-test",
        ),
    )
    manifest.verification.is_accurate = False
    manifest.tracks.missing_tracks = [3]
    panel._on_state_changed(WorkflowState.RIP_VALIDATED, manifest)
    assert panel.track_table.item(2, 3).text() == STATUS_SKIPPED

    # Finalize marks pending/active rows Done when not all_accurate
    manifest.verification.accuraterip_summary = "Track not present in database"
    panel._on_state_changed(WorkflowState.RIP_VALIDATED, manifest)
    panel.track_table.reset_statuses()
    panel._on_whipper_event(TrackProgressEvent(current_track=1, total_tracks=3, percent=100.0, phase="Reading"))
    panel._on_workflow_finished(True, "Complete")
    assert panel.track_table.item(0, 3).text() == STATUS_DONE
