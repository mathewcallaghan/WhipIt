"""Modal user prompt dialogs for the WhipIt desktop GUI."""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.launcher import get_icon_path
from whipit.models import JobManifest, TrackInfo
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
)


def parse_tracklist_clipboard_text(text: str) -> List[str]:
    """
    Intelligently parses track titles from raw clipboard text copied from
    Wikipedia, Bandcamp, Discogs, Spotify, or text files.
    Strips leading track numbers, trailing durations, and surrounding quotes.
    """
    if not text or not text.strip():
        return []

    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    cleaned_titles: List[str] = []

    re_trailing_duration = re.compile(
        r"""
        (?:\s*[-–—|•/]\s*|\s+)
        (?:[\(\[]\s*)?
        \b(?:\d{1,2}:)?\d{1,2}:\d{2}\b
        (?:\s*[\)\]])?
        \s*$
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    re_leading_index = re.compile(
        r"""
        ^\s*
        (?:
            \[?\s*[A-Za-z]?\d{1,3}(?:/\d{1,3})?\s*\]?[.)\-–—:]*\s+
            |
            (?:\btrack\b|\btr\b)\s*\d{1,3}\s*[:.\-–—]*\s+
        )
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    for line in lines:
        s = re_trailing_duration.sub("", line.strip()).strip()
        s = re_leading_index.sub("", s).strip()
        s = re_trailing_duration.sub("", s).strip()
        s = s.strip("\"'“”‘’`")
        s = re.sub(r"\s+", " ", s).strip()
        if s:
            cleaned_titles.append(s)

    return cleaned_titles

def show_message_dialog(
    parent: Optional[QWidget],
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
) -> QMessageBox.StandardButton:
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(buttons)
    return box.exec()


def prompt_restart_dialog(parent: Optional[QWidget], message: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Update Complete")
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(f"{message}\n\nWould you like to restart WhipIt now to apply the update?")
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def show_about_dialog(parent: Optional[QWidget] = None) -> None:
    icon_path = get_icon_path()
    pixmap_html = ""
    if icon_path.exists():
        pixmap_html = f'<p style="text-align: center;"><img src="{icon_path}" width="64" height="64"></p>'

    about_text = f"""
    {pixmap_html}
    <h2 style="text-align: center; margin-bottom: 4px;">WhipIt v{__version__}</h2>
    <p style="text-align: center; color: #38bdf8; font-weight: bold; margin-top: 0;">
        AccurateRip + MusicBrainz Archival CD Ripper &amp; Opus Transcoder
    </p>
    <hr style="border: 0; border-top: 1px solid #2b3248;">
    <p>&bull; Bit-perfect FLAC master archiving using <b>Whipper</b> &amp; <b>cd-paranoia</b></p>
    <p>&bull; AccurateRip sector checksum verification</p>
    <p>&bull; High-efficiency 192kbps VBR <b>Opus</b> portable mirror encoding</p>
    <p>&bull; EBU R128 loudness normalization via <b>loudgain</b></p>
    <p>&bull; Cover art embedding and replication</p>
    <p>&bull; Atomic candidate staging and transaction rollbacks</p>
    """
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setWindowTitle("About WhipIt")
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(about_text)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def prompt_empty_multi_drives_dialog(
    parent: Optional[QWidget],
    drives_with_hw: List[Tuple[str, str]],
) -> DiscPromptResult:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Insert Audio CD")
    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel("Multiple optical drives detected, but none have a disc loaded:"))

    combo = QComboBox()
    for d, hw in drives_with_hw:
        combo.addItem(f"{d} ({hw})", d)
    layout.addWidget(combo)

    btn_row = QHBoxLayout()
    scan_btn = QPushButton("Scan All Drives")
    eject_btn = QPushButton("Eject Selected Drive")
    cancel_btn = QPushButton("Cancel")
    btn_row.addWidget(scan_btn)
    btn_row.addWidget(eject_btn)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)

    result = DiscPromptResult(DiscPromptAction.ABORT)

    def on_scan() -> None:
        nonlocal result
        result = DiscPromptResult(DiscPromptAction.RETRY)
        dlg.accept()

    def on_eject() -> None:
        nonlocal result
        result = DiscPromptResult(DiscPromptAction.EJECT, selected_device=combo.currentData())
        dlg.accept()

    scan_btn.clicked.connect(on_scan)
    eject_btn.clicked.connect(on_eject)
    cancel_btn.clicked.connect(dlg.reject)

    dlg.exec()
    return result


