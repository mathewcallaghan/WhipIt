"""Track listing table widget with tabular monospaced typography and live status pills."""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget

from whipit.models import TrackInfo
from whipit.ui.gui.styles import (
    COLOR_ACCENT_AMBER,
    COLOR_ACCENT_CYAN,
    COLOR_ACCENT_GREEN,
    COLOR_ACCENT_RED,
    COLOR_TEXT_MUTED,
)

STATUS_PENDING = "—"
STATUS_ACTIVE = "[ ⟳ ]"
STATUS_RETRY = "[ ⚠ Retry ]"
STATUS_STALL = "[ ⚠ Stall ]"
STATUS_MISMATCH = "[ ⚠ Mismatch ]"
STATUS_CRC_MATCH = "[ ✓ CRC Match ]"
STATUS_ACCURIP = "[ ✓ AccRip ]"
STATUS_DONE = "[ ✓ Done ]"
STATUS_SKIPPED = "[ ✕ Skipped ]"
STATUS_HALTED = "[ ⚠ Halted ]"

_TONE_STYLES: Dict[str, tuple[str, Optional[str]]] = {
    "muted": (COLOR_TEXT_MUTED, None),
    "active": (COLOR_ACCENT_CYAN, "#0E2F3A"),
    "retry": (COLOR_ACCENT_AMBER, "#36250B"),
    "error": (COLOR_ACCENT_RED, "#3B1618"),
    "ok": (COLOR_ACCENT_GREEN, "#143320"),
}


