"""Segmented QPainter sector-health heatmap bar for CD extraction telemetry."""

from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from whipit.ui.gui.styles import (
    COLOR_ACCENT_AMBER,
    COLOR_ACCENT_GREEN,
    COLOR_ACCENT_RED,
    COLOR_BG_RECESS,
    COLOR_BORDER_RACK,
)

SEG_EMPTY = 0
SEG_CLEAN = 1
SEG_RETRY = 2
SEG_STALL = 3

_SEGMENT_COLORS: dict[int, str] = {
    SEG_EMPTY: COLOR_BG_RECESS,
    SEG_CLEAN: COLOR_ACCENT_GREEN,
    SEG_RETRY: COLOR_ACCENT_AMBER,
    SEG_STALL: COLOR_ACCENT_RED,
}


class SectorHealthHeatmap(QWidget):
    """Displays per-sector extraction health across the disc (clean, retried, stalled)."""

    def __init__(self, parent: Optional[QWidget] = None, num_segments: int = 120) -> None:
        super().__init__(parent)
        self._num_segments = max(12, num_segments)
        self._segments: List[int] = [SEG_EMPTY] * self._num_segments
        self._total_tracks: int = 0
        self._last_track: int = 0
        self._last_percent: float = 0.0
        self._last_index: int = 0

        self.setFixedHeight(14)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Sector Health Map: Idle")

    @property
    def segments(self) -> List[int]:
        return list(self._segments)

    def counts(self) -> Tuple[int, int, int, int]:
        empty = sum(1 for s in self._segments if s == SEG_EMPTY)
        clean = sum(1 for s in self._segments if s == SEG_CLEAN)
        retry = sum(1 for s in self._segments if s == SEG_RETRY)
        stall = sum(1 for s in self._segments if s == SEG_STALL)
        return empty, clean, retry, stall

    def reset(self) -> None:
        self._segments = [SEG_EMPTY] * self._num_segments
        self._total_tracks = 0
        self._last_track = 0
        self._last_percent = 0.0
        self._last_index = 0
        self.setToolTip("Sector Health Map: Idle")
        self.update()

    def _segment_index(self, track: int, percent: float, total_tracks: int) -> int:
        if total_tracks <= 0:
            return 0
        clamped_track = max(1, min(total_tracks, track))
        clamped_pct = max(0.0, min(100.0, percent))
        frac = ((clamped_track - 1) + (clamped_pct / 100.0)) / float(total_tracks)
        idx = int(frac * self._num_segments)
        return max(0, min(self._num_segments - 1, idx))

    def _upgrade_segment(self, idx: int, severity: int) -> None:
        if 0 <= idx < self._num_segments and severity > self._segments[idx]:
            self._segments[idx] = severity

    def _refresh_tooltip(self) -> None:
        _, clean, retry, stall = self.counts()
        self.setToolTip(
            f"Sector Health Map: {clean} clean, {retry} retried, {stall} stalled/error segment(s)"
        )

    def record_progress(
        self,
        track: int,
        total_tracks: int,
        percent: float,
        try_number: int = 1,
        phase: str = "Reading",
    ) -> None:
        if total_tracks <= 0:
            return
        self._total_tracks = total_tracks
        start_idx = self._segment_index(track, 0.0, total_tracks)
        cur_idx = self._segment_index(track, percent, total_tracks)

        if self._last_track != track:
            if self._last_track > 0 and track > self._last_track:
                prev_end = self._segment_index(self._last_track, 100.0, total_tracks)
                for idx in range(self._last_index, prev_end + 1):
                    self._upgrade_segment(idx, SEG_CLEAN)
            self._last_index = start_idx

        severity = SEG_RETRY if try_number > 1 else SEG_CLEAN
        fill_start = min(self._last_index, cur_idx) if self._last_track == track else start_idx
        for idx in range(fill_start, cur_idx + 1):
            self._upgrade_segment(idx, severity)

        self._last_track = track
        self._last_percent = max(0.0, min(100.0, percent))
        self._last_index = cur_idx
        self._refresh_tooltip()
        self.update()

    def record_stall(self, track_number: Optional[int] = None) -> None:
        if self._total_tracks > 0:
            target_track = track_number if track_number is not None else (self._last_track or 1)
            pct = self._last_percent if target_track == self._last_track else 0.0
            idx = self._segment_index(target_track, pct, self._total_tracks)
        else:
            idx = 0
        self._upgrade_segment(idx, SEG_STALL)
        self._refresh_tooltip()
        self.update()

    def record_fixup(self, track: Optional[int], severity: str) -> None:
        sev = SEG_STALL if severity == "error" else (SEG_RETRY if severity == "retry" else SEG_CLEAN)
        if self._total_tracks > 0:
            target_track = track if track is not None else (self._last_track or 1)
            pct = self._last_percent if target_track == self._last_track else 0.0
            idx = self._segment_index(target_track, pct, self._total_tracks)
        else:
            idx = 0
        self._upgrade_segment(idx, sev)
        self._refresh_tooltip()
        self.update()

    def mark_complete(self) -> None:
        if self._total_tracks > 0:
            for idx in range(self._num_segments):
                self._upgrade_segment(idx, SEG_CLEAN)
            self._refresh_tooltip()
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            return

        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

            bg_rect = QRectF(0.5, 0.5, max(1.0, w - 1.0), max(1.0, h - 1.0))
            painter.fillRect(bg_rect, QColor(COLOR_BG_RECESS))
            painter.setPen(QPen(QColor(COLOR_BORDER_RACK), 1.0))
            painter.drawRect(bg_rect)

            inner_x = 2.0
            inner_y = 2.0
            inner_w = max(0.0, w - 4.0)
            inner_h = max(0.0, h - 4.0)
            if inner_w <= 0.0 or inner_h <= 0.0:
                return

            cell_step = inner_w / float(self._num_segments)
            gap = 1.0 if cell_step >= 3.0 else 0.0

            painter.setPen(Qt.PenStyle.NoPen)
            for idx, state in enumerate(self._segments):
                if state == SEG_EMPTY:
                    continue
                x0 = inner_x + idx * cell_step
                cw = max(1.0, cell_step - gap)
                color_hex = _SEGMENT_COLORS.get(state, COLOR_BG_RECESS)
                painter.fillRect(QRectF(x0, inner_y, cw, inner_h), QColor(color_hex))
        finally:
            painter.end()
