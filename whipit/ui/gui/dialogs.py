"""Modal user prompt dialogs for the WhipIt desktop GUI."""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.config import UserPreferences, load_preferences, save_preferences
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
from whipit.ui.gui.styles import (
    LED_PILL_GREEN,
    LED_PILL_UNCATALOGED,
    STYLE_CONFLICT_WARNING,
    STYLE_STATUS_INFO,
    STYLE_SUBTITLE_MUTED,
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
    other_drives = [(d, hw) for d, hw in (available_drives or []) if d != device]
    if other_drives:
        layout.addWidget(QLabel("Or switch to another optical drive:"))
        drive_combo = QComboBox()
        for d, hw in other_drives:
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
    """Categorized wizard dialog for launching AccurateRip offset auto-calibration."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Drive Calibration Wizard — {device}")
    dlg.setMinimumWidth(560)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    header_card = QFrame()
    header_card.setObjectName("readErrorHeaderAlert")
    card_layout = QVBoxLayout(header_card)
    card_layout.setContentsMargins(12, 10, 12, 10)

    title_lbl = QLabel(
        "<span style='color: #00E5FF; font-weight: bold; font-size: 14px;'>"
        f"🎯 AccurateRip Read Offset Calibration ({html.escape(device)})</span>"
    )
    title_lbl.setTextFormat(Qt.TextFormat.RichText)
    body_lbl = QLabel(
        "To verify bit-perfect extractions, Whipper requires your drive's hardware sample read offset.<br>"
        "Whipper will read the disc TOC, query AccurateRip, and test sample boundaries across audio tracks."
    )
    body_lbl.setTextFormat(Qt.TextFormat.RichText)
    body_lbl.setWordWrap(True)
    card_layout.addWidget(title_lbl)
    card_layout.addWidget(body_lbl)

    info_lbl = QLabel("Note: Auto-detection takes 1–3 minutes testing sample offsets against online checksums.")
    info_lbl.setObjectName("diagnosticDetailText")
    info_lbl.setWordWrap(True)
    card_layout.addWidget(info_lbl)
    layout.addWidget(header_card)

    opt_group = QGroupBox("Calibration Strategy")
    opt_layout = QVBoxLayout(opt_group)
    opt_layout.setSpacing(8)

    rb_auto = QRadioButton("Auto-detect offset using inserted Audio CD (Recommended)")
    rb_auto.setChecked(True)

    rb_manual = QRadioButton("Enter sample read offset manually:")
    spin = QSpinBox()
    spin.setRange(-10000, 10000)
    spin.setValue(6)
    spin.setFixedWidth(100)
    spin.setEnabled(False)

    mono_font = QFont("Monospace")
    mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
    mono_font.setPointSize(9)
    spin.setFont(mono_font)
    rb_manual.toggled.connect(spin.setEnabled)

    opt_layout.addWidget(rb_auto)
    manual_row = QHBoxLayout()
    manual_row.addWidget(rb_manual)
    manual_row.addWidget(spin)
    manual_row.addStretch(1)
    opt_layout.addLayout(manual_row)
    layout.addWidget(opt_group)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn is not None:
        ok_btn.setText("Start Calibration")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_manual.isChecked():
            return CalibrationChoice(CalibrationAction.MANUAL_OFFSET, manual_offset=spin.value())
        return CalibrationChoice(CalibrationAction.AUTO_DETECT)
    return CalibrationChoice(CalibrationAction.ABORT)


def prompt_wait_for_reload_dialog(parent: Optional[QWidget], device: str, message: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(f"Disc Reload Required ({device})")
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(message.strip() or f"Please insert an Audio CD into {device} and click Continue.")
    cont_btn = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Abort", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cont_btn)
    box.exec()
    return box.clickedButton() is cont_btn


def prompt_yes_no_dialog(parent: Optional[QWidget], title: str, text: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.WindowModal)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def prompt_select_loaded_drive_dialog(
    parent: Optional[QWidget],
    drives_with_hw: List[Tuple[str, str]],
) -> Optional[str]:
    if not drives_with_hw:
        return None
    items = [f"{d} ({hw})" for d, hw in drives_with_hw]
    choice, ok = QInputDialog.getItem(
        parent,
        "Select Optical Drive",
        "Multiple drives have Audio CDs loaded.\nSelect which drive to rip:",
        items,
        0,
        False,
    )
    if ok and choice in items:
        return drives_with_hw[items.index(choice)][0]
    return None


def prompt_extraction_speed_dialog(parent: Optional[QWidget]) -> Optional[int]:
    options: List[Tuple[str, Optional[int]]] = [
        ("Full Speed (Normal)", None),
        ("4x (Safe / Scratched)", 4),
        ("8x (Moderate)", 8),
        ("12x", 12),
        ("16x", 16),
        ("24x", 24),
    ]
    labels = [lbl for lbl, _ in options]
    choice, ok = QInputDialog.getItem(
        parent,
        "Select Extraction Speed",
        "Choose maximum CD read speed:",
        labels,
        0,
        False,
    )
    if ok and choice in labels:
        return options[labels.index(choice)][1]
    return None


def prompt_calibration_retry_dialog(
    parent: Optional[QWidget],
    device: str,
    error_reason: Optional[str] = None,
) -> CalibrationChoice:
    """Categorized recovery wizard when AccurateRip auto-calibration fails."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Offset Auto-Detection Failed — {device}")
    dlg.setMinimumWidth(560)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    alert_card = QFrame()
    alert_card.setObjectName("stallHeaderAlert")
    card_layout = QVBoxLayout(alert_card)
    card_layout.setContentsMargins(12, 10, 12, 10)

    title_lbl = QLabel(
        "<span style='color: #FF453A; font-weight: bold; font-size: 14px;'>"
        f"⚠️ AccurateRip Auto-Detection Failed ({html.escape(device)})</span>"
    )
    title_lbl.setTextFormat(Qt.TextFormat.RichText)
    reason = error_reason or "Disc TOC was not matched in the AccurateRip database or contains unread sectors."
    body_lbl = QLabel(reason)
    body_lbl.setTextFormat(Qt.TextFormat.PlainText)
    body_lbl.setWordWrap(True)
    card_layout.addWidget(title_lbl)
    card_layout.addWidget(body_lbl)
    layout.addWidget(alert_card)

    opt_group = QGroupBox("Select Recovery Option")
    opt_layout = QVBoxLayout(opt_group)
    opt_layout.setSpacing(8)

    rb_manual = QRadioButton("Enter read offset manually:")
    rb_manual.setChecked(True)

    spin = QSpinBox()
    spin.setRange(-10000, 10000)
    spin.setValue(0)
    spin.setFixedWidth(100)
    mono_font = QFont("Monospace")
    mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
    mono_font.setPointSize(9)
    spin.setFont(mono_font)
    rb_manual.toggled.connect(spin.setEnabled)

    rb_swap = QRadioButton("Eject and swap for another commercial Audio CD")
    rb_zero = QRadioButton("Use 0 as temporary fallback for this session")
    rb_skip = QRadioButton("Skip drive calibration for now")

    man_row = QHBoxLayout()
    man_row.addWidget(rb_manual)
    man_row.addWidget(spin)
    man_row.addStretch(1)
    opt_layout.addLayout(man_row)
    opt_layout.addWidget(rb_swap)
    opt_layout.addWidget(rb_zero)
    opt_layout.addWidget(rb_skip)
    layout.addWidget(opt_group)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn is not None:
        ok_btn.setText("Apply Selection")
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