def prompt_insert_disc_dialog(
    parent: Optional[QWidget],
    device: str,
    hw_id: str,
    available_drives: Optional[List[Tuple[str, str]]],
    allow_main_menu: bool,
) -> DiscPromptResult:
    dlg = QDialog(parent)
    dlg.setWindowTitle("No Audio CD Detected")
    layout = QVBoxLayout(dlg)
    lbl = QLabel(f"No Audio CD detected in <b>{html.escape(device)}</b> ({html.escape(hw_id)}).")
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setWordWrap(True)
    layout.addWidget(lbl)

    drive_combo: Optional[QComboBox] = None
    if available_drives and len(available_drives) > 1:
        layout.addWidget(QLabel("Or switch to another optical drive:"))
        drive_combo = QComboBox()
        for d, hw in available_drives:
            drive_combo.addItem(f"{d} ({hw})", d)
        layout.addWidget(drive_combo)

    btn_row = QHBoxLayout()
    retry_btn = QPushButton("Retry Drive")
    eject_btn = QPushButton("Eject Tray")
    btn_row.addWidget(retry_btn)
    btn_row.addWidget(eject_btn)

    switch_btn: Optional[QPushButton] = None
    if drive_combo is not None:
        switch_btn = QPushButton("Switch Drive")
        btn_row.addWidget(switch_btn)

    menu_btn: Optional[QPushButton] = None
    if allow_main_menu:
        menu_btn = QPushButton("Back")
        btn_row.addWidget(menu_btn)

    abort_btn = QPushButton("Abort")
    btn_row.addWidget(abort_btn)
    layout.addLayout(btn_row)

    result = DiscPromptResult(DiscPromptAction.ABORT)

    def set_and_close(res: DiscPromptResult) -> None:
        nonlocal result
        result = res
        dlg.accept()

    retry_btn.clicked.connect(lambda: set_and_close(DiscPromptResult(DiscPromptAction.RETRY)))
    eject_btn.clicked.connect(lambda: set_and_close(DiscPromptResult(DiscPromptAction.EJECT)))
    if switch_btn and drive_combo:
        switch_btn.clicked.connect(
            lambda: set_and_close(
                DiscPromptResult(DiscPromptAction.SWITCH_DRIVE, selected_device=drive_combo.currentData())
            )
        )
    if menu_btn:
        menu_btn.clicked.connect(lambda: set_and_close(DiscPromptResult(DiscPromptAction.MAIN_MENU)))
    abort_btn.clicked.connect(dlg.reject)

    dlg.exec()
    return result


def prompt_calibration_start_dialog(parent: Optional[QWidget], device: str) -> CalibrationChoice:
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Drive Calibration Wizard ({device})")
    layout = QVBoxLayout(dlg)
    lbl = QLabel(
        f"<b>Calibrating optical drive: {html.escape(device)}</b><br><br>"
        "To verify AccurateRip checksums, Whipper needs your drive's sample read offset.<br>"
        "Insert a popular commercial Audio CD to auto-detect, or enter a known offset below:"
    )
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setWordWrap(True)
    layout.addWidget(lbl)

    rb_auto = QRadioButton("Auto-detect offset using loaded Audio CD")
    rb_auto.setChecked(True)
    rb_manual = QRadioButton("Enter read offset manually:")
    spin = QSpinBox()
    spin.setRange(-10000, 10000)
    spin.setValue(6)
    spin.setEnabled(False)
    rb_manual.toggled.connect(spin.setEnabled)

    layout.addWidget(rb_auto)
    row = QHBoxLayout()
    row.addWidget(rb_manual)
    row.addWidget(spin)
    layout.addLayout(row)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_manual.isChecked():
            return CalibrationChoice(CalibrationAction.MANUAL_OFFSET, manual_offset=spin.value())
        return CalibrationChoice(CalibrationAction.AUTO_DETECT)
    return CalibrationChoice(CalibrationAction.ABORT)


