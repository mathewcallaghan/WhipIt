"""Main application window and modal interaction dialogs for WhipIt GUI."""

from __future__ import annotations

import html
import queue
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.audio.metadata import fetch_discogs_url_from_mbid
from whipit.bootstrap import (
    check_or_install_dependencies,
    get_missing_dependencies,
)
from whipit.config import (
    get_disc_status,
    drive_has_disc,
    eject_drive,
    get_drive_hardware_id,
    get_drives_with_hardware_ids,
    get_output_root,
)
from whipit.inhibit import MediaDialogInhibitor
from whipit.launcher import get_icon_path, install_icon, resolve_executable_path
from whipit.models import JobManifest, ReleaseOption, TrackInfo, WorkflowState
from whipit.net import is_strict_https_url
from whipit.recovery import RecoveryCoordinator
from whipit.ripping.identify import DiscInspectionResult, _MB_REL_RE, inspect_disc
from whipit.state_store import ManifestStore
from whipit.ui.base import (
    CalibrationAction,
    CalibrationChoice,
    ConflictChoice,
    DiscPromptAction,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
)
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.ui.gui.updater import UpdateWorker
from whipit.ui.gui.worker import (
    CoverArtWorker,
    DiscardWorker,
    DrivePollWorker,
    EjectWorker,
    IdentifyWorker,
    ReleaseDetailsWorker,
    ResumeWorker,
    RipWorker,
    StartupCheckWorker,
)

PIPELINE_STEPS: List[Tuple[str, str, List[WorkflowState]]] = [
    ("DISC_DETECTED", "1. Disc Ready", [WorkflowState.DISC_DETECTED]),
    (
        "RIPPING",
        "2. Rip & Verify",
        [
            WorkflowState.RIPPING,
            WorkflowState.EXTRACTION_HALTED,
            WorkflowState.RIP_EXTRACTED,
            WorkflowState.RIP_VALIDATED,
            WorkflowState.CANDIDATE_STAGED,
        ],
    ),
    (
        "OPUS_ENCODED",
        "3. Opus & ReplayGain",
        [
            WorkflowState.OPUS_ENCODED,
            WorkflowState.REPLAYGAIN_APPLIED,
            WorkflowState.CANDIDATE_VERIFIED,
        ],
    ),
    ("PUBLISHING", "4. Publishing", [WorkflowState.PUBLISHING]),
    ("COMMITTED", "5. Committed", [WorkflowState.COMMITTED]),
]

_ACTIVE_DETACHED_WORKERS: set[QThread] = set()


def _track_detached_worker(worker: QThread) -> QThread:
    _ACTIVE_DETACHED_WORKERS.add(worker)
    worker.finished.connect(lambda: _ACTIVE_DETACHED_WORKERS.discard(worker))
    worker.finished.connect(worker.deleteLater)
    return worker


def _is_worker_running(worker: Optional[QThread]) -> bool:
    if worker is None:
        return False
    try:
        return worker.isRunning()
    except RuntimeError:
        return False