def prompt_unconfigured_offset_dialog(
    parent: Optional[QWidget],
    device: str,
) -> Tuple[str, Optional[int]]:
    """Dialog prompting user to configure offset before ripping an uncalibrated drive."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Drive Offset Unconfigured — {device}")
    dlg.setMinimumWidth(540)

    layout = QVBoxLayout(dlg)
    layout.setSpacing(12)

    header_card = QFrame()
    header_card.setObjectName("readErrorHeaderAlert")
    card_layout = QVBoxLayout(header_card)
    card_layout.setContentsMargins(12, 10, 12, 10)

    title_lbl = QLabel(
        "<span style='color: #00E5FF; font-weight: bold; font-size: 14px;'>"
        f"⚙️ Sample Read Offset Required ({html.escape(device)})</span>"
    )
    title_lbl.setTextFormat(Qt.TextFormat.RichText)
    body_lbl = QLabel(
        "Whipper requires a sample read offset to verify extracted audio against AccurateRip checksums."
    )
    body_lbl.setWordWrap(True)
    card_layout.addWidget(title_lbl)
    card_layout.addWidget(body_lbl)
    layout.addWidget(header_card)

    opt_group = QGroupBox("Configuration Options")
    opt_layout = QVBoxLayout(opt_group)
    opt_layout.setSpacing(8)

    rb_calibrate = QRadioButton("Calibrate drive now using loaded Audio CD (Recommended)")
    rb_calibrate.setChecked(True)
    rb_zero = QRadioButton("Use 0 as temporary fallback offset")
    rb_manual = QRadioButton("Enter read offset manually:")

    spin = QSpinBox()
    spin.setRange(-10000, 10000)
    spin.setValue(6)
    spin.setFixedWidth(100)
    spin.setEnabled(False)
    mono_font = QFont("Monospace")
    mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
    mono_font.setPointSize(9)
    spin.setFont(mono_font)
    rb_manual.toggled.connect(spin.setEnabled)

    opt_layout.addWidget(rb_calibrate)
    opt_layout.addWidget(rb_zero)
    man_row = QHBoxLayout()
    man_row.addWidget(rb_manual)
    man_row.addWidget(spin)
    man_row.addStretch(1)
    opt_layout.addLayout(man_row)
    layout.addWidget(opt_group)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn is not None:
        ok_btn.setText("Proceed")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        if rb_calibrate.isChecked():
            return "calibrate", None
        if rb_zero.isChecked():
            return "zero", 0
        if rb_manual.isChecked():
            return "manual", spin.value()
    return "cancel", None


def prompt_extraction_error_dialog(
    parent: Optional[QWidget],
    device: str,
    available_drives: List[Tuple[str, str]],
    error_reason: Optional[str] = None,
    has_partial_tracks: bool = True,
    is_hardware_stall: bool = False,
    stalled_track: Optional[int] = None,
) -> ExtractionErrorChoice:
    """Displays a categorized, high-contrast dialog for extraction error recovery."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(
        "Drive Hardware Controller Lockup" if is_hardware_stall else "Extraction Halted — Read Error Recovery"
    )
    dlg.setMinimumWidth(560)

    main_layout = QVBoxLayout(dlg)
    main_layout.setSpacing(12)

    # 1. Diagnostic Header Alert Card
    alert_card = QFrame()
    alert_card.setObjectName("stallHeaderAlert" if is_hardware_stall else "readErrorHeaderAlert")
    card_layout = QVBoxLayout(alert_card)
    card_layout.setContentsMargins(12, 10, 12, 10)

    if is_hardware_stall:
        tr_str = f"Track {stalled_track}" if stalled_track is not None else "disc"
        title_text = (
            "<span style='color: #FF453A; font-weight: bold; font-size: 14px;'>"
            f"⚠️ Drive Hardware Controller Lockup on {html.escape(device)}</span>"
        )
        body_text = (
            f"The optical drive stopped responding for 60s while reading <b>{tr_str}</b>.<br>"
            "This indicates a kernel SCSI deadlock on corrupted disc sectors."
        )
    else:
        title_text = (
            "<span style='color: #00E5FF; font-weight: bold; font-size: 14px;'>"
            f"💿 Extraction Halted on {html.escape(device)}</span>"
        )
        body_text = "cd-paranoia encountered uncorrectable read errors or severe surface scratches."

    lbl_title = QLabel(title_text)
    lbl_title.setTextFormat(Qt.TextFormat.RichText)
    lbl_body = QLabel(body_text)
    lbl_body.setTextFormat(Qt.TextFormat.RichText)
    lbl_body.setWordWrap(True)
    card_layout.addWidget(lbl_title)
    card_layout.addWidget(lbl_body)

    if error_reason:
        lbl_detail = QLabel(f"Detail: {error_reason}")
        lbl_detail.setObjectName("diagnosticDetailText")
        lbl_detail.setTextFormat(Qt.TextFormat.PlainText)
        lbl_detail.setWordWrap(True)
        card_layout.addWidget(lbl_detail)

    main_layout.addWidget(alert_card)

    # Prepare Radio Controls & Enforce Cross-GroupBox Mutual Exclusivity
    other_drives = [(d, hw) for d, hw in available_drives if d != device]
    tr_num = stalled_track if stalled_track is not None else 1

    if is_hardware_stall and len(other_drives) == 1:
        switch_label = f"Switch to Drive {other_drives[0][0]} (Recommended):"
    elif is_hardware_stall and other_drives:
        switch_label = "Switch to another optical drive (Recommended):"
    else:
        switch_label = "Switch to another optical drive:"
    rb_switch = QRadioButton(switch_label)
    drive_combo = QComboBox()
    if other_drives:
        for d, hw in other_drives:
            drive_combo.addItem(f"{d} ({hw})", d)
    else:
        drive_combo.addItem("No other optical drive available", None)
        rb_switch.setEnabled(False)
    drive_combo.setEnabled(False)
    rb_switch.toggled.connect(lambda chk: drive_combo.setEnabled(chk and bool(other_drives)))

    rb_keep = QRadioButton(f"Skip Track {tr_num} & Rescue Remaining Tracks (--keep-going)")
    rb_raw = QRadioButton("Emergency raw sector fallback (cdrdao --paranoia-mode 0)")
    if not shutil.which("cdrdao"):
        rb_raw.setEnabled(False)
        rb_raw.setToolTip("Requires 'cdrdao' installed in PATH.")
    else:
        rb_raw.setToolTip("Bypasses cd-paranoia jitter loops with a sequential raw sector pass (unverified).")

    rb_4x = QRadioButton("Retry album at slow speed (4x)")
    rb_8x = QRadioButton("Retry album at moderate speed (8x)")
    rb_eject = QRadioButton("Eject disc to inspect/clean, then retry")

    rb_partial = QRadioButton("Accept partial rip (keep already-extracted tracks)")
    if not has_partial_tracks:
        rb_partial.setEnabled(False)
        rb_partial.setToolTip("No completed tracks have been extracted yet.")
    rb_abort = QRadioButton("Abort rip")

    btn_group = QButtonGroup(dlg)
    for rb in (rb_switch, rb_keep, rb_raw, rb_4x, rb_8x, rb_eject, rb_partial, rb_abort):
        btn_group.addButton(rb)

    # 2. Recommended Strategy Card
    group_rec = QGroupBox("Recommended Recovery Strategy")
    layout_rec = QVBoxLayout(group_rec)
    if is_hardware_stall and other_drives:
        rec_key = "switch"
        sw_row = QHBoxLayout()
        sw_row.addWidget(rb_switch)
        sw_row.addWidget(drive_combo)
        layout_rec.addLayout(sw_row)
        rb_switch.setChecked(True)
        drive_combo.setEnabled(True)
    elif is_hardware_stall:
        rec_key = "eject"
        layout_rec.addWidget(rb_eject)
        rb_eject.setChecked(True)
    else:
        rec_key = "4x"
        layout_rec.addWidget(rb_4x)
        rb_4x.setChecked(True)
    main_layout.addWidget(group_rec)

    # 3. Advanced Sector Rescue Card
    group_rescue = QGroupBox("Advanced Sector Rescue")
    layout_rescue = QVBoxLayout(group_rescue)
    layout_rescue.addWidget(rb_keep)
    layout_rescue.addWidget(rb_raw)
    main_layout.addWidget(group_rescue)

    # 4. Drive & Speed Adjustments Card
    group_hw = QGroupBox("Drive & Speed Adjustments")
    layout_hw = QVBoxLayout(group_hw)
    if rec_key != "4x":
        layout_hw.addWidget(rb_4x)
    layout_hw.addWidget(rb_8x)
    if rec_key != "eject":
        layout_hw.addWidget(rb_eject)
    if rec_key != "switch":
        sw_row2 = QHBoxLayout()
        sw_row2.addWidget(rb_switch)
        sw_row2.addWidget(drive_combo)
        layout_hw.addLayout(sw_row2)
    main_layout.addWidget(group_hw)

    # 5. Session Termination Card
    group_term = QGroupBox("Session Termination")
    layout_term = QVBoxLayout(group_term)
    layout_term.addWidget(rb_partial)
    layout_term.addWidget(rb_abort)
    main_layout.addWidget(group_term)

    # Button Box
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    if ok_btn is not None:
        ok_btn.setText("Execute Recovery Strategy")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    main_layout.addWidget(buttons)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return ExtractionErrorChoice(ExtractionErrorAction.ABORT)

    if rb_switch.isChecked() and drive_combo.currentData():
        return ExtractionErrorChoice(
            ExtractionErrorAction.SWITCH_DRIVE,
            selected_device=drive_combo.currentData(),
        )
    if rb_keep.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.KEEP_GOING)
    if rb_raw.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.RAW_SECTOR_FALLBACK)
    if rb_4x.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.RETRY_SLOW_4X)
    if rb_8x.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.RETRY_MODERATE_8X)
    if rb_eject.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.EJECT_AND_RETRY)
    if rb_partial.isChecked():
        return ExtractionErrorChoice(ExtractionErrorAction.ACCEPT_PARTIAL)

    return ExtractionErrorChoice(ExtractionErrorAction.ABORT)


