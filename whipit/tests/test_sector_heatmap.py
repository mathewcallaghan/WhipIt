"""Tests for Stage 1 SectorHealthHeatmap, Throughput HUD, and cd-paranoia tick parsing."""

from __future__ import annotations

import pytest

from whipit.ripping.whipper import (
    DriveStallEvent,
    SectorFixupEvent,
    TrackProgressEvent,
    WhipperStreamParser,
)


def test_stream_parser_parses_paranoia_fixup_and_error_ticks() -> None:
    parser = WhipperStreamParser()
    parser.current_track = 4

    assert parser.parse_line("##: 0 - - - 0 @ 0000\n") is None
    assert parser.parse_line("##: 1 [verify] @ 1234\n") is None

    ev_retry = parser.parse_line("##: 2 [fixup edge] @ 1500\n")
    assert isinstance(ev_retry, SectorFixupEvent)
    assert ev_retry.track == 4
    assert ev_retry.severity == "retry"
    assert ev_retry.code == 2
    assert ev_retry.sector == 1500

    ev_err = parser.parse_line("##: 5 [repair] @ 2200\n")
    assert isinstance(ev_err, SectorFixupEvent)
    assert ev_err.track == 4
    assert ev_err.severity == "error"
    assert ev_err.code == 5
    assert ev_err.sector == 2200


def test_sector_heatmap_monotonic_severity_and_reset() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.widgets.sector_heatmap import (
        SEG_CLEAN,
        SEG_EMPTY,
        SEG_RETRY,
        SEG_STALL,
        SectorHealthHeatmap,
    )

    _ = QApplication.instance() or QApplication([])
    hm = SectorHealthHeatmap(num_segments=20)
    hm.resize(240, 14)

    hm.record_progress(track=1, total_tracks=2, percent=50.0, try_number=1, phase="Reading")
    assert hm.segments[0] == SEG_CLEAN

    hm.record_progress(track=2, total_tracks=2, percent=25.0, try_number=2, phase="Reading")
    assert SEG_RETRY in hm.segments

    hm.record_stall(track_number=2)
    assert SEG_STALL in hm.segments

    # Subsequent Verifying pass must not downgrade retry/stall markers
    hm.record_progress(track=2, total_tracks=2, percent=100.0, try_number=1, phase="Verifying")
    assert SEG_RETRY in hm.segments
    assert SEG_STALL in hm.segments

    hm.mark_complete()
    assert SEG_EMPTY not in hm.segments
    assert SEG_STALL in hm.segments

    # Offscreen paintEvent must render cleanly including zero-size guard
    hm.repaint()
    hm.resize(0, 0)
    hm.repaint()

    hm.reset()
    assert all(s == SEG_EMPTY for s in hm.segments)


def test_sector_heatmap_handles_stall_before_tracks_start() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.widgets.sector_heatmap import SEG_STALL, SectorHealthHeatmap

    _ = QApplication.instance() or QApplication([])
    hm = SectorHealthHeatmap(num_segments=20)
    hm.record_stall(track_number=None)
    assert hm.segments[0] == SEG_STALL


def test_drive_panel_hud_updates_and_deduplicates_retries_and_stalls() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel
    from whipit.ui.gui.widgets.sector_heatmap import SEG_EMPTY, SEG_RETRY, SEG_STALL

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")

    assert "Speed: --" in panel.hud_stats_lbl.text()
    assert "Throughput: --" in panel.hud_stats_lbl.text()
    assert "Retries: 0" in panel.hud_stats_lbl.text()
    assert "Stalls: 0" in panel.hud_stats_lbl.text()

    panel._on_whipper_event(
        TrackProgressEvent(current_track=1, total_tracks=4, percent=50.0, phase="Reading", speed_x=10.0)
    )
    assert "Speed: 10.0x" in panel.hud_stats_lbl.text()
    assert "Throughput: 1.76 MB/s" in panel.hud_stats_lbl.text()
    assert "Retries: 0" in panel.hud_stats_lbl.text()

    # Multiple ticks for the same (track=2, try=2) count as 1 retry
    panel._on_whipper_event(
        TrackProgressEvent(current_track=2, total_tracks=4, percent=10.0, phase="Reading", try_number=2, speed_x=4.0)
    )
    panel._on_whipper_event(
        TrackProgressEvent(current_track=2, total_tracks=4, percent=60.0, phase="Reading", try_number=2, speed_x=4.0)
    )
    assert "Retries: 1" in panel.hud_stats_lbl.text()
    assert SEG_RETRY in panel.sector_heatmap.segments

    # Consecutive stall events on same track count as 1 stall episode
    panel._on_whipper_event(DriveStallEvent(seconds_stalled=30.0, track_number=2))
    panel._on_whipper_event(DriveStallEvent(seconds_stalled=45.0, track_number=2))
    assert "Stalls: 1" in panel.hud_stats_lbl.text()
    assert "Speed: --" in panel.hud_stats_lbl.text()
    assert SEG_STALL in panel.sector_heatmap.segments

    # Fixup tick marks error segment cleanly
    panel._on_whipper_event(SectorFixupEvent(track=3, severity="error", code=5, sector=100))
    assert SEG_STALL in panel.sector_heatmap.segments

    # Completion fills remaining segments and resets live speed to --
    panel._on_workflow_finished(True, "Complete")
    assert SEG_EMPTY not in panel.sector_heatmap.segments
    assert "Speed: --" in panel.hud_stats_lbl.text()
    assert "Retries: 1" in panel.hud_stats_lbl.text()

    # Reset restores baseline HUD and heatmap
    panel._reset_telemetry_hud()
    assert "Retries: 0" in panel.hud_stats_lbl.text()
    assert "Stalls: 0" in panel.hud_stats_lbl.text()
    assert all(s == SEG_EMPTY for s in panel.sector_heatmap.segments)