def prompt_calibration_retry_dialog(parent: Optional[QWidget], device: str) -> CalibrationChoice:
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Offset Detection Failed ({device})")
    layout = QVBoxLayout(dlg)
    retry_lbl = QLabel(
        "AccurateRip offset auto-detection failed (disc not matched in database).\n"
        "How would you like to proceed?"
    )
    retry_lbl.setWordWrap(True)
    layout.addWidget(retry_lbl)

    rb_manual = QRadioButton("Enter offset manually:")
    rb_manual.setChecked(True)
    spin = QSpinBox()
    spin.setRange(-10000, 10000)
    spin.setValue(0)
    rb_manual.toggled.connect(spin.setEnabled)

    rb_swap = QRadioButton("Eject and swap for another commercial Audio CD")
    rb_zero = QRadioButton("Use 0 as temporary fallback for this session")
    rb_skip = QRadioButton("Skip calibration for now")

    row = QHBoxLayout()
    row.addWidget(rb_manual)
    row.addWidget(spin)
    layout.addLayout(row)
    layout.addWidget(rb_swap)
    layout.addWidget(rb_zero)
    layout.addWidget(rb_skip)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_manual.isChecked():
            return CalibrationChoice(CalibrationAction.MANUAL_OFFSET, manual_offset=spin.value())
        if rb_swap.isChecked():
            return CalibrationChoice(CalibrationAction.SWAP_DISC)
        if rb_zero.isChecked():
            return CalibrationChoice(CalibrationAction.USE_ZERO)
        return CalibrationChoice(CalibrationAction.SKIP)
    return CalibrationChoice(CalibrationAction.ABORT)


def prompt_extraction_error_dialog(
    parent: Optional[QWidget],
    device: str,
    available_drives: List[Tuple[str, str]],
) -> ExtractionErrorChoice:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Extraction Halted — Read Error Recovery")
    layout = QVBoxLayout(dlg)
    lbl = QLabel(
        f"<b>Extraction on {html.escape(device)} halted due to read errors or scratched sectors.</b><br>"
        "Choose a recovery strategy:"
    )
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setWordWrap(True)
    layout.addWidget(lbl)

    rb_4x = QRadioButton("Retry album at slow speed (4x)")
    rb_4x.setChecked(True)
    rb_8x = QRadioButton("Retry album at moderate speed (8x)")
    rb_eject = QRadioButton("Eject disc to inspect/clean, then retry")
    rb_switch = QRadioButton("Switch to another optical drive:")
    drive_combo = QComboBox()
    for d, hw in available_drives:
        drive_combo.addItem(f"{d} ({hw})", d)
    drive_combo.setEnabled(False)
    rb_switch.toggled.connect(drive_combo.setEnabled)

    rb_keep = QRadioButton("Rescue all remaining readable tracks (--keep-going)")
    rb_partial = QRadioButton("Accept partial rip (keep already-extracted tracks)")
    rb_abort = QRadioButton("Abort rip")

    layout.addWidget(rb_4x)
    layout.addWidget(rb_8x)
    layout.addWidget(rb_eject)
    sw_row = QHBoxLayout()
    sw_row.addWidget(rb_switch)
    sw_row.addWidget(drive_combo)
    layout.addLayout(sw_row)
    layout.addWidget(rb_keep)
    layout.addWidget(rb_partial)
    layout.addWidget(rb_abort)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_4x.isChecked():
            return ExtractionErrorChoice(ExtractionErrorAction.RETRY_SLOW_4X)
        if rb_8x.isChecked():
            return ExtractionErrorChoice(ExtractionErrorAction.RETRY_MODERATE_8X)
        if rb_eject.isChecked():
            return ExtractionErrorChoice(ExtractionErrorAction.EJECT_AND_RETRY)
        if rb_switch.isChecked():
            return ExtractionErrorChoice(
                ExtractionErrorAction.SWITCH_DRIVE,
                selected_device=drive_combo.currentData() or device,
            )
        if rb_keep.isChecked():
            return ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING)
        if rb_partial.isChecked():
            return ExtractionErrorChoice(ExtractionErrorAction.ACCEPT_PARTIAL)
    return ExtractionErrorChoice(ExtractionErrorAction.ABORT)