class TrackTableWidget(QTableWidget):
    """QTableWidget displaying track metadata with monospaced numeric columns and live status pills."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mono_font = QFont("Monospace")
        self._mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
        self._mono_font.setPointSize(9)

        self._duration_col: int = 2
        self._status_col: int = 3
        self._track_row_map: Dict[int, int] = {}
        self._retried_tracks: set[int] = set()
        self._stalled_tracks: set[int] = set()

        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["#", "Track Title", "Duration", "Status"])
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.setMinimumHeight(160)

    def clear_tracks(self) -> None:
        self._track_row_map.clear()
        self._retried_tracks.clear()
        self._stalled_tracks.clear()
        self.setRowCount(0)

    def populate_tracks(self, tracks: List[TrackInfo], album_artist: str = "") -> None:
        self._track_row_map.clear()
        self._retried_tracks.clear()
        self._stalled_tracks.clear()

        if not tracks:
            self.setRowCount(0)
            return

        has_artists = any(
            bool(getattr(tr, "artist", None) and tr.artist != album_artist)
            for tr in tracks
        )
        if has_artists:
            self.setColumnCount(5)
            self.setHorizontalHeaderLabels(["#", "Track Artist", "Track Title", "Duration", "Status"])
            self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            self.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            self._duration_col = 3
            self._status_col = 4
        else:
            self.setColumnCount(4)
            self.setHorizontalHeaderLabels(["#", "Track Title", "Duration", "Status"])
            self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            self._duration_col = 2
            self._status_col = 3

        self.setRowCount(len(tracks))
        for row, tr in enumerate(tracks):
            self._track_row_map[tr.number] = row

            item_num = QTableWidgetItem(f"{tr.number:02d}")
            item_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_num.setFont(self._mono_font)

            item_dur = QTableWidgetItem(tr.duration or "")
            item_dur.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_dur.setFont(self._mono_font)

            item_status = QTableWidgetItem(STATUS_PENDING)
            item_status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_status.setFont(self._mono_font)
            item_status.setForeground(QBrush(QColor(COLOR_TEXT_MUTED)))

            if has_artists:
                item_art = QTableWidgetItem(getattr(tr, "artist", None) or "")
                item_title = QTableWidgetItem(tr.title)
                self.setItem(row, 0, item_num)
                self.setItem(row, 1, item_art)
                self.setItem(row, 2, item_title)
                self.setItem(row, 3, item_dur)
                self.setItem(row, 4, item_status)
            else:
                item_title = QTableWidgetItem(tr.title)
                self.setItem(row, 0, item_num)
                self.setItem(row, 1, item_title)
                self.setItem(row, 2, item_dur)
                self.setItem(row, 3, item_status)

    def _row_for_track(self, track_number: Optional[int]) -> Optional[int]:
        if track_number is None or self.rowCount() == 0:
            return None
        if track_number in self._track_row_map:
            row = self._track_row_map[track_number]
            return row if 0 <= row < self.rowCount() else None
        fallback = track_number - 1
        return fallback if 0 <= fallback < self.rowCount() else None

    def _apply_row_status(
        self,
        row: int,
        text: str,
        tone: str = "muted",
        tooltip: Optional[str] = None,
    ) -> None:
        if row < 0 or row >= self.rowCount() or self._status_col >= self.columnCount():
            return
        item = self.item(row, self._status_col)
        if item is None:
            item = QTableWidgetItem()
            self.setItem(row, self._status_col, item)
        fg_hex, bg_hex = _TONE_STYLES.get(tone, _TONE_STYLES["muted"])
        item.setText(text)
        item.setFont(self._mono_font)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QBrush(QColor(fg_hex)))
        if bg_hex:
            item.setBackground(QBrush(QColor(bg_hex)))
        else:
            item.setData(Qt.ItemDataRole.BackgroundRole, None)
        item.setToolTip(tooltip or "")

    def reset_statuses(self) -> None:
        self._retried_tracks.clear()
        self._stalled_tracks.clear()
        for row in range(self.rowCount()):
            self._apply_row_status(row, STATUS_PENDING, tone="muted", tooltip="")

    def mark_track_progress(
        self,
        track_number: int,
        phase: str,
        percent: float,
        try_number: int = 1,
        retry_reason: Optional[str] = None,
    ) -> None:
        row = self._row_for_track(track_number)
        if row is None:
            return
        if try_number > 1:
            self._retried_tracks.add(track_number)
            reason = f": {retry_reason}" if retry_reason else ""
            self._apply_row_status(
                row,
                STATUS_RETRY,
                tone="retry",
                tooltip=f"Retrying track {track_number} (Try {try_number}{reason})",
            )
            return

        item = self.item(row, self._status_col)
        cur_text = item.text() if item else ""
        if cur_text in (STATUS_ACCURIP, STATUS_CRC_MATCH, STATUS_RETRY):
            return
        pct_int = int(max(0.0, min(100.0, percent)))
        self._apply_row_status(
            row,
            STATUS_ACTIVE,
            tone="active",
            tooltip=f"{phase} track {track_number} ({pct_int}%)",
        )

    def mark_track_crc(self, track_number: int, matched: bool) -> None:
        row = self._row_for_track(track_number)
        if row is None:
            return
        if matched:
            note = "Test & Copy CRCs match (after retry)" if track_number in self._retried_tracks else "Test & Copy CRCs match"
            self._apply_row_status(row, STATUS_CRC_MATCH, tone="ok", tooltip=note)
        else:
            self._apply_row_status(
                row,
                STATUS_MISMATCH,
                tone="error",
                tooltip="Test & Copy CRCs did not match",
            )

    def mark_track_stall(self, track_number: Optional[int]) -> None:
        if track_number is None:
            return
        row = self._row_for_track(track_number)
        if row is None:
            return
        self._stalled_tracks.add(track_number)
        self._apply_row_status(
            row,
            STATUS_STALL,
            tone="error",
            tooltip=f"Drive read stalled on track {track_number}",
        )

    def mark_all_accurip(self) -> None:
        for row in range(self.rowCount()):
            item = self.item(row, self._status_col)
            if item and item.text() == STATUS_SKIPPED:
                continue
            self._apply_row_status(
                row,
                STATUS_ACCURIP,
                tone="ok",
                tooltip="Verified accurate against AccurateRip database",
            )

    def mark_missing_tracks(self, missing_tracks: List[int]) -> None:
        for t_num in missing_tracks:
            row = self._row_for_track(t_num)
            if row is not None:
                self._apply_row_status(
                    row,
                    STATUS_SKIPPED,
                    tone="error",
                    tooltip=f"Track {t_num} missing from extracted output",
                )

    def finalize_statuses(self, success: bool, all_accurate: bool = False) -> None:
        for row in range(self.rowCount()):
            item = self.item(row, self._status_col)
            cur = item.text() if item else STATUS_PENDING
            if success:
                if all_accurate and cur != STATUS_SKIPPED:
                    self._apply_row_status(
                        row,
                        STATUS_ACCURIP,
                        tone="ok",
                        tooltip="Verified accurate against AccurateRip database",
                    )
                elif cur in (STATUS_PENDING, STATUS_ACTIVE):
                    self._apply_row_status(row, STATUS_DONE, tone="ok", tooltip="Extraction complete")
            else:
                if cur == STATUS_ACTIVE:
                    self._apply_row_status(row, STATUS_HALTED, tone="retry", tooltip="Extraction halted")
