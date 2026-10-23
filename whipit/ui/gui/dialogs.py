"""Modal user prompt dialogs for the WhipIt desktop GUI."""

from __future__ import annotations

import html
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.launcher import get_icon_path
from whipit.models import JobManifest
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
)


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