def prompt_conflict_resolution_dialog(
    parent: Optional[QWidget],
    flac_dir: Optional[Path],
    opus_dir: Optional[Path],
    warning: Optional[str] = None,
) -> ConflictChoice:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Destination Directory Already Exists")
    layout = QVBoxLayout(dlg)

    paths_info = ""
    if flac_dir and flac_dir.exists():
        paths_info += f"• FLAC: {flac_dir}\n"
    if opus_dir and opus_dir.exists():
        paths_info += f"• Opus: {opus_dir}\n"

    lbl = QLabel(
        f"The destination archive folder already exists:\n\n{paths_info}\n"
        "How would you like to proceed?"
    )
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setWordWrap(True)
    layout.addWidget(lbl)

    if warning:
        warn_lbl = QLabel(warning)
        warn_lbl.setTextFormat(Qt.TextFormat.PlainText)
        warn_lbl.setStyleSheet(
            "color: #fecaca; background-color: #7f1d1d; border: 1px solid #ef4444; "
            "border-radius: 5px; padding: 8px; font-weight: bold;"
        )
        warn_lbl.setWordWrap(True)
        layout.addWidget(warn_lbl)

    rb_suffix = QRadioButton("Save alongside existing folder with a timestamp suffix (Recommended)")
    rb_suffix.setChecked(True)
    rb_overwrite = QRadioButton("Overwrite existing (safely backs up previous rip during transaction)")
    rb_abort = QRadioButton("Abort publication")

    layout.addWidget(rb_suffix)
    layout.addWidget(rb_overwrite)
    layout.addWidget(rb_abort)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_overwrite.isChecked():
            return ConflictChoice.OVERWRITE
        if rb_suffix.isChecked():
            return ConflictChoice.TIMESTAMP_SUFFIX
    return ConflictChoice.ABORT