def prompt_conflict_resolution_dialog(
    parent: Optional[QWidget],
    flac_dir: Optional[Path],
    opus_dir: Optional[Path],
    warning: Optional[str] = None,
    suggested_suffix: Optional[str] = None,
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
        warn_lbl.setStyleSheet(STYLE_CONFLICT_WARNING)
        warn_lbl.setWordWrap(True)
        layout.addWidget(warn_lbl)

    suffix_label = (
        f"Save alongside existing folder as '{suggested_suffix}' (Recommended)"
        if suggested_suffix
        else "Save alongside existing folder with a timestamp suffix (Recommended)"
    )
    rb_suffix = QRadioButton(suffix_label)
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
    """Categorized desktop dialog for customizing album identity, track titles, and box set parameters."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        artist: str = "",
        album: str = "",
        year: Optional[str] = None,
        genre: Optional[str] = None,
        disc_number: Optional[int] = None,
        total_discs: Optional[int] = None,
        tracks: Optional[List[TrackInfo]] = None,
        has_mbid: bool = False,
        clear_mb_release_id: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Album & Track Details")
        self.setMinimumSize(680, 580)
        self.resize(780, 640)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 1. Header & MusicBrainz Lock Status Banner
        header_card = QFrame()
        header_card.setObjectName("diagnosticHeaderCard")
        hdr_layout = QHBoxLayout(header_card)
        hdr_layout.setContentsMargins(12, 10, 12, 10)

        hdr_text_box = QVBoxLayout()
        title_lbl = QLabel("<h3 style='margin:0; color:#ECECF1;'>🏷️ Edit Album &amp; Track Metadata</h3>")
        title_lbl.setTextFormat(Qt.TextFormat.RichText)
        sub_lbl = QLabel("Customize metadata tags written to FLAC archival masters and Opus portable files.")
        sub_lbl.setWordWrap(True)
        sub_lbl.setStyleSheet(STYLE_SUBTITLE_MUTED)
        hdr_text_box.addWidget(title_lbl)
        hdr_text_box.addWidget(sub_lbl)
        hdr_layout.addLayout(hdr_text_box, stretch=1)

        self.mb_status_badge = QLabel()
        self.mb_status_badge.setFixedHeight(24)
        self.mb_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr_layout.addWidget(self.mb_status_badge)

        layout.addWidget(header_card)

        # 2. Album Identity & Box Set Parameters Card
        album_group = QGroupBox("Album Identity & Release Parameters")
        album_layout = QVBoxLayout(album_group)
        album_layout.setSpacing(8)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Artist:"))
        self.artist_edit = QLineEdit(artist if artist != "Unknown Artist" else "")
        self.artist_edit.setPlaceholderText("Album Artist Name")
        row1.addWidget(self.artist_edit, stretch=2)

        row1.addWidget(QLabel("Album Title:"))
        self.album_edit = QLineEdit(album if album != "Unknown Album" else "")
        self.album_edit.setPlaceholderText("Album Title")
        row1.addWidget(self.album_edit, stretch=3)
        album_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Year / Date:"))
        self.year_edit = QLineEdit(year or "")
        self.year_edit.setPlaceholderText("e.g. 1998")
        self.year_edit.setFixedWidth(110)
        row2.addWidget(self.year_edit)

        row2.addSpacing(16)
        row2.addWidget(QLabel("Genre:"))
        self.genre_edit = QLineEdit(genre or "")
        self.genre_edit.setPlaceholderText("e.g. Electronic / House")
        row2.addWidget(self.genre_edit, stretch=1)
        album_layout.addLayout(row2)

        row3 = QHBoxLayout()
        self.multidisc_chk = QCheckBox("Multi-disc box set:")
        is_multi = bool((disc_number is not None and disc_number > 1) or (total_discs is not None and total_discs > 1))
        self.multidisc_chk.setChecked(is_multi)
        row3.addWidget(self.multidisc_chk)

        row3.addWidget(QLabel("Disc"))
        self.disc_spin = QSpinBox()
        self.disc_spin.setRange(1, 99)
        self.disc_spin.setValue(disc_number if disc_number is not None else 1)
        self.disc_spin.setEnabled(is_multi)
        self.disc_spin.setFixedWidth(55)
        row3.addWidget(self.disc_spin)

        row3.addWidget(QLabel("of"))
        self.total_discs_spin = QSpinBox()
        self.total_discs_spin.setRange(1, 99)
        self.total_discs_spin.setValue(total_discs if total_discs is not None else max(2, self.disc_spin.value()))
        self.total_discs_spin.setEnabled(is_multi)
        self.total_discs_spin.setFixedWidth(55)
        row3.addWidget(self.total_discs_spin)

        self.multidisc_chk.toggled.connect(self._on_multidisc_toggled)

        row3.addSpacing(24)
        self.compilation_chk = QCheckBox("Various Artists (per-track artists)")
        has_per_track_art = any(bool(getattr(tr, "artist", None) and tr.artist != artist) for tr in (tracks or []))
        self.compilation_chk.setChecked(has_per_track_art)
        self.compilation_chk.toggled.connect(self._on_compilation_toggled)
        row3.addWidget(self.compilation_chk)
        row3.addStretch(1)
        album_layout.addLayout(row3)

        self._has_mbid = has_mbid
        self._initial_clear_mb_release_id = clear_mb_release_id
        self._detach_user_toggled = False
        self._orig_artist = self.artist_edit.text().strip()
        self._orig_album = self.album_edit.text().strip()
        self._orig_year = self.year_edit.text().strip()
        self._orig_is_multi = is_multi
        self._orig_total_discs: Optional[int] = (
            max(self.disc_spin.value(), self.total_discs_spin.value()) if is_multi else None
        )

        self.detach_mb_chk = QCheckBox(
            "Detach from MusicBrainz Release ID (clear release MBID tags)"
        )
        self.detach_mb_chk.setToolTip(
            "Recommended when correcting album title, artist, year,\n"
            "or multi-disc numbering for a release that does not\n"
            "exactly match MusicBrainz.\n\n"
            "Clearing release-level MusicBrainz IDs prevents media\n"
            "players (Plex, Roon, Kodi) from splitting discs into separate albums."
        )
        self.detach_mb_chk.setChecked(clear_mb_release_id)
        self.detach_mb_chk.setVisible(has_mbid)
        self.detach_mb_chk.clicked.connect(self._on_detach_mb_clicked)
        album_layout.addWidget(self.detach_mb_chk)
        self._sync_mb_status_badge()

        self.artist_edit.textChanged.connect(self._on_album_identity_changed)
        self.album_edit.textChanged.connect(self._on_album_identity_changed)
        self.year_edit.textChanged.connect(self._on_album_identity_changed)
        self.multidisc_chk.toggled.connect(self._on_album_identity_changed)
        self.disc_spin.valueChanged.connect(self._on_album_identity_changed)
        self.total_discs_spin.valueChanged.connect(self._on_album_identity_changed)

        layout.addWidget(album_group)

        # 3. Tracklist Management Card
        track_group = QGroupBox("Tracklist & Clipboard Tools")
        track_layout = QVBoxLayout(track_group)
        track_layout.setSpacing(6)

        btn_row = QHBoxLayout()
        self.paste_btn = QPushButton("📋 Import Tracklist from Clipboard")
        self.paste_btn.setToolTip("Parses track titles and durations from Wikipedia, Bandcamp, or Discogs")
        self.paste_btn.clicked.connect(self._on_paste_clipboard)
        btn_row.addWidget(self.paste_btn)

        self.shift_down_btn = QPushButton("↓ Shift Down (+1)")
        self.shift_down_btn.setToolTip("Shifts all titles down by 1 row (useful if Track 1 is a hidden pregap track)")
        self.shift_down_btn.clicked.connect(self._on_shift_down)
        btn_row.addWidget(self.shift_down_btn)

        self.shift_up_btn = QPushButton("↑ Shift Up (-1)")
        self.shift_up_btn.setToolTip("Shifts all titles up by 1 row (useful if an extra intro was skipped)")
        self.shift_up_btn.clicked.connect(self._on_shift_up)
        btn_row.addWidget(self.shift_up_btn)

        self.clear_btn = QPushButton("Clear Titles")
        self.clear_btn.clicked.connect(self._on_clear_titles)
        btn_row.addWidget(self.clear_btn)

        btn_row.addStretch(1)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(STYLE_STATUS_INFO)
        btn_row.addWidget(self.status_lbl)
        track_layout.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["#", "Track Title", "Track Artist", "Duration"])
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnHidden(2, not has_per_track_art)

        mono_font = QFont("Monospace")
        mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
        mono_font.setPointSize(9)

        raw_tracks = tracks or []
        self.table.setRowCount(len(raw_tracks))
        for row, tr in enumerate(raw_tracks):
            num_item = QTableWidgetItem(f"{tr.number:02d}")
            num_item.setFont(mono_font)
            num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            num_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            title_item = QTableWidgetItem(tr.title)
            title_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)

            artist_item = QTableWidgetItem(getattr(tr, "artist", None) or "")
            artist_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable)

            dur_item = QTableWidgetItem(tr.duration or "")
            dur_item.setFont(mono_font)
            dur_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dur_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            self.table.setItem(row, 0, num_item)
            self.table.setItem(row, 1, title_item)
            self.table.setItem(row, 2, artist_item)
            self.table.setItem(row, 3, dur_item)

        track_layout.addWidget(self.table)
        layout.addWidget(track_group, stretch=1)

        # Dialog Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("Save Metadata")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _sync_mb_status_badge(self) -> None:
        if self._has_mbid and not self.detach_mb_chk.isChecked():
            self.mb_status_badge.setText("🔒 Linked to MusicBrainz")
            self.mb_status_badge.setStyleSheet(LED_PILL_GREEN)
        elif self._has_mbid:
            self.mb_status_badge.setText("🔓 Detached from MusicBrainz")
            self.mb_status_badge.setStyleSheet(LED_PILL_UNCATALOGED)
        else:
            self.mb_status_badge.setText("🔓 Custom Metadata Mode")
            self.mb_status_badge.setStyleSheet(LED_PILL_UNCATALOGED)

    def _on_multidisc_toggled(self, checked: bool) -> None:
        self.disc_spin.setEnabled(checked)
        self.total_discs_spin.setEnabled(checked)
        if checked and self.total_discs_spin.value() < self.disc_spin.value():
            self.total_discs_spin.setValue(max(2, self.disc_spin.value()))

    def _on_compilation_toggled(self, checked: bool) -> None:
        self.table.setColumnHidden(2, not checked)

    def _on_detach_mb_clicked(self) -> None:
        self._detach_user_toggled = True
        self._sync_mb_status_badge()

    def _current_identity(self) -> Tuple[str, str, str, bool, Optional[int]]:
        cur_tot: Optional[int] = (
            max(self.disc_spin.value(), self.total_discs_spin.value())
            if self.multidisc_chk.isChecked()
            else None
        )
        return (
            self.artist_edit.text().strip(),
            self.album_edit.text().strip(),
            self.year_edit.text().strip(),
            self.multidisc_chk.isChecked(),
            cur_tot,
        )

    def _identity_changed(self) -> bool:
        orig_identity = (
            self._orig_artist,
            self._orig_album,
            self._orig_year,
            self._orig_is_multi,
            self._orig_total_discs,
        )
        return self._current_identity() != orig_identity

    def _on_album_identity_changed(self) -> None:
        if not self._has_mbid or self._detach_user_toggled:
            return
        if self._identity_changed():
            self.detach_mb_chk.setChecked(True)
        elif not self._initial_clear_mb_release_id:
            self.detach_mb_chk.setChecked(False)
        self._sync_mb_status_badge()

    @property
    def clear_mb_release_id(self) -> bool:
        if self._has_mbid:
            return bool(self.detach_mb_chk.isChecked())
        return bool(self._initial_clear_mb_release_id or self._identity_changed())

    def _on_shift_down(self) -> None:
        table_rows = self.table.rowCount()
        if table_rows < 2:
            return
        for row in range(table_rows - 1, 0, -1):
            prev_title = self.table.item(row - 1, 1).text() if self.table.item(row - 1, 1) else ""
            prev_art = self.table.item(row - 1, 2).text() if self.table.item(row - 1, 2) else ""
            if self.table.item(row, 1):
                self.table.item(row, 1).setText(prev_title)
            if self.table.item(row, 2):
                self.table.item(row, 2).setText(prev_art)
        if self.table.item(0, 1):
            self.table.item(0, 1).setText("Track 01")
        if self.table.item(0, 2):
            self.table.item(0, 2).setText("")
        self.status_lbl.setText("✓ Shifted track titles down (+1)")

    def _on_shift_up(self) -> None:
        table_rows = self.table.rowCount()
        if table_rows < 2:
            return
        for row in range(table_rows - 1):
            next_title = self.table.item(row + 1, 1).text() if self.table.item(row + 1, 1) else ""
            next_art = self.table.item(row + 1, 2).text() if self.table.item(row + 1, 2) else ""
            if self.table.item(row, 1):
                self.table.item(row, 1).setText(next_title)
            if self.table.item(row, 2):
                self.table.item(row, 2).setText(next_art)
        last_row = table_rows - 1
        if self.table.item(last_row, 1):
            self.table.item(last_row, 1).setText(f"Track {table_rows:02d}")
        if self.table.item(last_row, 2):
            self.table.item(last_row, 2).setText("")
        self.status_lbl.setText("✓ Shifted track titles up (-1)")

    def _on_paste_clipboard(self, start_index: Optional[int] = None) -> None:
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

        table_rows = self.table.rowCount()
        if len(titles) > table_rows:
            if start_index is not None:
                start_offset = start_index
            else:
                suggested_start = 1
                if self.multidisc_chk.isChecked() and self.disc_spin.value() > 1:
                    calc = (self.disc_spin.value() - 1) * table_rows + 1
                    if calc <= len(titles):
                        suggested_start = calc
                start_offset, ok = QInputDialog.getInt(
                    self,
                    "Tracklist Range",
                    f"Clipboard contains {len(titles)} titles, but this disc has {table_rows} tracks.\n\n"
                    f"Start importing from title number:",
                    suggested_start,
                    1,
                    len(titles),
                    1,
                )
                if not ok:
                    return
            titles = titles[max(0, start_offset - 1):]

        applied = 0
        is_comp = self.compilation_chk.isChecked()
        for row in range(table_rows):
            item_title = self.table.item(row, 1)
            item_art = self.table.item(row, 2)
            if row < len(titles):
                raw_entry = titles[row]
                if is_comp and " - " in raw_entry:
                    p_art, p_title = raw_entry.split(" - ", 1)
                    if item_art:
                        item_art.setText(p_art.strip())
                    if item_title:
                        item_title.setText(p_title.strip())
                else:
                    if item_title:
                        item_title.setText(raw_entry)
                applied += 1
            else:
                if item_title:
                    item_title.setText(f"Track {row + 1:02d}")
                if item_art:
                    item_art.setText("")

        self.status_lbl.setText(f"✓ Pasted {applied} track title(s)")

    def _on_clear_titles(self) -> None:
        for row in range(self.table.rowCount()):
            item_title = self.table.item(row, 1)
            if item_title:
                item_title.setText(f"Track {row + 1:02d}")
            item_art = self.table.item(row, 2)
            if item_art:
                item_art.setText("")
        self.status_lbl.setText("Titles reset to generic")

    def get_metadata(
        self,
    ) -> Tuple[str, str, Optional[str], Optional[str], Optional[int], Optional[int], List[TrackInfo]]:
        art = self.artist_edit.text().strip() or "Unknown Artist"
        alb = self.album_edit.text().strip() or "Unknown Album"
        yr = self.year_edit.text().strip() or None
        gnr = self.genre_edit.text().strip() or None

        disc_no: Optional[int] = None
        tot_discs: Optional[int] = None
        if self.multidisc_chk.isChecked():
            disc_no = self.disc_spin.value()
            tot_discs = max(disc_no, self.total_discs_spin.value())

        is_comp = self.compilation_chk.isChecked()
        updated_tracks: List[TrackInfo] = []
        for row in range(self.table.rowCount()):
            num_item = self.table.item(row, 0)
            title_item = self.table.item(row, 1)
            art_item = self.table.item(row, 2)
            dur_item = self.table.item(row, 3)
            t_num = int(num_item.text()) if num_item and num_item.text().isdigit() else (row + 1)
            t_title = title_item.text().strip() if title_item and title_item.text().strip() else f"Track {t_num}"
            t_art = art_item.text().strip() if (is_comp and art_item and art_item.text().strip()) else None
            t_dur = dur_item.text().strip() if dur_item and dur_item.text().strip() else None
            updated_tracks.append(TrackInfo(number=t_num, title=t_title, artist=t_art, duration=t_dur))

        return art, alb, yr, gnr, disc_no, tot_discs, updated_tracks


def prompt_edit_metadata_dialog(
    parent: Optional[QWidget],
    artist: str,
    album: str,
    year: Optional[str] = None,
    genre: Optional[str] = None,
    disc_number: Optional[int] = None,
    total_discs: Optional[int] = None,
    tracks: Optional[List[TrackInfo]] = None,
) -> Optional[Tuple[str, str, Optional[str], Optional[str], Optional[int], Optional[int], List[TrackInfo]]]:
    dlg = EditMetadataDialog(
        parent,
        artist=artist,
        album=album,
        year=year,
        genre=genre,
        disc_number=disc_number,
        total_discs=total_discs,
        tracks=tracks,
    )
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.get_metadata()
    return None


class PreferencesDialog(QDialog):
    """Desktop preferences dialog for configuring output paths, audio mirrors, and drive defaults."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        prefs: Optional[UserPreferences] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(620)
        self.prefs = prefs or load_preferences()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        header_card = QFrame()
        header_card.setObjectName("diagnosticHeaderCard")
        hdr_layout = QVBoxLayout(header_card)
        hdr_layout.setContentsMargins(12, 10, 12, 10)

        title_lbl = QLabel("<h3 style='margin:0; color:#ECECF1;'>⚙️ WhipIt Preferences &amp; Archival Defaults</h3>")
        title_lbl.setTextFormat(Qt.TextFormat.RichText)
        sub_lbl = QLabel("Configure destination roots, dual FLAC/Opus output mirrors, and optical drive speed caps.")
        sub_lbl.setStyleSheet(STYLE_SUBTITLE_MUTED)
        sub_lbl.setWordWrap(True)
        hdr_layout.addWidget(title_lbl)
        hdr_layout.addWidget(sub_lbl)
        layout.addWidget(header_card)

        dest_group = QGroupBox("Output Destinations & Audio Mirrors")
        dest_layout = QVBoxLayout(dest_group)
        dest_layout.setSpacing(8)

        flac_row = QHBoxLayout()
        flac_row.addWidget(QLabel("FLAC Archival Root:"))
        self.flac_edit = QLineEdit(str(self.prefs.flac_root))
        flac_row.addWidget(self.flac_edit, stretch=1)
        self.flac_browse_btn = QPushButton("Browse...")
        self.flac_browse_btn.clicked.connect(self._browse_flac)
        flac_row.addWidget(self.flac_browse_btn)
        dest_layout.addLayout(flac_row)

        self.opus_chk = QCheckBox("Generate Opus 192k portable mirror archive")
        self.opus_chk.setChecked(self.prefs.generate_opus)
        self.opus_chk.toggled.connect(self._toggle_opus_fields)
        dest_layout.addWidget(self.opus_chk)

        self.opus_row_widget = QWidget()
        opus_row = QHBoxLayout(self.opus_row_widget)
        opus_row.setContentsMargins(0, 0, 0, 0)
        opus_row.addWidget(QLabel("Opus Mirror Root:"))
        self.opus_edit = QLineEdit(str(self.prefs.opus_root))
        opus_row.addWidget(self.opus_edit, stretch=1)
        self.opus_browse_btn = QPushButton("Browse...")
        self.opus_browse_btn.clicked.connect(self._browse_opus)
        opus_row.addWidget(self.opus_browse_btn)
        dest_layout.addWidget(self.opus_row_widget)

        opus_opts_row = QHBoxLayout()
        opus_opts_row.addWidget(QLabel("Opus Encoder Bitrate:"))
        self.opus_bitrate_combo = QComboBox()
        self.opus_bitrate_combo.addItem("96 kbps (Compact / Minimal Storage)", 96)
        self.opus_bitrate_combo.addItem("128 kbps (Standard Mobile)", 128)
        self.opus_bitrate_combo.addItem("160 kbps (High Quality)", 160)
        self.opus_bitrate_combo.addItem("192 kbps (Transparent — Recommended Default)", 192)
        self.opus_bitrate_combo.addItem("256 kbps (Ultra High Quality)", 256)
        self.opus_bitrate_combo.addItem("320 kbps (Maximum Standard Bitrate)", 320)

        cur_bitrate = getattr(self.prefs, "opus_bitrate", 192)
        b_idx = self.opus_bitrate_combo.findData(cur_bitrate)
        self.opus_bitrate_combo.setCurrentIndex(b_idx if b_idx >= 0 else 3)
        opus_opts_row.addWidget(self.opus_bitrate_combo)
        opus_opts_row.addStretch(1)
        dest_layout.addLayout(opus_opts_row)

        self._toggle_opus_fields(self.prefs.generate_opus)
        layout.addWidget(dest_group)

        hw_group = QGroupBox("Hardware Performance & Drive Automation")
        hw_layout = QVBoxLayout(hw_group)
        hw_layout.setSpacing(8)

        retries_row = QHBoxLayout()
        retries_row.addWidget(QLabel("Max Track Read Retries:"))
        self.retries_combo = QComboBox()
        self.retries_combo.addItem("5 (Fast / Strict)", 5)
        self.retries_combo.addItem("10 (Standard Default)", 10)
        self.retries_combo.addItem("20 (Deep Scratch Recovery)", 20)
        self.retries_combo.addItem("30 (Maximum Persistence)", 30)
        self.retries_combo.addItem("50 (Extended Archival Pass)", 50)

        cur_retries = getattr(self.prefs, "max_retries", 10)
        r_idx = self.retries_combo.findData(cur_retries)
        self.retries_combo.setCurrentIndex(r_idx if r_idx >= 0 else 1)
        self.retries_combo.setToolTip(
            "Number of read passes cd-paranoia attempts on corrupted sectors before prompting for error recovery.\n"
            "Higher settings (20–30) help resolve hairline scuffs when paired with a 4x read speed cap."
        )
        retries_row.addWidget(self.retries_combo)
        retries_row.addStretch(1)
        hw_layout.addLayout(retries_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Default Read Speed Limit:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItem("Full Speed (Normal Maximum)", None)
        self.speed_combo.addItem("4x (Safe / Scratched Discs)", 4)
        self.speed_combo.addItem("8x (Moderate / Recommended)", 8)
        self.speed_combo.addItem("12x", 12)
        self.speed_combo.addItem("16x", 16)
        self.speed_combo.addItem("24x", 24)

        if self.prefs.default_speed is not None:
            idx = self.speed_combo.findData(self.prefs.default_speed)
            if idx >= 0:
                self.speed_combo.setCurrentIndex(idx)
        speed_row.addWidget(self.speed_combo)
        speed_row.addStretch(1)
        hw_layout.addLayout(speed_row)

        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("Sample Read Offset Override:"))
        self.offset_edit = QLineEdit()
        self.offset_edit.setPlaceholderText("Auto / Calibrated")
        self.offset_edit.setToolTip(
            "Leave blank to use drive auto-calibration or stored AccurateRip offset (-10000 to +10000)"
        )
        self.offset_edit.setFixedWidth(140)

        mono_font = QFont("Monospace")
        mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
        mono_font.setPointSize(9)
        self.offset_edit.setFont(mono_font)

        if self.prefs.offset_override is not None:
            self.offset_edit.setText(str(self.prefs.offset_override))
        offset_row.addWidget(self.offset_edit)
        offset_row.addStretch(1)
        hw_layout.addLayout(offset_row)

        self.eject_chk = QCheckBox("Auto-eject optical drive tray upon successful rip completion")
        self.eject_chk.setChecked(self.prefs.auto_eject)
        hw_layout.addWidget(self.eject_chk)

        layout.addWidget(hw_group)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_btn is not None:
            save_btn.setText("Save Preferences")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.saved_preferences: Optional[UserPreferences] = None

    def _toggle_opus_fields(self, enabled: bool) -> None:
        self.opus_edit.setEnabled(enabled)
        self.opus_browse_btn.setEnabled(enabled)
        self.opus_bitrate_combo.setEnabled(enabled)

    def _browse_flac(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Select FLAC Archival Directory", self.flac_edit.text())
        if f:
            self.flac_edit.setText(f)

    def _browse_opus(self) -> None:
        f = QFileDialog.getExistingDirectory(self, "Select Opus Mirror Directory", self.opus_edit.text())
        if f:
            self.opus_edit.setText(f)

    def _on_save(self) -> None:
        flac_text = self.flac_edit.text().strip()
        opus_text = self.opus_edit.text().strip()

        if not flac_text:
            QMessageBox.warning(self, "Invalid Path", "FLAC Archival Root cannot be empty.")
            return

        raw_offset = self.offset_edit.text().strip()
        offset_override: Optional[int] = None
        if raw_offset:
            if not re.match(r"^[+-]?\d+$", raw_offset):
                QMessageBox.warning(self, "Invalid Offset", "Sample read offset must be an integer (e.g. +6, 667, -12).")
                return
            offset_override = int(raw_offset)
            if not -10000 <= offset_override <= 10000:
                QMessageBox.warning(self, "Invalid Offset", "Sample read offset must be between -10000 and 10000.")
                return

        flac_path = Path(flac_text).expanduser()
        opus_path = Path(opus_text).expanduser() if opus_text else flac_path

        try:
            new_prefs = UserPreferences(
                flac_root=flac_path,
                opus_root=opus_path,
                generate_opus=self.opus_chk.isChecked(),
                opus_bitrate=int(self.opus_bitrate_combo.currentData() or 192),
                max_retries=int(self.retries_combo.currentData() or 10),
                default_speed=self.speed_combo.currentData(),
                offset_override=offset_override,
                auto_eject=self.eject_chk.isChecked(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Validation Error", f"Invalid preference values: {exc}")
            return

        save_preferences(new_prefs)
        self.saved_preferences = new_prefs
        self.accept()


def prompt_preferences_dialog(
    parent: Optional[QWidget] = None,
    prefs: Optional[UserPreferences] = None,
) -> Optional[UserPreferences]:
    dlg = PreferencesDialog(parent=parent, prefs=prefs)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.saved_preferences
    return None


_ACTIVE_DIAG_WORKERS: set[QThread] = set()


class _DiagWorker(QThread):
    report_ready = pyqtSignal(str)

    def __init__(self, selected_device: Optional[str] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.selected_device = selected_device

    def run(self) -> None:
        from whipit.diagnostics import generate_diagnostic_report
        report = generate_diagnostic_report(selected_device=self.selected_device, check_network=True)
        self.report_ready.emit(report)


def _format_knoppix_diagnostic_html(raw_report: str) -> Tuple[str, int, int, int]:
    """Transforms raw diagnostic report lines into Knoppix terminal HTML with pass/warn/fail counts."""
    ok_count = 0
    warn_count = 0
    fail_count = 0
    html_lines: List[str] = []

    for line in raw_report.splitlines():
        escaped = html.escape(line)

        if (
            escaped.startswith("[")
            and escaped.endswith("]")
            and not any(tag in escaped for tag in ("OK", "WARN", "FAIL", "INFO"))
        ):
            sec_title = escaped[1:-1]
            html_lines.append(
                "<div style='color: #00E5FF; font-weight: bold; margin-top: 10px; margin-bottom: 4px;'>"
                f"&gt;&gt;&gt; [{sec_title}]</div>"
            )
            continue

        if escaped.startswith("====") or "Diagnostic Report" in escaped:
            html_lines.append(f"<div style='color: #30D158; font-weight: bold;'>{escaped}</div>")
            continue

        if "[OK]" in escaped:
            ok_count += escaped.count("[OK]")
            escaped = escaped.replace(
                "[OK]",
                "<span style='color: #30D158; font-weight: bold;'>[  OK  ]</span>",
            )
        if "[WARN]" in escaped:
            warn_count += escaped.count("[WARN]")
            escaped = escaped.replace(
                "[WARN]",
                "<span style='color: #FF9F0A; font-weight: bold;'>[ WARN ]</span>",
            )
        if "[FAIL]" in escaped:
            fail_count += escaped.count("[FAIL]")
            escaped = escaped.replace(
                "[FAIL]",
                "<span style='color: #FF453A; font-weight: bold;'>[ FAIL ]</span>",
            )
        if "[INFO]" in escaped:
            escaped = escaped.replace(
                "[INFO]",
                "<span style='color: #00E5FF; font-weight: bold;'>[ INFO ]</span>",
            )

        if escaped.startswith("• "):
            escaped = (
                "<span style='color: #888899;'>•</span> "
                f"<span style='color: #DFDFE6;'>{escaped[2:]}</span>"
            )

        html_lines.append(f"<div style='font-family: monospace; line-height: 1.3;'>{escaped}</div>")

    formatted_html = (
        "<div style='background-color: #101014; color: #D4D4DC; font-family: monospace;'>"
        f"{''.join(html_lines)}</div>"
    )
    return formatted_html, ok_count, warn_count, fail_count


class DiagnosticDialog(QDialog):
    """Knoppix terminal-inspired system and hardware diagnostics viewer."""

    def __init__(self, parent: Optional[QWidget] = None, selected_device: Optional[str] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("WhipIt System & Hardware Diagnostics")
        self.setMinimumSize(760, 560)
        self.resize(840, 620)
        self.selected_device = selected_device
        self.raw_report_text = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        header_card = QFrame()
        header_card.setObjectName("diagnosticHeaderCard")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        header_lbl = QLabel("<h3 style='margin:0; color:#ECECF1;'>⚡ System &amp; Hardware Diagnostics</h3>")
        desc_lbl = QLabel(
            "Inspects optical drive capabilities, Linux user permissions, AccurateRip offsets, "
            "audio toolchain binaries, and online service connectivity."
        )
        desc_lbl.setStyleSheet(STYLE_SUBTITLE_MUTED)
        desc_lbl.setWordWrap(True)
        header_layout.addWidget(header_lbl)
        header_layout.addWidget(desc_lbl)
        layout.addWidget(header_card)

        self.text_box = QTextEdit()
        self.text_box.setObjectName("diagnosticConsole")
        self.text_box.setReadOnly(True)

        mono_font = QFont("Monospace")
        mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
        mono_font.setPointSize(9)
        self.text_box.setFont(mono_font)
        self.text_box.setHtml("<span style='color: #00E5FF;'>Initialising diagnostic inspector...</span>")
        layout.addWidget(self.text_box, stretch=1)

        btn_row = QHBoxLayout()
        self.copy_btn = QPushButton("📋 Copy Report")
        self.copy_btn.setToolTip("Copy raw diagnostic report to clipboard")
        self.copy_btn.clicked.connect(self._copy_to_clipboard)
        btn_row.addWidget(self.copy_btn)

        self.refresh_btn = QPushButton("🔄 Re-run Inspection")
        self.refresh_btn.clicked.connect(self._run_diagnostics)
        btn_row.addWidget(self.refresh_btn)

        self.hud_lbl = QLabel("Initializing...")
        self.hud_lbl.setObjectName("diagnosticSummaryHud")
        btn_row.addWidget(self.hud_lbl)

        btn_row.addStretch(1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._run_diagnostics()

    def _run_diagnostics(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.hud_lbl.setText("Running system checks...")
        self.hud_lbl.setStyleSheet("color: #00E5FF;")
        self.worker = _DiagWorker(self.selected_device, parent=None)
        _ACTIVE_DIAG_WORKERS.add(self.worker)
        self.worker.report_ready.connect(self._on_report_ready)
        self.worker.finished.connect(lambda w=self.worker: _ACTIVE_DIAG_WORKERS.discard(w))
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def done(self, result: int) -> None:
        if getattr(self, "worker", None) is not None:
            try:
                self.worker.report_ready.disconnect(self._on_report_ready)
            except Exception:
                pass
        super().done(result)

    def _on_report_ready(self, report: str) -> None:
        self.raw_report_text = report
        html_content, ok_cnt, warn_cnt, fail_cnt = _format_knoppix_diagnostic_html(report)
        self.text_box.setHtml(html_content)
        self.text_box.moveCursor(QTextCursor.MoveOperation.Start)
        self.refresh_btn.setEnabled(True)

        status_color = "#30D158" if fail_cnt == 0 else "#FF453A"
        self.hud_lbl.setStyleSheet(f"color: {status_color};")
        self.hud_lbl.setText(f"Checks: {ok_cnt} Passed  |  {warn_cnt} Warnings  |  {fail_cnt} Failed")

    def _copy_to_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        payload = self.raw_report_text or self.text_box.toPlainText()
        if clipboard and payload:
            clipboard.setText(payload)
            self.hud_lbl.setText("📋 Diagnostic report copied to clipboard!")


def show_diagnostic_dialog(parent: Optional[QWidget] = None, selected_device: Optional[str] = None) -> None:
    dlg = DiagnosticDialog(parent=parent, selected_device=selected_device)
    dlg.exec()