__all__ = [
    "CoverArtWorker",
    "DiscardWorker",
    "DrivePollWorker",
    "EjectWorker",
    "IdentifyWorker",
    "MainWindow",
    "ReleaseDetailsWorker",
    "ResumeWorker",
    "RipWorker",
    "StartupCheckWorker",
]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"WhipIt v{__version__} — Archival CD Ripper & Opus Transcoder")
        self.setMinimumSize(880, 780)
        self.resize(940, 840)

        icon_path = get_icon_path()
        if not icon_path.exists():
            try:
                icon_path = install_icon()
            except Exception:
                pass
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.adapter = GuiInteractionAdapter()
        self.worker: Optional[RipWorker] = None
        self.identify_worker: Optional[IdentifyWorker] = None
        self.details_worker: Optional[ReleaseDetailsWorker] = None
        self.cover_worker: Optional[CoverArtWorker] = None
        self.poll_worker: Optional[DrivePollWorker] = None
        self.startup_worker: Optional[StartupCheckWorker] = None
        self.update_worker: Optional[UpdateWorker] = None
        self.resume_worker: Optional[ResumeWorker] = None
        self.discard_worker: Optional[DiscardWorker] = None
        self.eject_worker: Optional[EjectWorker] = None
        self._scraped_artist: Optional[str] = None
        self._scraped_title: Optional[str] = None
        self._ripping_started: bool = False
        self._last_inspection: Optional[DiscInspectionResult] = None
        self.badge_labels: Dict[str, QLabel] = {}
        self._current_step_key: Optional[str] = None

        self._current_mbid: Optional[str] = None
        self._current_mb_url: Optional[str] = None
        self._current_mb_lookup_url: Optional[str] = None
        self._current_discogs_url: Optional[str] = None
        self._matching_releases: List[ReleaseOption] = []
        self._current_art_path: Optional[Path] = None
        self._downloaded_art_path: Optional[Path] = None
        self._art_is_user_selected: bool = False
        self._disc_was_ejected: bool = False
        self._requested_mbids: set[str] = set()

        self._setup_ui()
        self._connect_signals()
        self._apply_stylesheet()

        self.refresh_drives()
        self.drive_poll_timer = QTimer(self)
        self.drive_poll_timer.setInterval(2500)
        self.drive_poll_timer.timeout.connect(self._trigger_drive_poll)
        self.drive_poll_timer.start()

        QTimer.singleShot(250, self._startup_checks)
        self._refresh_action_buttons()

    def _setup_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(10)

        # Menu Bar
        menubar = self.menuBar()
        help_menu = menubar.addMenu("&Help")

        check_update_act = help_menu.addAction("Check for &Updates...")
        check_update_act.triggered.connect(self.check_for_updates)

        help_menu.addSeparator()

        about_act = help_menu.addAction("&About WhipIt")
        about_act.triggered.connect(self.show_about)

        # 1. Drive Selection Group
        drive_group = QGroupBox("Optical Drive & Archival Settings")
        drive_layout = QVBoxLayout(drive_group)

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Optical Drive:"))
        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(280)
        self.drive_combo.currentIndexChanged.connect(self._trigger_drive_poll)
        top_row.addWidget(self.drive_combo, stretch=1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_drives)
        top_row.addWidget(self.refresh_btn)

        self.disc_status_badge = QLabel("Checking...")
        self.disc_status_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.disc_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disc_status_badge.setMinimumWidth(145)
        self.disc_status_badge.setObjectName("statusBadge")
        top_row.addWidget(self.disc_status_badge)
        drive_layout.addLayout(top_row)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Read Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItem("Full Speed (Normal)", None)
        self.speed_combo.addItem("4x (Safe / Scratched Disc)", 4)
        self.speed_combo.addItem("8x (Moderate)", 8)
        self.speed_combo.addItem("12x", 12)
        self.speed_combo.addItem("16x", 16)
        self.speed_combo.addItem("24x", 24)
        opts_row.addWidget(self.speed_combo)

        opts_row.addSpacing(12)
        opts_row.addWidget(QLabel("Offset Override:"))
        self.offset_input = QLineEdit()
        self.offset_input.setPlaceholderText("Auto / Saved")
        self.offset_input.setFixedWidth(100)
        opts_row.addWidget(self.offset_input)

        opts_row.addSpacing(12)
        self.keep_going_chk = QCheckBox("Rescue readable tracks (--keep-going)")
        self.unknown_chk = QCheckBox("Allow unknown disc (-U)")
        self.overwrite_chk = QCheckBox("Overwrite existing")
        opts_row.addWidget(self.keep_going_chk)
        opts_row.addWidget(self.unknown_chk)
        opts_row.addWidget(self.overwrite_chk)
        opts_row.addStretch()
        drive_layout.addLayout(opts_row)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output Folder:"))
        self.output_edit = QLineEdit(str(get_output_root()))
        out_row.addWidget(self.output_edit, stretch=1)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_output_dir)
        out_row.addWidget(self.browse_btn)
        drive_layout.addLayout(out_row)

        root_layout.addWidget(drive_group)

        # 2. Pipeline Badges
        pipeline_frame = QFrame()
        pipeline_frame.setObjectName("pipelineFrame")
        pipeline_layout = QHBoxLayout(pipeline_frame)
        pipeline_layout.setContentsMargins(10, 8, 10, 8)
        pipeline_layout.setSpacing(8)

        for key, label_text, _ in PIPELINE_STEPS:
            badge = QLabel(label_text)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedHeight(30)
            badge.setObjectName("stepInactive")
            self.badge_labels[key] = badge
            pipeline_layout.addWidget(badge, stretch=1)

        root_layout.addWidget(pipeline_frame)

        # 3. Album, Tracklist & Progress Group
        progress_group = QGroupBox("Current Album & Progress")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setSpacing(10)

        top_info_widget = QWidget()
        top_info_layout = QHBoxLayout(top_info_widget)
        top_info_layout.setContentsMargins(0, 0, 0, 0)
        top_info_layout.setSpacing(14)

        # Left Column: Cover Art Card
        art_card = QWidget()
        art_card.setFixedWidth(146)
        art_card_layout = QVBoxLayout(art_card)
        art_card_layout.setContentsMargins(0, 0, 0, 0)
        art_card_layout.setSpacing(6)

        self.cover_art_label = QLabel()
        self.cover_art_label.setFixedSize(140, 140)
        self.cover_art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_art_label.setObjectName("coverArtLabel")
        self.cover_art_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_art_label.setToolTip("Click to select custom cover art")
        self.cover_art_label.mousePressEvent = lambda ev: self._choose_custom_art()
        self._render_cover_art_placeholder()
        art_card_layout.addWidget(self.cover_art_label, 0, Qt.AlignmentFlag.AlignHCenter)

        art_btn_row = QHBoxLayout()
        art_btn_row.setSpacing(4)
        self.change_art_btn = QPushButton("Change...")
        self.change_art_btn.setFixedHeight(26)
        self.change_art_btn.setStyleSheet("padding: 2px 6px; font-size: 11px;")
        self.change_art_btn.setToolTip("Select custom cover art from file")
        self.change_art_btn.clicked.connect(self._choose_custom_art)
        art_btn_row.addWidget(self.change_art_btn)

        self.reset_art_btn = QPushButton("Reset")
        self.reset_art_btn.setFixedHeight(26)
        self.reset_art_btn.setStyleSheet("padding: 2px 6px; font-size: 11px;")
        self.reset_art_btn.setToolTip("Reset to original Cover Art Archive image")
        self.reset_art_btn.setVisible(False)
        self.reset_art_btn.clicked.connect(self._reset_cover_art)
        art_btn_row.addWidget(self.reset_art_btn)

        art_card_layout.addLayout(art_btn_row)
        art_card_layout.addStretch()
        top_info_layout.addWidget(art_card)

        # Right Column: Album details, edition selector, external links, phase
        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(6)

        meta_row = QHBoxLayout()
        self.album_title_lbl = QLabel("Ready to rip Audio CD")
        self.album_title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        self.album_title_lbl.setFont(title_font)
        meta_row.addWidget(self.album_title_lbl, stretch=1)

        self.verification_lbl = QLabel("")
        self.verification_lbl.setTextFormat(Qt.TextFormat.PlainText)
        meta_row.addWidget(self.verification_lbl)
        meta_col.addLayout(meta_row)

        self.release_combo_row = QWidget()
        rel_layout = QHBoxLayout(self.release_combo_row)
        rel_layout.setContentsMargins(0, 0, 0, 0)
        rel_layout.addWidget(QLabel("Select Pressing:"))
        self.release_combo = QComboBox()
        self.release_combo.currentIndexChanged.connect(self._on_release_combo_changed)
        rel_layout.addWidget(self.release_combo, stretch=1)
        meta_col.addWidget(self.release_combo_row)
        self.release_combo_row.setVisible(False)

        self.external_links_lbl = QLabel("")
        self.external_links_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.external_links_lbl.setOpenExternalLinks(False)
        self.external_links_lbl.linkActivated.connect(self._on_external_link_clicked)
        self.external_links_lbl.setVisible(False)
        meta_col.addWidget(self.external_links_lbl)

        self.phase_detail_lbl = QLabel("Insert an Audio CD and click Identify Disc or Start Rip.")
        self.phase_detail_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.phase_detail_lbl.setWordWrap(True)
        meta_col.addWidget(self.phase_detail_lbl)
        meta_col.addStretch()

        top_info_layout.addLayout(meta_col, stretch=1)
        progress_layout.addWidget(top_info_widget)

        # Pre-rip tracklist table
        self.track_table = QTableWidget()
        self.track_table.setColumnCount(3)
        self.track_table.setHorizontalHeaderLabels(["#", "Track Title", "Duration"])
        self.track_table.setAlternatingRowColors(True)
        self.track_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.track_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.verticalHeader().setDefaultSectionSize(26)
        self.track_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.track_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.track_table.setMinimumHeight(220)
        self.track_table.setMaximumHeight(360)
        self.track_table.setVisible(False)
        progress_layout.addWidget(self.track_table, stretch=1)

        form_prog = QFormLayout()
        self.rip_progress_bar = QProgressBar()
        self.rip_progress_bar.setRange(0, 100)
        self.rip_progress_bar.setValue(0)
        self.rip_progress_bar.setFormat("Idle")
        form_prog.addRow("CD Extraction:", self.rip_progress_bar)

        self.opus_progress_bar = QProgressBar()
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat("Waiting...")
        form_prog.addRow("Opus 192k Mirror:", self.opus_progress_bar)
        progress_layout.addLayout(form_prog)

        root_layout.addWidget(progress_group)

        # 4. Action Controls
        controls_row = QHBoxLayout()

        self.identify_btn = QPushButton("Identify Disc")
        self.identify_btn.setMinimumHeight(38)
        self.identify_btn.clicked.connect(self.identify_disc)
        controls_row.addWidget(self.identify_btn, stretch=1)

        self.start_btn = QPushButton("Start Rip")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.clicked.connect(self.start_rip)
        controls_row.addWidget(self.start_btn, stretch=2)

        self.eject_btn = QPushButton("Eject Drive")
        self.eject_btn.setMinimumHeight(38)
        self.eject_btn.clicked.connect(self.eject_selected_drive)
        controls_row.addWidget(self.eject_btn, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerButton")
        self.cancel_btn.setMinimumHeight(38)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_rip)
        controls_row.addWidget(self.cancel_btn, stretch=1)

        root_layout.addLayout(controls_row)

        # 5. Live Console
        console_header = QHBoxLayout()
        self.console_toggle_btn = QToolButton()
        self.console_toggle_btn.setText("▼ Hide Live Whipper & Activity Console")
        self.console_toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.console_toggle_btn.setCheckable(True)
        self.console_toggle_btn.setChecked(True)
        self.console_toggle_btn.toggled.connect(self._toggle_console)
        console_header.addWidget(self.console_toggle_btn)
        console_header.addStretch()

        clear_btn = QPushButton("Clear Log")
        clear_btn.setFixedWidth(90)
        clear_btn.clicked.connect(lambda: self.console_box.clear())
        console_header.addWidget(clear_btn)
        root_layout.addLayout(console_header)

        self.console_box = QPlainTextEdit()
        self.console_box.setReadOnly(True)
        self.console_box.setMaximumBlockCount(3000)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(9)
        self.console_box.setFont(mono)
        root_layout.addWidget(self.console_box, stretch=1)

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QMenuBar {
                background-color: #1b2030;
                color: #e4e8f0;
                border-bottom: 1px solid #2b3248;
                padding: 2px 4px;
            }
            QMenuBar::item:selected {
                background-color: #262d42;
                border-radius: 3px;
            }
            QMenu {
                background-color: #1b2030;
                color: #eceff4;
                border: 1px solid #2b3248;
            }
            QMenu::item:selected {
                background-color: #0e7490;
                color: #ffffff;
            }
            QMainWindow {
                background-color: #12151f;
                color: #e4e8f0;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #2b3248;
                border-radius: 6px;
                margin-top: 8px;
                padding-top: 10px;
                color: #e4e8f0;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }
            QLabel, QCheckBox {
                color: #d8dee9;
            }
            QLabel#coverArtLabel {
                background-color: #161a26;
                border: 2px dashed #353e5a;
                border-radius: 8px;
                color: #6e7891;
                font-size: 11px;
            }
            QLabel#coverArtLabel:hover {
                border-color: #06b6d4;
            }
            QLineEdit, QComboBox, QPlainTextEdit {
                background-color: #1b2030;
                color: #eceff4;
                border: 1px solid #353e5a;
                border-radius: 4px;
                padding: 4px 6px;
            }
            QTableWidget {
                background-color: #161a26;
                alternate-background-color: #1c2233;
                color: #e4e8f0;
                border: 1px solid #2b3248;
                border-radius: 4px;
                gridline-color: #242c42;
            }
            QHeaderView::section {
                background-color: #1f273b;
                color: #22d3ee;
                padding: 4px 8px;
                font-weight: bold;
                border: none;
                border-bottom: 1px solid #353e5a;
            }
            QPushButton, QToolButton {
                background-color: #262d42;
                color: #eceff4;
                border: 1px solid #3b4564;
                border-radius: 5px;
                padding: 6px 12px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #323b56;
            }
            QPushButton:disabled {
                background-color: #1a1e2b;
                color: #59627a;
                border-color: #252b3d;
            }
            QPushButton#primaryButton {
                background-color: #0e7490;
                border-color: #06b6d4;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton#primaryButton:hover {
                background-color: #0891b2;
            }
            QPushButton#primaryButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }
            QPushButton#dangerButton {
                background-color: #881337;
                border-color: #e11d48;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton#dangerButton:hover {
                background-color: #9f1239;
            }
            QPushButton#dangerButton:disabled {
                background-color: #1e293b;
                border-color: #334155;
                color: #64748b;
            }
            QProgressBar {
                border: 1px solid #353e5a;
                border-radius: 4px;
                background-color: #1b2030;
                text-align: center;
                color: #ffffff;
                min-height: 20px;
            }
            QProgressBar::chunk {
                background-color: #06b6d4;
                border-radius: 3px;
            }
            QLabel#stepInactive {
                background-color: #1b2030;
                color: #6e7891;
                border: 1px solid #2b3248;
                border-radius: 5px;
                font-weight: 600;
            }
            QLabel#stepActive {
                background-color: #0e7490;
                color: #ffffff;
                border: 1px solid #22d3ee;
                border-radius: 5px;
                font-weight: bold;
            }
            QLabel#stepDone {
                background-color: #065f46;
                color: #a7f3d0;
                border: 1px solid #10b981;
                border-radius: 5px;
                font-weight: bold;
            }
            QLabel#stepError {
                background-color: #7f1d1d;
                color: #fecaca;
                border: 1px solid #ef4444;
                border-radius: 5px;
                font-weight: bold;
            }
            """
        )

    def _connect_signals(self) -> None:
        sig = self.adapter.signaler
        sig.log_emitted.connect(self._on_log)
        sig.state_changed.connect(self._on_state_changed)
        sig.whipper_output.connect(self._on_whipper_output)
        sig.transcode_progress.connect(self._on_transcode_progress)
        sig.disc_spinup_poll.connect(self._on_disc_spinup_poll)
        sig.prompt_requested.connect(self._on_prompt_requested)

    def _startup_checks(self) -> None:
        out_root = Path(self.output_edit.text().strip() or str(get_output_root())).expanduser().resolve()
        self.startup_worker = StartupCheckWorker(self.adapter, out_root, parent=None)
        _track_detached_worker(self.startup_worker)
        self.startup_worker.checks_completed.connect(self._on_startup_checks_completed)
        self.startup_worker.finished.connect(self._clear_startup_worker)
        self.startup_worker.start()
        self._refresh_action_buttons()

    def _clear_startup_worker(self) -> None:
        self.startup_worker = None
        self._refresh_action_buttons()

    def _on_startup_checks_completed(
        self,
        all_ok: bool,
        missing: List[str],
        preserved_jobs: List[Tuple[JobManifest, str]],
        cleanup_actions: List[str],
    ) -> None:
        self._refresh_action_buttons()
        if not all_ok and missing:
            self.phase_detail_lbl.setText(
                f"⚠ Missing required dependencies: {', '.join(missing)}. Ripping is disabled."
            )

        for act in cleanup_actions:
            self._append_console(f"[STARTUP] {act}")

        store = ManifestStore()
        coordinator = RecoveryCoordinator(store, ui=self.adapter)

        for manifest, status in preserved_jobs:
            user_choice = self._dialog_recover_job(manifest, status)
            if user_choice == "resume":
                self._start_resume_worker(coordinator, manifest)
                break  # Resume one job at a time
            elif user_choice == "discard":
                self._start_discard_worker(coordinator, manifest.job_id, manifest.metadata.album_stem)

    def _start_discard_worker(self, coordinator: RecoveryCoordinator, job_id: str, album_stem: str) -> None:
        self.phase_detail_lbl.setText(f"Discarding candidate: {album_stem}...")
        self._append_console(f"[RECOVERY] Discarding candidate: {album_stem}...")
        self.discard_worker = DiscardWorker(coordinator, job_id, album_stem, parent=None)
        _track_detached_worker(self.discard_worker)
        self.discard_worker.discard_finished.connect(self._on_discard_finished)
        self.discard_worker.finished.connect(self._clear_discard_worker)
        self.discard_worker.start()

    def _clear_discard_worker(self) -> None:
        self.discard_worker = None
        self._refresh_action_buttons()

    def _on_discard_finished(self, ok: bool, actions: List[str], album_stem: str) -> None:
        for act in actions:
            self._append_console(f"[RECOVERY] {act}")
        self.phase_detail_lbl.setText(f"✓ Discarded candidate: {album_stem}")

    def _dialog_recover_job(self, manifest: JobManifest, status: str) -> str:
        dlg = QDialog(self)
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

    def _start_resume_worker(self, coordinator: RecoveryCoordinator, manifest: JobManifest) -> None:
        self._set_controls_running(True)
        stem = manifest.metadata.album_stem
        self.phase_detail_lbl.setText(f"Resuming publication for {stem}...")
        self._append_console(f"[RECOVERY] Resuming publication for {stem}...")

        self.resume_worker = ResumeWorker(coordinator, manifest.job_id, self.adapter, parent=None)
        _track_detached_worker(self.resume_worker)
        self.resume_worker.resume_finished.connect(
            lambda ok, acts, s=stem: self._on_resume_finished(ok, acts, s)
        )
        self.resume_worker.start()

    def _on_resume_finished(self, success: bool, actions: List[str], album_stem: str) -> None:
        self._set_controls_running(False)
        for act in actions:
            self._append_console(f"[RECOVERY] {act}")
        if success:
            self.phase_detail_lbl.setText(f"✓ Successfully published: {album_stem}")
            self._set_pipeline_step("COMMITTED")
        else:
            self.phase_detail_lbl.setText(f"⚠ Failed resuming publication: {album_stem}")
        self._trigger_drive_poll()

    def refresh_drives(self) -> None:
        current_dev = self.drive_combo.currentData()
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()

        drives = get_drives_with_hardware_ids()
        if not drives:
            self.drive_combo.addItem("No optical drives detected", None)
        else:
            for dev, hw in drives:
                self.drive_combo.addItem(f"{dev} ({hw})", dev)

        if current_dev:
            idx = self.drive_combo.findData(current_dev)
            if idx >= 0:
                self.drive_combo.setCurrentIndex(idx)

        self.drive_combo.blockSignals(False)
        self._trigger_drive_poll()

    def _clear_poll_worker(self) -> None:
        self.poll_worker = None

    def _trigger_drive_poll(self) -> None:
        if _is_worker_running(self.worker) or _is_worker_running(self.identify_worker):
            return

        if _is_worker_running(self.poll_worker):
            return

        dev = self.drive_combo.currentData()
        if not dev:
            self.disc_status_badge.setText("No Drive")
            self.disc_status_badge.setStyleSheet(
                "background-color: #7f1d1d; color: #fecaca; border-radius: 4px; padding: 4px; font-weight: bold;"
            )
            return

        self.poll_worker = DrivePollWorker(dev, parent=None)
        _track_detached_worker(self.poll_worker)
        self.poll_worker.status_polled.connect(self._on_drive_status_polled)
        self.poll_worker.finished.connect(self._clear_poll_worker)
        self.poll_worker.start()

    def _on_drive_status_polled(self, dev: str, drive_st: int, disc_st: int) -> None:
        current_dev = self.drive_combo.currentData()
        if dev != current_dev:
            return

        from whipit.config import (
            CDS_AUDIO,
            CDS_DATA_1,
            CDS_DATA_2,
            CDS_DISC_OK,
            CDS_MIXED,
            CDS_XA_2_1,
            CDS_XA_2_2,
        )

        has_audio = (drive_st == CDS_DISC_OK) and (disc_st in (CDS_AUDIO, CDS_MIXED))
        is_data_disc = (drive_st == CDS_DISC_OK) and (
            disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2)
        )

        if has_audio:
            if self._disc_was_ejected:
                self._disc_was_ejected = False
                self._reset_disc_state(clear_user_art=True)
                self._reset_pipeline_badges()
            disc_name = "Enhanced CD" if disc_st == CDS_MIXED else "Audio CD"
            self.disc_status_badge.setText(f"{disc_name} Loaded")
            self.disc_status_badge.setStyleSheet(
                "background-color: #065f46; color: #a7f3d0; border-radius: 4px; padding: 4px; font-weight: bold;"
            )
            if self._current_step_key != "COMMITTED":
                self._set_pipeline_step("DISC_DETECTED")
        elif is_data_disc:
            self._disc_was_ejected = True
            self.disc_status_badge.setText("Data / DVD Disc")
            self.disc_status_badge.setStyleSheet(
                "background-color: #92400e; color: #fef3c7; border-radius: 4px; padding: 4px; font-weight: bold;"
            )
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()
        elif drive_st == CDS_DISC_OK:
            self.disc_status_badge.setText("Disc Loaded")
            self.disc_status_badge.setStyleSheet(
                "background-color: #1e3a5f; color: #bfdbfe; border-radius: 4px; padding: 4px; font-weight: bold;"
            )
        else:
            self._disc_was_ejected = True
            self.disc_status_badge.setText("Tray Empty")
            self.disc_status_badge.setStyleSheet(
                "background-color: #374151; color: #d1d5db; border-radius: 4px; padding: 4px; font-weight: bold;"
            )
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()

    def _browse_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Archival Output Directory",
            self.output_edit.text(),
        )
        if folder:
            self.output_edit.setText(folder)

    def _toggle_console(self, checked: bool) -> None:
        self.console_box.setVisible(checked)
        self.console_toggle_btn.setText(
            "▼ Hide Live Whipper & Activity Console" if checked else "▶ Show Live Whipper & Activity Console"
        )

    def _append_console(self, text: str) -> None:
        self.console_box.moveCursor(QTextCursor.MoveOperation.End)
        self.console_box.insertPlainText(text if text.endswith("\n") else f"{text}\n")
        self.console_box.moveCursor(QTextCursor.MoveOperation.End)

    def _reset_pipeline_badges(self) -> None:
        self._current_step_key = None
        for badge in self.badge_labels.values():
            badge.setObjectName("stepInactive")
            badge.style().unpolish(badge)
            badge.style().polish(badge)

    def _set_pipeline_step(self, active_key: str, error: bool = False) -> None:
        self._current_step_key = active_key
        found_active = False
        for key, _, _ in PIPELINE_STEPS:
            badge = self.badge_labels[key]
            if key == active_key:
                badge.setObjectName("stepError" if error else "stepActive")
                found_active = True
            elif not found_active:
                badge.setObjectName("stepDone")
            else:
                badge.setObjectName("stepInactive")
            badge.style().unpolish(badge)
            badge.style().polish(badge)

    @staticmethod
    def _is_safe_url(url: Optional[str]) -> bool:
        return is_strict_https_url(url, ("musicbrainz.org", "discogs.com"))

    def _update_links_display(self) -> None:
        links = []
        if self._current_mb_url and self._is_safe_url(self._current_mb_url):
            esc_mb = html.escape(self._current_mb_url, quote=True)
            links.append(
                f'<a href="{esc_mb}" style="color: #22d3ee; text-decoration: none; font-weight: 500;">↗ MusicBrainz</a>'
            )
        if self._current_discogs_url and self._is_safe_url(self._current_discogs_url):
            esc_discogs = html.escape(self._current_discogs_url, quote=True)
            links.append(
                f'<a href="{esc_discogs}" style="color: #a7f3d0; text-decoration: none; font-weight: 500;">↗ Discogs</a>'
            )
        if self._current_mb_lookup_url and self._is_safe_url(self._current_mb_lookup_url):
            esc_lookup = html.escape(self._current_mb_lookup_url, quote=True)
            label = "↗ Attach Disc ID to MusicBrainz" if self._current_mb_url else "↗ Add this Disc to MusicBrainz"
            links.append(
                f'<a href="{esc_lookup}" style="color: #38bdf8; text-decoration: none; font-weight: 500;">{html.escape(label)}</a>'
            )
            links.append(
                '<a href="#copy-mb-note" style="color: #f472b6; text-decoration: none; font-weight: 500;">'
                '📋 Copy Submission Note</a>'
            )

        if links:
            sep = '<span style="color: #4b5563;"> &nbsp;•&nbsp; </span>'
            self.external_links_lbl.setText(sep.join(links))
            self.external_links_lbl.setVisible(True)
        else:
            self.external_links_lbl.setVisible(False)

    def _on_external_link_clicked(self, url: str) -> None:
        if url == "#copy-mb-note":
            self.copy_musicbrainz_note()
        elif self._is_safe_url(url):
            QDesktopServices.openUrl(QUrl(url))
        else:
            self._append_console(f"[WARN] Blocked opening untrusted external URL: {url}")

    def copy_musicbrainz_note(self) -> None:
        dev = self.drive_combo.currentData() or "/dev/cdrom"
        hw_name = get_drive_hardware_id(dev)

        lines = [
            "Physical CD in hand.",
            f"- Disc ID extracted from optical drive TOC via WhipIt v{__version__} (Whipper backend).",
            f"- Optical Drive: {hw_name}.",
        ]

        if hasattr(self, "_last_inspection") and self._last_inspection:
            insp = self._last_inspection
            if insp.tracks_count:
                dur_str = f", total duration {insp.duration}" if insp.duration else ""
                lines.append(f"- {insp.tracks_count} audio tracks{dur_str}.")
            if insp.barcode:
                lines.append(f"- Barcode: {insp.barcode}")
            if insp.cat_no:
                lines.append(f"- Catalog number: {insp.cat_no}")

        lines.append("- Track timings matched against disc TOC.")

        if self._current_discogs_url:
            lines.append(f"- Discogs release reference: {self._current_discogs_url}")

        note_text = "\n".join(lines)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(note_text)
        self.phase_detail_lbl.setText("✓ MusicBrainz edit note copied to clipboard!")
        self._append_console("[OK] MusicBrainz submission note copied to clipboard.")

    def _reset_disc_state(self, clear_user_art: bool = True) -> None:
        """Clears all metadata, track preview, links, and cover art from any previous disc."""
        self._scraped_artist = None
        self._scraped_title = None
        self._ripping_started = False
        self._current_mbid = None
        self._current_mb_url = None
        self._current_mb_lookup_url = None
        self._current_discogs_url = None
        self._last_inspection = None
        self._matching_releases.clear()
        self._requested_mbids.clear()
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        self.release_combo.blockSignals(False)
        self.release_combo_row.setVisible(False)
        self.external_links_lbl.setText("")
        self.external_links_lbl.setVisible(False)
        self.track_table.setRowCount(0)
        self.track_table.setVisible(False)
        self.album_title_lbl.setText("Ready to rip Audio CD")
        self.verification_lbl.setText("")

        if clear_user_art or not self._art_is_user_selected:
            self._art_is_user_selected = False
            self._current_art_path = None
            self._downloaded_art_path = None
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._render_cover_art_placeholder()

    def _render_cover_art_placeholder(self, text: str = "No Cover Art\n\nClick to add") -> None:
        self.cover_art_label.clear()
        self.cover_art_label.setText(text)
        self.cover_art_label.setToolTip("Click to select custom cover art")

    def _render_cover_art(self, art_path: Path) -> None:
        if not art_path.exists():
            self._render_cover_art_placeholder()
            return

        from PyQt6.QtGui import QImageReader

        if hasattr(QImageReader, "setAllocationLimit"):
            QImageReader.setAllocationLimit(128)

        reader = QImageReader(str(art_path))
        sz = reader.size()
        if not sz.isValid() or sz.width() * sz.height() > 50_000_000:
            self._render_cover_art_placeholder()
            return

        pix = QPixmap.fromImageReader(reader) if hasattr(QPixmap, "fromImageReader") else QPixmap(str(art_path))
        if pix.isNull():
            self._render_cover_art_placeholder()
            return
        scaled = pix.scaled(
            136,
            136,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_art_label.setPixmap(scaled)
        self.cover_art_label.setToolTip(f"Cover Art: {art_path.name}\n(Click to replace)")

    def _fetch_cover_art(self, mbid: str) -> None:
        if self._art_is_user_selected:
            return
        if _is_worker_running(self.cover_worker):
            self.cover_worker.cancel()
        self.cover_worker = CoverArtWorker(mbid, parent=None)
        _track_detached_worker(self.cover_worker)
        self.cover_worker.cover_found.connect(self._on_cover_art_found)
        self.cover_worker.cover_failed.connect(self._on_cover_art_failed)
        self.cover_worker.finished.connect(self._clear_cover_worker)
        self.cover_worker.start()

    def _clear_cover_worker(self) -> None:
        self.cover_worker = None

    def _on_cover_art_found(self, mbid: str, image_path: str) -> None:
        path = Path(image_path)
        self._downloaded_art_path = path
        if not self._art_is_user_selected:
            self._current_art_path = path
            self._render_cover_art(path)
            self.adapter.set_cover_art(path)
            self._append_console(f"[OK] Cover Art Archive thumbnail loaded ({path.name})")

    def _on_cover_art_failed(self, mbid: str) -> None:
        if not self._art_is_user_selected:
            self._current_art_path = None
            self._downloaded_art_path = None
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._render_cover_art_placeholder()
            self._append_console(f"[INFO] No cover art found in Cover Art Archive for release {mbid}")

    def _choose_custom_art(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Album Cover Art",
            "",
            "Image Files (*.jpg *.jpeg *.png *.webp);;All Files (*)",
        )
        if not file_path:
            return
        chosen_path = Path(file_path)
        if not chosen_path.exists() or not chosen_path.is_file():
            return

        if chosen_path.stat().st_size > 30 * 1024 * 1024:
            QMessageBox.warning(self, "Invalid Image", "Cover art must be 30 MB or smaller.")
            return

        from whipit.audio.metadata import _image_ext
        try:
            with open(chosen_path, "rb") as fh:
                sniffed = _image_ext(fh.read(16))
        except OSError:
            sniffed = None
        suffix = chosen_path.suffix.lower().replace(".jpeg", ".jpg")

        from PyQt6.QtGui import QImageReader

        if hasattr(QImageReader, "setAllocationLimit"):
            QImageReader.setAllocationLimit(128)

        reader = QImageReader(str(chosen_path))
        sz = reader.size()
        if not sz.isValid() or sz.width() * sz.height() > 50_000_000:
            QMessageBox.warning(self, "Invalid Image", "The selected file is not a valid image or exceeds dimension limits.")
            return

        pix = QPixmap.fromImageReader(reader) if hasattr(QPixmap, "fromImageReader") else QPixmap(str(chosen_path))
        if pix.isNull():
            QMessageBox.warning(self, "Invalid Image", "The selected file is not a valid image format.")
            return
        if sniffed is None or sniffed != suffix:
            from whipit.config import get_cache_dir
            covers_dir = get_cache_dir() / "covers"
            covers_dir.mkdir(parents=True, exist_ok=True)
            conv_path = covers_dir / f"custom_{chosen_path.stem}.jpg"
            pix.toImage().save(str(conv_path), "JPEG", 95)
            target_path = conv_path
        else:
            target_path = chosen_path
        self._current_art_path = target_path
        self._art_is_user_selected = True
        self._render_cover_art(target_path)
        self.adapter.set_cover_art(target_path)
        self.reset_art_btn.setVisible(bool(self._downloaded_art_path))
        self._append_console(f"[INFO] Cover art set to: {chosen_path.name}")
        self.phase_detail_lbl.setText(f"✓ Cover art selected: {chosen_path.name}")

    def _reset_cover_art(self) -> None:
        if self._downloaded_art_path and self._downloaded_art_path.exists():
            self._current_art_path = self._downloaded_art_path
            self._art_is_user_selected = False
            self._render_cover_art(self._current_art_path)
            self.adapter.set_cover_art(self._current_art_path)
            self.reset_art_btn.setVisible(False)
            self._append_console("[INFO] Reverted to Cover Art Archive original.")
            self.phase_detail_lbl.setText("✓ Restored Cover Art Archive image.")
        else:
            self._current_art_path = None
            self._art_is_user_selected = False
            self._render_cover_art_placeholder()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._append_console("[INFO] Cleared cover art.")

    def _populate_track_table(self, tracks: List[TrackInfo]) -> None:
        if not tracks:
            self.track_table.setVisible(False)
            return

        self.track_table.setRowCount(len(tracks))
        for row, tr in enumerate(tracks):
            item_num = QTableWidgetItem(f"{tr.number:02d}")
            item_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_title = QTableWidgetItem(tr.title)
            item_dur = QTableWidgetItem(tr.duration or "")
            item_dur.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.track_table.setItem(row, 0, item_num)
            self.track_table.setItem(row, 1, item_title)
            self.track_table.setItem(row, 2, item_dur)

        self.track_table.setVisible(True)

    def _on_details_resolved(self, mbid: str, discogs_url: str, tracks: List[TrackInfo]) -> None:
        if self._current_mb_url and mbid in self._current_mb_url:
            if discogs_url:
                self._current_discogs_url = discogs_url
                self._update_links_display()
            if tracks:
                self._populate_track_table(tracks)

    def _clear_details_worker(self) -> None:
        self.details_worker = None

    def _start_release_details_lookup(
        self,
        mbid: str,
        barcode: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        expected_tracks: Optional[int] = None,
    ) -> None:
        if _is_worker_running(self.details_worker):
            self.details_worker.cancel()

        self.details_worker = ReleaseDetailsWorker(
            mbid,
            barcode=barcode,
            artist=artist,
            title=title,
            expected_tracks=expected_tracks,
            parent=None,
        )
        _track_detached_worker(self.details_worker)
        self.details_worker.details_found.connect(self._on_details_resolved)
        self.details_worker.finished.connect(self._clear_details_worker)
        self.details_worker.start()

    def _on_release_combo_changed(self, idx: int) -> None:
        if not self._matching_releases or idx < 0 or idx >= len(self._matching_releases):
            return
        rel = self._matching_releases[idx]
        self.album_title_lbl.setText(f"{rel.artist} — {rel.title}")
        self._current_mbid = rel.mbid
        self._current_mb_url = f"https://musicbrainz.org/release/{rel.mbid}"
        self._current_discogs_url = None
        self._update_links_display()
        self._populate_track_table(rel.tracks)
        if not rel.tracks and rel.mbid not in self._requested_mbids:
            self._requested_mbids.add(rel.mbid)
            self._start_release_details_lookup(rel.mbid, barcode=rel.barcode, artist=rel.artist, title=rel.title)
        if rel.mbid and not self._art_is_user_selected:
            self._fetch_cover_art(rel.mbid)

    def _clear_identify_worker(self) -> None:
        self.identify_worker = None
        self._refresh_action_buttons()

    def identify_disc(self) -> None:
        if _is_worker_running(self.worker) or _is_worker_running(self.identify_worker):
            return

        missing = get_missing_dependencies()
        if missing:
            QMessageBox.critical(
                self,
                "Missing Dependencies",
                f"Cannot proceed. Missing required system tools:\n  {', '.join(missing)}\n\n"
                f"Please install them via your system package manager.",
            )
            self._refresh_action_buttons()
            return

        dev = self.drive_combo.currentData()
        if not dev:
            QMessageBox.warning(self, "No Drive", "Please select an optical drive first.")
            return

        self._reset_disc_state(clear_user_art=True)
        self._set_controls_running(True)
        self.phase_detail_lbl.setText(f"Inspecting Table of Contents on {dev}...")
        self._append_console(f"[INFO] Inspecting disc in {dev}...")

        self.identify_worker = IdentifyWorker(dev, parent=None)
        _track_detached_worker(self.identify_worker)
        self.identify_worker.line_received.connect(self._append_console)
        self.identify_worker.inspection_completed.connect(self._on_identify_finished)
        self.identify_worker.finished.connect(self._clear_identify_worker)
        self.identify_worker.start()

    def _on_identify_finished(self, result: DiscInspectionResult) -> None:
        self._set_controls_running(False)
        self._current_mb_lookup_url = result.mb_lookup_url
        self._last_inspection = result

        if result.success and (result.artist != "Unknown Artist" or result.mb_url):
            self.album_title_lbl.setText(f"{result.artist} — {result.title}")

            details = []
            if result.tracks_count:
                details.append(f"{result.tracks_count} Tracks")
            if result.duration:
                details.append(f"Duration: {result.duration}")
            if result.cat_no:
                details.append(f"Cat: {result.cat_no}")
            if result.barcode:
                details.append(f"Barcode: {result.barcode}")

            status_text = " • ".join(details) if details else "Disc identified."
            self.phase_detail_lbl.setText(f"✓ {status_text} (Ready to rip)")
            self._set_pipeline_step("DISC_DETECTED")

            self._current_mbid = result.mbid
            self._current_mb_url = result.mb_url
            self._current_discogs_url = result.discogs_url
            self._update_links_display()
            self._populate_track_table(result.tracks)

            if result.matching_releases and len(result.matching_releases) > 1:
                self._matching_releases = list(result.matching_releases)
                self.release_combo.blockSignals(True)
                self.release_combo.clear()
                for rel in self._matching_releases:
                    det = []
                    if rel.country:
                        det.append(rel.country)
                    if rel.cat_no:
                        det.append(f"Cat: {rel_cat if (rel_cat := rel.cat_no) else ''}")
                    extra = f" [{', '.join(det)}]" if det else ""
                    self.release_combo.addItem(f"{rel.title}{extra}")
                self.release_combo.blockSignals(False)
                self.release_combo_row.setVisible(True)
                if self._matching_releases[0].mbid and not self._art_is_user_selected:
                    self._fetch_cover_art(self._matching_releases[0].mbid)
            else:
                self.release_combo_row.setVisible(False)
                if result.mbid and not self._art_is_user_selected:
                    self._fetch_cover_art(result.mbid)
        else:
            self.release_combo_row.setVisible(False)
            self.track_table.setVisible(False)
            self.album_title_lbl.setText(f"Uncataloged Audio CD ({result.tracks_count or '?'} Tracks)")
            if not self._art_is_user_selected:
                self._current_art_path = None
                self._downloaded_art_path = None
                self.adapter.set_cover_art(None)
                self.reset_art_btn.setVisible(False)
                self._render_cover_art_placeholder()
            self._current_mb_url = None
            self._current_discogs_url = None
            self._update_links_display()

            err = result.error_message or "Could not retrieve disc metadata from MusicBrainz."
            self.phase_detail_lbl.setText(f"⚠ {err}")
            self._append_console(f"[WARN] {err}")
            self._msg(
                QMessageBox.Icon.Information,
                "Disc Inspection",
                f"{err}\n\nYou can attach this CD to MusicBrainz using the link above, or rip using 'Allow unknown disc (-U)'.",
            )

    def _clear_rip_worker(self) -> None:
        self.worker = None
        self._refresh_action_buttons()

    def start_rip(self) -> None:
        if _is_worker_running(self.worker) or _is_worker_running(self.identify_worker):
            return

        missing = get_missing_dependencies()
        if missing:
            QMessageBox.critical(
                self,
                "Missing Dependencies",
                f"Cannot proceed. Missing required system tools:\n  {', '.join(missing)}\n\n"
                f"Please install them via your system package manager.",
            )
            self._refresh_action_buttons()
            return

        if self._current_step_key == "COMMITTED":
            self._reset_disc_state(clear_user_art=True)
            self._reset_pipeline_badges()

        dev = self.drive_combo.currentData()
        speed = self.speed_combo.currentData()

        manual_offset: Optional[int] = None
        offset_str = self.offset_input.text().strip()
        if offset_str:
            if not re.match(r"^[+-]?\d+$", offset_str):
                QMessageBox.warning(
                    self,
                    "Invalid Offset",
                    "Read offset override must be an integer (e.g. +6, 667, -12) or left blank.",
                )
                return
            manual_offset = int(offset_str)
            if len(offset_str) > 7 or not -10000 <= manual_offset <= 10000:
                QMessageBox.warning(
                    self,
                    "Invalid Offset",
                    "Read offset must be between -10000 and 10000.",
                )
                return

        output_path = Path(self.output_edit.text().strip() or str(get_output_root())).expanduser().resolve()

        selected_release_id: Optional[str] = None
        if self.release_combo_row.isVisible() and self._matching_releases:
            idx = self.release_combo.currentIndex()
            if 0 <= idx < len(self._matching_releases):
                selected_release_id = self._matching_releases[idx].mbid

        if selected_release_id and not self._current_art_path and not self._art_is_user_selected:
            self._fetch_cover_art(selected_release_id)

        self._current_mbid = selected_release_id
        if not selected_release_id:
            self._current_mb_url = None
            self._current_discogs_url = None
            self._current_mb_lookup_url = None
            self.external_links_lbl.setText("")
            self.external_links_lbl.setVisible(False)

        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat("Initializing Whipper...")
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat("Waiting for FLAC extraction...")
        self.verification_lbl.setText("")
        self._scraped_artist = None
        self._scraped_title = None
        self._ripping_started = False
        self.phase_detail_lbl.setText(f"Starting extraction on {dev or 'auto-detected drive'}...")

        self._set_controls_running(True)

        self.worker = RipWorker(
            ui_adapter=self.adapter,
            device=dev,
            speed=speed,
            manual_offset=manual_offset,
            release_id=selected_release_id,
            output_root=output_path,
            keep_going=self.keep_going_chk.isChecked(),
            unknown_disc=self.unknown_chk.isChecked(),
            overwrite=self.overwrite_chk.isChecked(),
            parent=None,
        )
        _track_detached_worker(self.worker)
        self.worker.workflow_finished.connect(self._on_workflow_finished)
        self.worker.finished.connect(self._clear_rip_worker)
        self.worker.start()

    def cancel_rip(self) -> None:
        if _is_worker_running(self.worker):
            ans = QMessageBox.question(
                self,
                "Cancel Rip",
                "Are you sure you want to cancel the active rip?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans == QMessageBox.StandardButton.Yes:
                self.phase_detail_lbl.setText("Cancelling active process...")
                self.cancel_btn.setEnabled(False)
                self.worker.cancel()

    def eject_selected_drive(self) -> None:
        dev = self.drive_combo.currentData()
        if not dev or _is_worker_running(self.eject_worker):
            return

        self._disc_was_ejected = True
        self.eject_btn.setEnabled(False)
        self.phase_detail_lbl.setText(f"Ejecting {dev}...")
        self._append_console(f"[INFO] Ejecting {dev}...")

        self.eject_worker = EjectWorker(dev, parent=None)
        _track_detached_worker(self.eject_worker)
        self.eject_worker.eject_finished.connect(self._on_eject_finished)
        self.eject_worker.finished.connect(self._clear_eject_worker)
        self.eject_worker.start()

    def _clear_eject_worker(self) -> None:
        self.eject_worker = None
        self._refresh_action_buttons()

    def _on_eject_finished(self, dev: str) -> None:
        self.phase_detail_lbl.setText(f"Ejected {dev}.")
        QTimer.singleShot(600, self._trigger_drive_poll)

    def _refresh_action_buttons(self) -> None:
        is_busy = (
            _is_worker_running(self.worker)
            or _is_worker_running(self.identify_worker)
            or _is_worker_running(self.startup_worker)
            or _is_worker_running(self.eject_worker)
            or _is_worker_running(self.resume_worker)
            or _is_worker_running(self.discard_worker)
        )
        if is_busy:
            self.identify_btn.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.eject_btn.setEnabled(False)
            self.cancel_btn.setEnabled(_is_worker_running(self.worker))
            return

        self.cancel_btn.setEnabled(False)
        self.eject_btn.setEnabled(True)

        missing = get_missing_dependencies()
        if missing:
            self.identify_btn.setEnabled(False)
            self.start_btn.setEnabled(False)
            self.phase_detail_lbl.setText(
                f"⚠ Missing required dependencies: {', '.join(missing)}. Ripping is disabled."
            )
            self.phase_detail_lbl.setStyleSheet("color: #f87171; font-weight: bold;")
        else:
            self.identify_btn.setEnabled(True)
            self.start_btn.setEnabled(True)
            self.phase_detail_lbl.setStyleSheet("")

    def _set_controls_running(self, running: bool) -> None:
        self.drive_combo.setEnabled(not running)
        self.refresh_btn.setEnabled(not running)
        self.speed_combo.setEnabled(not running)
        self.offset_input.setEnabled(not running)
        self.browse_btn.setEnabled(not running)
        self.release_combo.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self._refresh_action_buttons()

    def _on_log(self, level: LogLevel, message: str) -> None:
        prefix = f"[{level.value}]"
        self._append_console(f"{prefix} {message}")
        if level in (LogLevel.INFO, LogLevel.OK, LogLevel.WARN, LogLevel.HEADER):
            clean = message.strip("═ \n")
            if clean:
                self.phase_detail_lbl.setText(clean)

    def _on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        if manifest.metadata.album != "Unknown Album" or manifest.metadata.artist != "Unknown Artist":
            disc_suffix = f" (Disc {manifest.metadata.disc_number})" if manifest.metadata.disc_number else ""
            self.album_title_lbl.setText(
                f"{manifest.metadata.artist} — {manifest.metadata.album}{disc_suffix}"
            )

        if manifest.metadata.musicbrainz_url:
            self._current_mb_url = manifest.metadata.musicbrainz_url
        if manifest.metadata.discogs_url:
            self._current_discogs_url = manifest.metadata.discogs_url
        if manifest.metadata.musicbrainz_lookup_url:
            self._current_mb_lookup_url = manifest.metadata.musicbrainz_lookup_url

        self._update_links_display()

        if manifest.verification.accuraterip_summary != "Unknown":
            if manifest.verification.is_accurate:
                self.verification_lbl.setText("✓ AccurateRip Verified")
                self.verification_lbl.setStyleSheet("color: #34d399; font-weight: bold;")
            else:
                self.verification_lbl.setText(f"⚠ {manifest.verification.accuraterip_summary}")
                self.verification_lbl.setStyleSheet("color: #fbbf24; font-weight: bold;")

        for key, _, states in PIPELINE_STEPS:
            if state in states:
                self._set_pipeline_step(key)
                break

        if state == WorkflowState.RIP_EXTRACTED:
            self.rip_progress_bar.setRange(0, 100)
            self.rip_progress_bar.setValue(100)
            self.rip_progress_bar.setFormat("Extraction Complete (100%)")
        elif state in (WorkflowState.ROLLBACK_FAILED, WorkflowState.PUBLISH_FAILED):
            self._set_pipeline_step("PUBLISHING", error=True)
        elif state == WorkflowState.ABORTED:
            step_to_mark = self._current_step_key or "DISC_DETECTED"
            self._set_pipeline_step(step_to_mark, error=True)

    def _on_whipper_output(self, line: str) -> None:
        try:
            self.console_box.moveCursor(QTextCursor.MoveOperation.End)
            self.console_box.insertPlainText(line)
            self.console_box.moveCursor(QTextCursor.MoveOperation.End)

            m_lookup = re.search(r"MusicBrainz lookup URL\s*[:\s]\s*(https?://musicbrainz\.org/cdtoc/attach\S+)", line)
            if m_lookup:
                self._current_mb_lookup_url = m_lookup.group(1).strip()
                self._update_links_display()

            m_mb = _MB_REL_RE.search(line)
            if m_mb:
                mbid = m_mb.group(1).lower()
                if mbid not in self._requested_mbids:
                    self._requested_mbids.add(mbid)
                    self._current_mbid = mbid
                    self._current_mb_url = f"https://musicbrainz.org/release/{mbid}"
                    self._update_links_display()
                    self._start_release_details_lookup(mbid)
                if not self._art_is_user_selected and (not self._current_art_path or self._current_mbid != mbid):
                    self._current_mbid = mbid
                    self._fetch_cover_art(mbid)

            m_track = re.search(r"(?:Reading|Copying|Ripping)\s+track\s+(\d+)\s+of\s+(\d+)", line, re.I)
            if m_track:
                self._ripping_started = True
                cur_t, tot_t = int(m_track.group(1)), int(m_track.group(2))
                self.rip_progress_bar.setRange(0, tot_t)
                self.rip_progress_bar.setValue(cur_t - 1)
                self.rip_progress_bar.setFormat(f"Ripping Track {cur_t} of {tot_t}...")
                self.phase_detail_lbl.setText(f"Extracting Track {cur_t} / {tot_t} via cd-paranoia...")

            # Only scrape album-level headers before track extraction starts
            if not self._ripping_started and not re.search(r"\btrack\b", line, re.I):
                m_meta = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Artist\s*:\s*(.+)$", line)
                if m_meta:
                    self._scraped_artist = m_meta.group(1).strip()
                    if self._scraped_title:
                        self.album_title_lbl.setText(f"{self._scraped_artist} — {self._scraped_title}")
                    else:
                        self.album_title_lbl.setText(self._scraped_artist)

                m_rel = re.search(r"^\s*(?:\[?\w+\]?\s*(?:whipper\S*:\s*)?)?Title\s*:\s*(.+)$", line)
                if m_rel:
                    self._scraped_title = m_rel.group(1).strip()
                    if self._scraped_artist:
                        self.album_title_lbl.setText(f"{self._scraped_artist} — {self._scraped_title}")
                    else:
                        self.album_title_lbl.setText(self._scraped_title)
        except Exception:
            pass

    def _on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        self.opus_progress_bar.setRange(0, total)
        self.opus_progress_bar.setValue(completed)
        status = "OK" if success else "ERR"
        self.opus_progress_bar.setFormat(f"{completed}/{total} tracks ({track_name} [{status}])")
        self.phase_detail_lbl.setText(f"Transcoding Opus 192k: {completed} of {total} complete")

    def _on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        if ready is None:
            self.phase_detail_lbl.setText(f"Waiting for {device} ({hardware_id}) to spin up... ({elapsed:.1f}s)")
        elif ready is True:
            self.phase_detail_lbl.setText(f"Disc ready in {device} ({hardware_id}).")
        else:
            self.phase_detail_lbl.setText(f"Timeout waiting for disc in {device}.")

    def _on_workflow_finished(self, success: bool, message: str) -> None:
        self._set_controls_running(False)
        self.rip_progress_bar.setRange(0, 100)
        if success:
            self.rip_progress_bar.setValue(100)
            self.opus_progress_bar.setValue(self.opus_progress_bar.maximum() or 100)
            self._set_pipeline_step("COMMITTED")
            if "partial" in message.lower():
                self.rip_progress_bar.setFormat("Partial Extraction Complete (100%)")
            else:
                self.rip_progress_bar.setFormat("Complete (100%)")
            self.phase_detail_lbl.setText(message)
        else:
            self.rip_progress_bar.setFormat("Stopped")
            self.phase_detail_lbl.setText(message)
            if "cancel" in message.lower():
                self._reset_pipeline_badges()

        self._trigger_drive_poll()

    def _clear_update_worker(self) -> None:
        self.update_worker = None

    def check_for_updates(self) -> None:
        if _is_worker_running(self.worker):
            QMessageBox.warning(
                self,
                "Extraction Active",
                "Cannot check for updates while an extraction is in progress.",
            )
            return

        if _is_worker_running(self.update_worker):
            return

        self.phase_detail_lbl.setText("Checking for updates...")
        self._append_console("[INFO] Checking for updates...")

        self.update_worker = UpdateWorker(parent=None)
        _track_detached_worker(self.update_worker)
        self.update_worker.update_finished.connect(self._on_update_finished)
        self.update_worker.finished.connect(self._clear_update_worker)
        self.update_worker.start()

    def _on_update_finished(self, success: bool, message: str, restart_needed: bool) -> None:
        self.phase_detail_lbl.setText("Ready to rip Audio CD")
        if success:
            self._append_console(f"[OK] {message}")
            if restart_needed:
                QTimer.singleShot(150, lambda: self._prompt_restart_dialog(message))
            else:
                QTimer.singleShot(150, lambda: self._show_info_dialog("Check for Updates", message))
        else:
            self._append_console(f"[ERROR] {message}")
            QTimer.singleShot(150, lambda: self._show_warning_dialog("Update Failed", message))

    def _msg(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
    ) -> QMessageBox.StandardButton:
        box = QMessageBox(self)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        box.setStandardButtons(buttons)
        return box.exec()

    def _show_warning_dialog(self, title: str, message: str) -> None:
        self._msg(QMessageBox.Icon.Warning, title, message)

    def _show_info_dialog(self, title: str, message: str) -> None:
        self._msg(QMessageBox.Icon.Information, title, message)

    def _prompt_restart_dialog(self, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Update Complete")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"{message}\n\nWould you like to restart WhipIt now to apply the update?")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._restart_application()

    def _restart_application(self) -> None:
        exe = resolve_executable_path()
        if exe.endswith(" -m whipit"):
            QProcess.startDetached(exe[: -len(" -m whipit")], ["-m", "whipit"])
        else:
            QProcess.startDetached(exe, [])
        app = QApplication.instance()
        if app:
            app.quit()

    def show_about(self) -> None:
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
        box = QMessageBox(self)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setWindowTitle("About WhipIt")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(about_text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    # --- Thread-Bridged Modal Prompts ---

    def _on_prompt_requested(self, *args) -> None:
        if len(args) == 1 and hasattr(args[0], "prompt_type"):
            request = args[0]
            if request.is_resolved:
                return
            try:
                result = self._dispatch_modal_prompt(request.prompt_type, request.payload)
            except Exception as exc:
                self._append_console(f"[ERROR] Modal dialog error ({request.prompt_type}): {exc}")
                result = GuiInteractionAdapter._default_for_prompt(request.prompt_type)
            request.resolve(result)
        elif len(args) >= 3:
            prompt_type, payload, reply_queue = args[0], args[1], args[2]
            try:
                result = self._dispatch_modal_prompt(prompt_type, payload)
            except Exception as exc:
                self._append_console(f"[ERROR] Modal dialog error ({prompt_type}): {exc}")
                result = GuiInteractionAdapter._default_for_prompt(prompt_type)
            try:
                reply_queue.put_nowait(result)
            except Exception:
                pass

    def _dispatch_modal_prompt(self, prompt_type: str, payload: Any) -> Any:
        if prompt_type == "confirm_install_dependencies":
            missing, install_cmd, manager = payload
            res = self._msg(
                QMessageBox.Icon.Question,
                "Missing System Dependencies",
                f"The following required audio tools are missing:\n\n"
                f"  {', '.join(missing)}\n\n"
                f"Would you like to install them now using '{manager}'?\n\n"
                f"Command: {install_cmd}",
                buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return res == QMessageBox.StandardButton.Yes

        if prompt_type == "confirm_purge_stage":
            stage_dir, flac_count = payload
            res = self._msg(
                QMessageBox.Icon.Question,
                "Purge Orphaned Audio Files?",
                f"Found orphaned staging directory:\n  {stage_dir}\n\n"
                f"It contains {flac_count} extracted FLAC file(s) with no active job.\n\n"
                f"Do you want to permanently delete these audio files?",
                buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return res == QMessageBox.StandardButton.Yes

        if prompt_type == "select_loaded_drive":
            drives_with_hw: List[Tuple[str, str]] = payload
            items = [f"{d} ({hw})" for d, hw in drives_with_hw]
            choice, ok = QInputDialog.getItem(
                self,
                "Multiple Loaded Drives",
                "Multiple optical drives have an Audio CD loaded.\nSelect the drive to rip from:",
                items,
                0,
                False,
            )
            if ok and choice:
                idx = items.index(choice)
                return drives_with_hw[idx][0]
            return None

        if prompt_type == "prompt_select_release":
            releases: List[ReleaseOption] = payload
            items = []
            for r in releases:
                det = []
                if r.country:
                    det.append(r.country)
                if r.cat_no:
                    det.append(f"Cat: {rel_cat if (rel_cat := r.cat_no) else ''}")
                extra = f" [{', '.join(det)}]" if det else ""
                items.append(f"{r.title}{extra}")
            choice, ok = QInputDialog.getItem(
                self,
                "Select Pressing",
                f"MusicBrainz matched {len(releases)} releases for this disc:\nSelect your edition:",
                items,
                0,
                False,
            )
            if ok and choice:
                idx = items.index(choice)
                return releases[idx].mbid
            return None

        if prompt_type == "prompt_empty_multi_drives":
            drives_with_hw = payload
            return self._dialog_empty_multi_drives(drives_with_hw)

        if prompt_type == "prompt_insert_disc":
            device, hw_id, available_drives, allow_main_menu = payload
            return self._dialog_insert_disc(device, hw_id, available_drives, allow_main_menu)

        if prompt_type == "wait_for_user_reload":
            device, message = payload
            res = self._msg(
                QMessageBox.Icon.Information,
                f"Reload Optical Drive ({device})",
                message.strip(),
                buttons=QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            return res == QMessageBox.StandardButton.Ok

        if prompt_type == "prompt_extraction_speed":
            return self.speed_combo.currentData()

        if prompt_type == "prompt_calibration_start":
            device = payload
            return self._dialog_calibration_start(device)

        if prompt_type == "prompt_calibration_retry":
            device = payload
            return self._dialog_calibration_retry(device)

        if prompt_type == "prompt_cdr_detected":
            device = payload
            res = self._msg(
                QMessageBox.Icon.Question,
                "CD-R Media Detected",
                f"Whipper detected that the disc in {device} is a CD-R.\n\n"
                "Do you want to rip this CD-R anyway with --cdr?",
                buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            return res == QMessageBox.StandardButton.Yes

        if prompt_type == "prompt_unknown_disc":
            box = QMessageBox(self)
            box.setWindowTitle("Disc Not in MusicBrainz")
            box.setTextFormat(Qt.TextFormat.PlainText)
            msg_text = (
                "This disc was not found in the MusicBrainz catalog.\n\n"
                "You can submit this CD to MusicBrainz using its unique Table of Contents, "
                "or proceed ripping with generic tags ('Unknown Artist - Unknown Album')."
            )
            box.setText(msg_text)
            rip_btn = box.addButton("Rip as Unknown (-U)", QMessageBox.ButtonRole.AcceptRole)
            abort_btn = box.addButton("Abort", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            return box.clickedButton() == rip_btn

        if prompt_type == "prompt_extraction_error":
            device, available_drives = payload
            return self._dialog_extraction_error(device, available_drives)

        if prompt_type == "prompt_conflict_resolution":
            if isinstance(payload, tuple) and len(payload) == 3:
                flac_dir, opus_dir, warning = payload
            else:
                flac_dir, opus_dir = payload
                warning = None
            return self._dialog_conflict_resolution(flac_dir, opus_dir, warning)

        return GuiInteractionAdapter._default_for_prompt(prompt_type)

    def _dialog_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        dlg = QDialog(self)
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

    def _dialog_insert_disc(
        self,
        device: str,
        hw_id: str,
        available_drives: Optional[List[Tuple[str, str]]],
        allow_main_menu: bool,
    ) -> DiscPromptResult:
        dlg = QDialog(self)
        dlg.setWindowTitle("No Audio CD Detected")
        layout = QVBoxLayout(dlg)
        lbl = QLabel(f"No Audio CD detected in <b>{html.escape(device)}</b> ({html.escape(hw_id)}).")
        lbl.setTextFormat(Qt.TextFormat.RichText)
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

    def _dialog_calibration_start(self, device: str) -> CalibrationChoice:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Drive Calibration Wizard ({device})")
        layout = QVBoxLayout(dlg)
        lbl = QLabel(
            f"<b>Calibrating optical drive: {html.escape(device)}</b><br><br>"
            "To verify AccurateRip checksums, Whipper needs your drive's sample read offset.<br>"
            "Insert a popular commercial Audio CD to auto-detect, or enter a known offset below:"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
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

    def _dialog_calibration_retry(self, device: str) -> CalibrationChoice:
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Offset Detection Failed ({device})")
        layout = QVBoxLayout(dlg)
        layout.addWidget(
            QLabel(
                "AccurateRip offset auto-detection failed (disc not matched in database).\n"
                "How would you like to proceed?"
            )
        )

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

    def _dialog_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        dlg = QDialog(self)
        dlg.setWindowTitle("Extraction Halted — Read Error Recovery")
        layout = QVBoxLayout(dlg)
        lbl = QLabel(
            f"<b>Extraction on {html.escape(device)} halted due to read errors or scratched sectors.</b><br>"
            "Choose a recovery strategy:"
        )
        lbl.setTextFormat(Qt.TextFormat.RichText)
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

    def _dialog_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
    ) -> ConflictChoice:
        dlg = QDialog(self)
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

    def closeEvent(self, event) -> None:
        if _is_worker_running(self.worker):
            ans = QMessageBox.question(
                self,
                "Extraction in Progress",
                "A rip workflow is currently active.\nDo you want to cancel extraction and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.phase_detail_lbl.setText("Cancelling active rip and cleaning up...")
            self.cancel_btn.setEnabled(False)
            self.worker.cancel()

        self.adapter.shutdown()
        if hasattr(self, "drive_poll_timer") and self.drive_poll_timer.isActive():
            self.drive_poll_timer.stop()

        for w in (self.startup_worker, self.details_worker, self.cover_worker):
            if _is_worker_running(w) and hasattr(w, "cancel"):
                w.cancel()

        workers = [
            w for w in (
                self.worker,
                self.startup_worker,
                self.identify_worker,
                self.details_worker,
                self.cover_worker,
                self.poll_worker,
                self.update_worker,
                self.resume_worker,
                self.discard_worker,
                self.eject_worker,
            )
            if _is_worker_running(w)
        ]

        if workers:
            start_wait = time.time()
            max_wait = 6.0 if _is_worker_running(self.worker) else 2.5
            while any(w.isRunning() for w in workers) and (time.time() - start_wait < max_wait):
                QApplication.processEvents()
                time.sleep(0.05)

        for w in workers:
            if _is_worker_running(w):
                try:
                    w.disconnect()
                except Exception:
                    pass

        event.accept()