def prompt_recover_job_dialog(
    parent: Optional[QWidget],
    manifest: JobManifest,
    status: str,
) -> str:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Recover Interrupted CD Rip")
    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    header = QLabel("<h3>Interrupted CD Rip Detected</h3>")
    layout.addWidget(header)

    stage_flac = manifest.paths.candidate_stage / "flac"
    flac_count = len(list(stage_flac.glob("*.flac"))) if stage_flac.exists() else 0
    if flac_count == 0 and manifest.paths.whipper_stage.exists():
        flac_count = len(list(manifest.paths.whipper_stage.rglob("*.flac")))

    info_text = (
        f"<b>Album:</b> {html.escape(manifest.metadata.album_stem)}<br>"
        f"<b>Status:</b> {html.escape(status)}<br>"
        f"<b>Extracted Audio:</b> {flac_count} FLAC track(s)<br>"
        f"<b>Staging Path:</b> {html.escape(str(manifest.paths.candidate_stage))}"
    )
    info_lbl = QLabel(info_text)
    info_lbl.setWordWrap(True)
    layout.addWidget(info_lbl)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)

    resume_btn = QPushButton("Resume Publish")
    resume_btn.setObjectName("primaryButton")
    discard_btn = QPushButton("Discard")
    discard_btn.setObjectName("dangerButton")
    skip_btn = QPushButton("Keep for Later")

    btn_row.addWidget(resume_btn)
    btn_row.addWidget(discard_btn)
    btn_row.addWidget(skip_btn)
    layout.addLayout(btn_row)

    choice = "skip"

    def on_resume():
        nonlocal choice
        choice = "resume"
        dlg.accept()

    def on_discard():
        nonlocal choice
        ans = QMessageBox.question(
            dlg,
            "Confirm Discard",
            f"Are you sure you want to permanently discard the extracted tracks for '{manifest.metadata.album_stem}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            choice = "discard"
            dlg.accept()

    resume_btn.clicked.connect(on_resume)
    discard_btn.clicked.connect(on_discard)
    skip_btn.clicked.connect(dlg.reject)

    dlg.exec()
    return choice


class EditMetadataDialog(QDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        artist: str = "",
        album: str = "",
        year: Optional[str] = None,
        genre: Optional[str] = None,
        tracks: Optional[List[TrackInfo]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Album & Track Details")
        self.setMinimumSize(640, 520)
        self.resize(720, 580)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Header
        header_lbl = QLabel("<h3>Edit Album &amp; Track Metadata</h3>")
        header_lbl.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header_lbl)

        sub_lbl = QLabel(
            "Customize artist, album, and track names. These tags will be written to both the "
            "FLAC archival master and the Opus portable mirror."
        )
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(sub_lbl)

        # Album Info Group
        album_group = QGroupBox("Album Information")
        album_layout = QVBoxLayout(album_group)
        album_layout.setSpacing(6)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Artist:"))
        self.artist_edit = QLineEdit(artist if artist != "Unknown Artist" else "")
        self.artist_edit.setPlaceholderText("Artist Name")
        row1.addWidget(self.artist_edit, stretch=2)

        row1.addWidget(QLabel("Album Title:"))
        self.album_edit = QLineEdit(album if album != "Unknown Album" else "")
        self.album_edit.setPlaceholderText("Album Title")
        row1.addWidget(self.album_edit, stretch=3)
        album_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Year / Date:"))
        self.year_edit = QLineEdit(year or "")
        self.year_edit.setPlaceholderText("e.g. 2007")
        self.year_edit.setFixedWidth(110)
        row2.addWidget(self.year_edit)

        row2.addSpacing(16)
        row2.addWidget(QLabel("Genre:"))
        self.genre_edit = QLineEdit(genre or "")
        self.genre_edit.setPlaceholderText("e.g. Alternative Rock")
        row2.addWidget(self.genre_edit, stretch=1)
        album_layout.addLayout(row2)

        layout.addWidget(album_group)

        # Track Table Group
        track_group = QGroupBox("Track Titles")
        track_layout = QVBoxLayout(track_group)
        track_layout.setSpacing(6)

        btn_row = QHBoxLayout()
        self.paste_btn = QPushButton("📋 Paste Tracklist from Clipboard")
        self.paste_btn.setToolTip("Parses copied tracklist text from Wikipedia, Bandcamp, or Discogs")
        self.paste_btn.clicked.connect(self._on_paste_clipboard)
        btn_row.addWidget(self.paste_btn)

        self.clear_btn = QPushButton("Clear Titles")
        self.clear_btn.clicked.connect(self._on_clear_titles)
        btn_row.addWidget(self.clear_btn)

        btn_row.addStretch(1)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 11px;")
        btn_row.addWidget(self.status_lbl)
        track_layout.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["#", "Track Title", "Duration"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        raw_tracks = tracks or []
        self.table.setRowCount(len(raw_tracks))
        for row, tr in enumerate(raw_tracks):
            num_item = QTableWidgetItem(f"{tr.number:02d}")
            num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            num_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            title_item = QTableWidgetItem(tr.title)
            title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)

            dur_item = QTableWidgetItem(tr.duration or "")
            dur_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dur_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            self.table.setItem(row, 0, num_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, dur_item)

        track_layout.addWidget(self.table)
        layout.addWidget(track_group, stretch=1)

        # Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_paste_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard else ""
        titles = parse_tracklist_clipboard_text(text)
        if not titles:
            QMessageBox.information(
                self,
                "No Titles Found",
                "Could not detect track titles in your clipboard.\n\n"
                "Copy lines of track titles from Wikipedia, Bandcamp, or Discogs, then click this button.",
            )
            return

        applied = 0
        for row in range(min(len(titles), self.table.rowCount())):
            item = self.table.item(row, 1)
            if item:
                item.setText(titles[row])
                applied += 1

        self.status_lbl.setText(f"✓ Pasted {applied} track title(s) from clipboard")

    def _on_clear_titles(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item:
                item.setText(f"Track {row + 1}")
        self.status_lbl.setText("Titles reset to generic")

    def get_metadata(self) -> Tuple[str, str, Optional[str], Optional[str], List[TrackInfo]]:
        art = self.artist_edit.text().strip() or "Unknown Artist"
        alb = self.album_edit.text().strip() or "Unknown Album"
        yr = self.year_edit.text().strip() or None
        gnr = self.genre_edit.text().strip() or None

        updated_tracks: List[TrackInfo] = []
        for row in range(self.table.rowCount()):
            num_item = self.table.item(row, 0)
            title_item = self.table.item(row, 1)
            dur_item = self.table.item(row, 2)
            t_num = int(num_item.text()) if num_item and num_item.text().isdigit() else (row + 1)
            t_title = title_item.text().strip() if title_item and title_item.text().strip() else f"Track {t_num}"
            t_dur = dur_item.text().strip() if dur_item and dur_item.text().strip() else None
            updated_tracks.append(TrackInfo(number=t_num, title=t_title, duration=t_dur))

        return art, alb, yr, gnr, updated_tracks


def prompt_edit_metadata_dialog(
    parent: Optional[QWidget],
    artist: str,
    album: str,
    year: Optional[str] = None,
    genre: Optional[str] = None,
    tracks: Optional[List[TrackInfo]] = None,
) -> Optional[Tuple[str, str, Optional[str], Optional[str], List[TrackInfo]]]:
    dlg = EditMetadataDialog(parent, artist=artist, album=album, year=year, genre=genre, tracks=tracks)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.get_metadata()
    return None
