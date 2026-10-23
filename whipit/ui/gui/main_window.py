"""Main application window and user interaction coordinator for WhipIt GUI."""

from __future__ import annotations

import html
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QGuiApplication, QIcon, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.bootstrap import get_missing_dependencies
from whipit.config import (
    drive_has_disc,
    eject_drive,
    get_disc_status,
    get_drive_hardware_id,
    get_drives_with_hardware_ids,
    get_output_root,
)
from whipit.launcher import get_icon_path, install_icon, resolve_executable_path
from whipit.models import JobManifest, ReleaseOption, TrackInfo, WorkflowState
from whipit.net import is_strict_https_url
from whipit.recovery import RecoveryCoordinator
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.ripping.whipper import (
    AlbumHeaderEvent,
    DiscTOCEvent,
    DriveStallEvent,
    ReleasePickedEvent,
    TrackProgressEvent,
    WhipperEvent,
    WhipperStreamParser,
)
from whipit.state_store import ManifestStore
from whipit.ui.base import (
    CalibrationChoice,
    ConflictChoice,
    DiscPromptResult,
    ExtractionErrorAction,
    ExtractionErrorChoice,
    LogLevel,
)
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.ui.gui.dialogs import (
    prompt_calibration_retry_dialog,
    prompt_calibration_start_dialog,
    prompt_conflict_resolution_dialog,
    prompt_empty_multi_drives_dialog,
    prompt_extraction_error_dialog,
    prompt_insert_disc_dialog,
    prompt_recover_job_dialog,
    prompt_restart_dialog,
    show_about_dialog,
    show_message_dialog,
)
from whipit.ui.gui.session import ActiveSession
from whipit.ui.gui.styles import (
    STATUS_BADGE_AUDIO,
    STATUS_BADGE_DATA,
    STATUS_BADGE_DISC,
    STATUS_BADGE_EMPTY,
    STATUS_BADGE_NO_DRIVE,
    VERIFICATION_BADGE_ACCURATE,
    VERIFICATION_BADGE_UNVERIFIED,
    get_application_stylesheet,
)
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


def _is_worker_running(worker: Optional[Any]) -> bool:
    if worker is None:
        return False
    try:
        is_running = getattr(worker, "isRunning", None)
        if callable(is_running):
            return bool(is_running())
        return False
    except (RuntimeError, AttributeError):
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
        self.setMinimumSize(800, 580)

        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            target_w = min(1040, max(800, int(avail.width() * 0.80)))
            target_h = min(860, max(580, int(avail.height() * 0.88)))
            self.resize(target_w, target_h)
        else:
            self.resize(1000, 820)

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
        self.session = ActiveSession()
        self._scraped_artist: Optional[str] = None
        self._scraped_title: Optional[str] = None
        self._ripping_started: bool = False
        self._last_inspection: Optional[DiscInspectionResult] = None
        self.badge_labels: Dict[str, QLabel] = {}
        self._current_step_key: Optional[str] = None
        self._whipper_parser = WhipperStreamParser()
        self._disc_was_ejected: bool = False
        self._requested_mbids: set[str] = set()
        self._release_cache: Dict[str, Tuple[Optional[str], List[TrackInfo]]] = {}

        self._setup_ui()
        self._connect_signals()
        self.setStyleSheet(get_application_stylesheet())

        self.refresh_drives()
        self.drive_poll_timer = QTimer(self)
        self.drive_poll_timer.setInterval(2500)
        self.drive_poll_timer.timeout.connect(self._trigger_drive_poll)
        self.drive_poll_timer.start()

        QTimer.singleShot(250, self._startup_checks)
        self._refresh_action_buttons()

    # Backwards-compatible session properties for existing tests
    @property
    def _current_art_path(self) -> Optional[Path]:
        return self.session.art_path

    @_current_art_path.setter
    def _current_art_path(self, val: Optional[Path]) -> None:
        self.session.art_path = val

    @property
    def _current_mbid(self) -> Optional[str]:
        return self.session.mbid

    @_current_mbid.setter
    def _current_mbid(self, val: Optional[str]) -> None:
        self.session.mbid = val

    @property
    def _current_mb_url(self) -> Optional[str]:
        return self.session.mb_url

    @_current_mb_url.setter
    def _current_mb_url(self, val: Optional[str]) -> None:
        self.session.mb_url = val

    @property
    def _current_discogs_url(self) -> Optional[str]:
        return self.session.discogs_url

    @_current_discogs_url.setter
    def _current_discogs_url(self, val: Optional[str]) -> None:
        self.session.discogs_url = val

    @property
    def _current_mb_lookup_url(self) -> Optional[str]:
        return self.session.mb_lookup_url

    @_current_mb_lookup_url.setter
    def _current_mb_lookup_url(self, val: Optional[str]) -> None:
        self.session.mb_lookup_url = val

    @property
    def _art_is_user_selected(self) -> bool:
        return self.session.art_is_user_selected

    @_art_is_user_selected.setter
    def _art_is_user_selected(self, val: bool) -> None:
        self.session.art_is_user_selected = val

    @property
    def _downloaded_art_path(self) -> Optional[Path]:
        return self.session.downloaded_art_path

    @_downloaded_art_path.setter
    def _downloaded_art_path(self, val: Optional[Path]) -> None:
        self.session.downloaded_art_path = val

    @property
    def _matching_releases(self) -> List[ReleaseOption]:
        return self.session.matching_releases

    @_matching_releases.setter
    def _matching_releases(self, val: List[ReleaseOption]) -> None:
        self.session.matching_releases = val

    def _setup_ui(self) -> None:
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content_widget = QWidget()
        root_layout = QVBoxLayout(content_widget)
        root_layout.setContentsMargins(16, 14, 16, 14)
        root_layout.setSpacing(10)

        menubar = self.menuBar()
        help_menu = menubar.addMenu("&Help")

        check_update_act = help_menu.addAction("Check for &Updates...")
        check_update_act.triggered.connect(self.check_for_updates)

        help_menu.addSeparator()

        about_act = help_menu.addAction("&About WhipIt")
        about_act.triggered.connect(self.show_about)

        drive_group = QGroupBox("Optical Drive & Archival Settings")
        drive_layout = QVBoxLayout(drive_group)
        drive_layout.setContentsMargins(12, 10, 12, 10)
        drive_layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(QLabel("Optical Drive:"))
        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(220)
        self.drive_combo.currentIndexChanged.connect(self._trigger_drive_poll)
        top_row.addWidget(self.drive_combo, stretch=1)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_drives)
        top_row.addWidget(self.refresh_btn)

        self.disc_status_badge = QLabel("Checking...")
        self.disc_status_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.disc_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disc_status_badge.setMinimumWidth(130)
        self.disc_status_badge.setObjectName("statusBadge")
        top_row.addWidget(self.disc_status_badge)
        drive_layout.addLayout(top_row)

        hw_row = QHBoxLayout()
        hw_row.setSpacing(8)
        hw_row.addWidget(QLabel("Read Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItem("Full Speed (Normal)", None)
        self.speed_combo.addItem("4x (Safe / Scratched)", 4)
        self.speed_combo.addItem("8x (Moderate)", 8)
        self.speed_combo.addItem("12x", 12)
        self.speed_combo.addItem("16x", 16)
        self.speed_combo.addItem("24x", 24)
        hw_row.addWidget(self.speed_combo)

        hw_row.addSpacing(12)
        hw_row.addWidget(QLabel("Offset Override:"))
        self.offset_input = QLineEdit()
        self.offset_input.setPlaceholderText("Auto / Saved")
        self.offset_input.setFixedWidth(95)
        hw_row.addWidget(self.offset_input)
        hw_row.addStretch(1)
        drive_layout.addLayout(hw_row)

        chk_row = QHBoxLayout()
        chk_row.setSpacing(14)
        self.keep_going_chk = QCheckBox("Rescue readable tracks (--keep-going)")
        self.keep_going_chk.setToolTip("Continue ripping remaining readable tracks if read errors occur")
        self.unknown_chk = QCheckBox("Allow unknown disc (-U)")
        self.unknown_chk.setToolTip("Rip uncataloged discs or CD-Rs with generic track names when MusicBrainz data is unavailable")
        self.overwrite_chk = QCheckBox("Overwrite existing")
        self.overwrite_chk.setToolTip("Replace an existing rip with a backup preserved during the transaction")
        chk_row.addWidget(self.keep_going_chk)
        chk_row.addWidget(self.unknown_chk)
        chk_row.addWidget(self.overwrite_chk)
        chk_row.addStretch(1)
        drive_layout.addLayout(chk_row)

        out_row = QHBoxLayout()
        out_row.setSpacing(8)
        out_row.addWidget(QLabel("Output Folder:"))
        self.output_edit = QLineEdit(str(get_output_root()))
        out_row.addWidget(self.output_edit, stretch=1)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_output_dir)
        out_row.addWidget(self.browse_btn)
        drive_layout.addLayout(out_row)

        root_layout.addWidget(drive_group)

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

        progress_group = QGroupBox("Album")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setContentsMargins(12, 10, 12, 10)
        progress_layout.setSpacing(8)

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(14)

        left_card = QWidget()
        left_card.setMinimumWidth(260)
        left_card.setMaximumWidth(320)
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        art_container = QWidget()
        art_layout = QVBoxLayout(art_container)
        art_layout.setContentsMargins(0, 0, 0, 0)
        art_layout.setSpacing(4)

        self.cover_art_label = QLabel()
        self.cover_art_label.setFixedSize(150, 150)
        self.cover_art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_art_label.setObjectName("coverArtLabel")
        self.cover_art_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_art_label.setToolTip("Click to select custom cover art")
        self.cover_art_label.mousePressEvent = lambda ev: self._choose_custom_art()
        self._render_cover_art_placeholder()
        art_layout.addWidget(self.cover_art_label, 0, Qt.AlignmentFlag.AlignHCenter)

        art_btn_row = QHBoxLayout()
        art_btn_row.setSpacing(6)
        art_btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.change_art_btn = QPushButton("Change...")
        self.change_art_btn.setFixedHeight(26)
        self.change_art_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.change_art_btn.setToolTip("Select custom cover art from file")
        self.change_art_btn.clicked.connect(self._choose_custom_art)
        art_btn_row.addWidget(self.change_art_btn)

        self.reset_art_btn = QPushButton("Reset")
        self.reset_art_btn.setFixedHeight(26)
        self.reset_art_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.reset_art_btn.setToolTip("Reset to original Cover Art Archive image")
        self.reset_art_btn.setVisible(False)
        self.reset_art_btn.clicked.connect(self._reset_cover_art)
        art_btn_row.addWidget(self.reset_art_btn)

        art_layout.addLayout(art_btn_row)
        left_layout.addWidget(art_container, 0, Qt.AlignmentFlag.AlignHCenter)

        self.album_title_lbl = QLabel("Ready to rip Audio CD")
        self.album_title_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.album_title_lbl.setWordWrap(True)
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        self.album_title_lbl.setFont(title_font)
        left_layout.addWidget(self.album_title_lbl)

        self.verification_lbl = QLabel("")
        self.verification_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.verification_lbl.setWordWrap(True)
        left_layout.addWidget(self.verification_lbl)

        self.release_combo_row = QWidget()
        rel_layout = QVBoxLayout(self.release_combo_row)
        rel_layout.setContentsMargins(0, 0, 0, 0)
        rel_layout.setSpacing(2)
        rel_lbl = QLabel("Select Pressing:")
        rel_lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #94a3b8;")
        rel_layout.addWidget(rel_lbl)
        self.release_combo = QComboBox()
        self.release_combo.currentIndexChanged.connect(self._on_release_combo_changed)
        rel_layout.addWidget(self.release_combo)
        left_layout.addWidget(self.release_combo_row)
        self.release_combo_row.setVisible(False)

        self.phase_detail_lbl = QLabel("Insert an Audio CD and click Identify Disc or Start Rip.")
        self.phase_detail_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.phase_detail_lbl.setWordWrap(True)
        left_layout.addWidget(self.phase_detail_lbl)
        left_layout.addStretch(1)

        columns_layout.addWidget(left_card)

        right_card = QWidget()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        table_header = QHBoxLayout()
        self.tracklist_header_lbl = QLabel("Track Listing")
        self.tracklist_header_lbl.setStyleSheet("font-weight: bold; color: #22d3ee; font-size: 11px;")
        table_header.addWidget(self.tracklist_header_lbl)
        table_header.addStretch()
        right_layout.addLayout(table_header)

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
        self.track_table.setMinimumHeight(160)
        right_layout.addWidget(self.track_table, stretch=1)

        self.track_placeholder = QLabel(
            "No tracklist loaded.\n\nInsert an Audio CD and click 'Identify Disc' or 'Start Rip' to preview tracks."
        )
        self.track_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.track_placeholder.setWordWrap(True)
        self.track_placeholder.setObjectName("trackPlaceholder")
        self.track_placeholder.setMinimumHeight(160)
        right_layout.addWidget(self.track_placeholder, stretch=1)

        self.track_table.setVisible(False)
        self.track_placeholder.setVisible(True)

        columns_layout.addWidget(right_card, stretch=1)
        progress_layout.addLayout(columns_layout, stretch=1)

        self.external_links_lbl = QLabel("")
        self.external_links_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.external_links_lbl.setOpenExternalLinks(False)
        self.external_links_lbl.setWordWrap(True)
        self.external_links_lbl.linkActivated.connect(self._on_external_link_clicked)
        self.external_links_lbl.setStyleSheet("padding: 2px 0px; font-size: 11px;")
        self.external_links_lbl.setVisible(False)
        progress_layout.addWidget(self.external_links_lbl)

        form_prog = QFormLayout()
        form_prog.setContentsMargins(0, 4, 0, 0)
        form_prog.setSpacing(6)
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

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

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
        self.console_box.setMinimumHeight(90)
        self.console_box.setMaximumHeight(200)
        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(9)
        self.console_box.setFont(mono)
        root_layout.addWidget(self.console_box)

        scroll_area.setWidget(content_widget)
        self.setCentralWidget(scroll_area)

    def _connect_signals(self) -> None:
        sig = self.adapter.signaler
        sig.log_emitted.connect(self._on_log)
        sig.state_changed.connect(self._on_state_changed)
        sig.whipper_output.connect(self._on_whipper_output)
        sig.whipper_event.connect(self._on_whipper_event)
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
            user_choice = prompt_recover_job_dialog(self, manifest, status)
            if user_choice == "resume":
                self._start_resume_worker(coordinator, manifest)
                break
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
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_NO_DRIVE)
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
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
            if self._current_step_key != "COMMITTED":
                self._set_pipeline_step("DISC_DETECTED")
        elif is_data_disc:
            self._disc_was_ejected = True
            self.disc_status_badge.setText("Data / DVD Disc")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DATA)
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()
        elif drive_st == CDS_DISC_OK:
            self.disc_status_badge.setText("Disc Loaded")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DISC)
        else:
            self._disc_was_ejected = True
            self.disc_status_badge.setText("Tray Empty")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_EMPTY)
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
        if self.session.mb_url and self._is_safe_url(self.session.mb_url):
            esc_mb = html.escape(self.session.mb_url, quote=True)
            links.append(
                f'<a href="{esc_mb}" style="color: #22d3ee; text-decoration: none; font-weight: 500;">↗ MusicBrainz</a>'
            )
        if self.session.discogs_url and self._is_safe_url(self.session.discogs_url):
            esc_discogs = html.escape(self.session.discogs_url, quote=True)
            links.append(
                f'<a href="{esc_discogs}" style="color: #a7f3d0; text-decoration: none; font-weight: 500;">↗ Discogs</a>'
            )
        # Only show the submission / attach link when the disc is NOT already listed on MusicBrainz
        if not self.session.mb_url and self.session.mb_lookup_url and self._is_safe_url(self.session.mb_lookup_url):
            esc_lookup = html.escape(self.session.mb_lookup_url, quote=True)
            links.append(
                f'<a href="{esc_lookup}" style="color: #38bdf8; text-decoration: none; font-weight: 500;">↗ Add this Disc to MusicBrainz</a>'
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

        if self.session.discogs_url:
            lines.append(f"- Discogs release reference: {self.session.discogs_url}")

        note_text = "\n".join(lines)
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(note_text)
        self.phase_detail_lbl.setText("✓ MusicBrainz edit note copied to clipboard!")
        self._append_console("[OK] MusicBrainz submission note copied to clipboard.")

    def _reset_disc_state(self, clear_user_art: bool = True) -> None:
        """Clears all metadata, track preview, links, and cover art from any previous disc."""
        self.session.reset(keep_user_art=not clear_user_art and self.session.art_is_user_selected)
        self._scraped_artist = None
        self._scraped_title = None
        self._ripping_started = False
        self._last_inspection = None
        self._requested_mbids.clear()
        self._release_cache.clear()
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        self.release_combo.blockSignals(False)
        self.release_combo_row.setVisible(False)
        self.external_links_lbl.setText("")
        self.external_links_lbl.setVisible(False)
        self.track_table.setRowCount(0)
        self.track_table.setVisible(False)
        if hasattr(self, "track_placeholder"):
            self.track_placeholder.setVisible(True)
        if hasattr(self, "tracklist_header_lbl"):
            self.tracklist_header_lbl.setText("Track Listing")
        self.album_title_lbl.setText("Ready to rip Audio CD")
        self.verification_lbl.setText("")

        if clear_user_art or not self.session.art_is_user_selected:
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
            146,
            146,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_art_label.setPixmap(scaled)
        self.cover_art_label.setToolTip(f"Cover Art: {art_path.name}\n(Click to replace)")

    def _fetch_cover_art(self, mbid: str) -> None:
        if self.session.art_is_user_selected or not mbid:
            return
        if _is_worker_running(self.cover_worker):
            if getattr(self.cover_worker, "mbid", None) == mbid:
                return
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
        if self.session.mbid and mbid != self.session.mbid:
            return
        path = Path(image_path)
        self.session.set_downloaded_art(path)
        if not self.session.art_is_user_selected:
            self._render_cover_art(path)
            self.adapter.set_cover_art(path)
            self._append_console(f"[OK] Cover Art Archive thumbnail loaded ({path.name})")

    def _on_cover_art_failed(self, mbid: str) -> None:
        if self.session.mbid and mbid != self.session.mbid:
            return
        if not self.session.art_is_user_selected:
            self.session.clear_art()
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
        self.session.set_user_art(target_path)
        self._render_cover_art(target_path)
        self.adapter.set_cover_art(target_path)
        self.reset_art_btn.setVisible(bool(self.session.downloaded_art_path))
        self._append_console(f"[INFO] Cover art set to: {chosen_path.name}")
        self.phase_detail_lbl.setText(f"✓ Cover art selected: {chosen_path.name}")

    def _reset_cover_art(self) -> None:
        if self.session.downloaded_art_path and self.session.downloaded_art_path.exists():
            self.session.reset_art_to_original()
            if self.session.art_path:
                self._render_cover_art(self.session.art_path)
                self.adapter.set_cover_art(self.session.art_path)
            self.reset_art_btn.setVisible(False)
            self._append_console("[INFO] Reverted to Cover Art Archive original.")
            self.phase_detail_lbl.setText("✓ Restored Cover Art Archive image.")
        else:
            self.session.clear_art()
            self.session.art_is_user_selected = False
            self._render_cover_art_placeholder()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._append_console("[INFO] Cleared cover art.")

    def _populate_track_table(self, tracks: List[TrackInfo]) -> None:
        if not tracks:
            self.track_table.setVisible(False)
            if hasattr(self, "track_placeholder"):
                self.track_placeholder.setVisible(True)
            if hasattr(self, "tracklist_header_lbl"):
                self.tracklist_header_lbl.setText("Track Listing")
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
        if hasattr(self, "track_placeholder"):
            self.track_placeholder.setVisible(False)
        if hasattr(self, "tracklist_header_lbl"):
            self.tracklist_header_lbl.setText(f"Track Listing ({len(tracks)} Tracks)")

    def _on_details_resolved(self, mbid: str, discogs_url: str, tracks: List[TrackInfo]) -> None:
        self._release_cache[mbid] = (discogs_url or None, list(tracks))

        for rel in self.session.matching_releases:
            if rel.mbid == mbid:
                rel.tracks = list(tracks)
                break

        if self.session.mbid == mbid:
            self.session.discogs_url = discogs_url or self.session.discogs_url
            self.session.tracks = list(tracks)
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
        if not self.session.matching_releases or idx < 0 or idx >= len(self.session.matching_releases):
            return
        rel = self.session.matching_releases[idx]
        self.session.select_release(rel)
        self.album_title_lbl.setText(self.session.display_title)

        if rel.mbid in self._release_cache:
            cached_discogs, cached_tracks = self._release_cache[rel.mbid]
            self.session.discogs_url = cached_discogs
            self._update_links_display()
            if cached_tracks:
                self._populate_track_table(cached_tracks)
                rel.tracks = cached_tracks
        elif rel.tracks:
            self._populate_track_table(rel.tracks)
            self._update_links_display()
        else:
            exp_tracks = self._last_inspection.tracks_count if self._last_inspection else None
            self._start_release_details_lookup(
                rel.mbid,
                barcode=rel.barcode,
                artist=rel.artist,
                title=rel.title,
                expected_tracks=exp_tracks,
            )
        if rel.mbid and not self.session.art_is_user_selected:
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
        self._last_inspection = result
        self.session.set_from_inspection(result)

        if result.success and (result.artist != "Unknown Artist" or result.mb_url):
            self.album_title_lbl.setText(self.session.display_title)

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

            self._update_links_display()
            self._populate_track_table(result.tracks)
            if result.mbid:
                self._release_cache[result.mbid] = (result.discogs_url, list(result.tracks))

            if self.session.matching_releases and len(self.session.matching_releases) > 1:
                if self.session.matching_releases[0].mbid == result.mbid:
                    self.session.matching_releases[0].tracks = list(result.tracks)
                self.release_combo.blockSignals(True)
                self.release_combo.clear()
                for rel in self.session.matching_releases:
                    det = []
                    if rel.country:
                        det.append(rel.country)
                    if rel.cat_no:
                        det.append(f"Cat: {rel.cat_no}")
                    extra = f" [{', '.join(det)}]" if det else ""
                    self.release_combo.addItem(f"{rel.title}{extra}")
                self.release_combo.blockSignals(False)
                self.release_combo_row.setVisible(True)
                if self.session.matching_releases[0].mbid and not self.session.art_is_user_selected:
                    self._fetch_cover_art(self.session.matching_releases[0].mbid)
            else:
                self.release_combo_row.setVisible(False)
                if result.mbid and not self.session.art_is_user_selected:
                    self._fetch_cover_art(result.mbid)
        else:
            self.release_combo_row.setVisible(False)
            self.track_table.setVisible(False)
            if hasattr(self, "track_placeholder"):
                self.track_placeholder.setVisible(True)
            if hasattr(self, "tracklist_header_lbl"):
                self.tracklist_header_lbl.setText("Track Listing")
            self.album_title_lbl.setText(self.session.display_title)
            if not self.session.art_is_user_selected:
                self.session.clear_art()
                self.adapter.set_cover_art(None)
                self.reset_art_btn.setVisible(False)
                self._render_cover_art_placeholder()
            self._update_links_display()

            err = result.error_message or "Could not retrieve disc metadata from MusicBrainz."
            self.phase_detail_lbl.setText(f"⚠ {err}")
            self._append_console(f"[WARN] {err}")
            show_message_dialog(
                self,
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

        selected_release_id: Optional[str] = self.session.mbid
        if self.release_combo_row.isVisible() and self.session.matching_releases:
            idx = self.release_combo.currentIndex()
            if 0 <= idx < len(self.session.matching_releases):
                selected_release_id = self.session.matching_releases[idx].mbid

        if selected_release_id:
            self.session.mbid = selected_release_id
            self.session.mb_url = f"https://musicbrainz.org/release/{selected_release_id}"
            self._update_links_display()
            if not self.session.art_path and not self.session.art_is_user_selected:
                self._fetch_cover_art(selected_release_id)

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
                self.phase_detail_lbl.setText("Cancelling active rip and cleaning up...")
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
            self.session.artist = manifest.metadata.artist
            self.session.title = manifest.metadata.album
            self.session.disc_number = manifest.metadata.disc_number
            self.session.total_discs = manifest.metadata.total_discs
            self.album_title_lbl.setText(self.session.display_title)

        if manifest.metadata.musicbrainz_url:
            self.session.mb_url = manifest.metadata.musicbrainz_url
        if manifest.metadata.discogs_url:
            self.session.discogs_url = manifest.metadata.discogs_url
        if manifest.metadata.musicbrainz_lookup_url:
            self.session.mb_lookup_url = manifest.metadata.musicbrainz_lookup_url

        if manifest.metadata.musicbrainz_id and not self.session.mbid:
            self.session.mbid = manifest.metadata.musicbrainz_id
            if not self.session.art_path and not self.session.art_is_user_selected:
                self._fetch_cover_art(manifest.metadata.musicbrainz_id)
            if self.track_table.rowCount() == 0:
                self._start_release_details_lookup(
                    manifest.metadata.musicbrainz_id,
                    artist=manifest.metadata.artist,
                    title=manifest.metadata.album,
                    expected_tracks=manifest.tracks.expected_count or None,
                )

        self._update_links_display()

        if manifest.verification.accuraterip_summary != "Unknown":
            if manifest.verification.is_accurate:
                self.verification_lbl.setText("✓ AccurateRip Verified")
                self.verification_lbl.setStyleSheet(VERIFICATION_BADGE_ACCURATE)
            else:
                self.verification_lbl.setText(f"⚠ {manifest.verification.accuraterip_summary}")
                self.verification_lbl.setStyleSheet(VERIFICATION_BADGE_UNVERIFIED)

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
            ev = self._whipper_parser.parse_line(line)
            if ev:
                self._on_whipper_event(ev)
        except Exception:
            pass

    def _on_whipper_event(self, event: WhipperEvent) -> None:
        if isinstance(event, DiscTOCEvent):
            if event.lookup_url:
                self.session.mb_lookup_url = event.lookup_url
                self._update_links_display()

        elif isinstance(event, DriveStallEvent):
            self.phase_detail_lbl.setText(
                f"⚠ Optical drive read stalled for {int(event.seconds_stalled)}s. Waiting on hardware..."
            )

        elif isinstance(event, ReleasePickedEvent):
            self.session.mbid = event.mbid
            self.session.mb_url = f"https://musicbrainz.org/release/{event.mbid}"
            self._update_links_display()
            if not self.session.art_is_user_selected and not self.session.art_path:
                self._fetch_cover_art(event.mbid)

        elif isinstance(event, TrackProgressEvent):
            self._ripping_started = True
            self.rip_progress_bar.setRange(0, event.total_tracks)
            self.rip_progress_bar.setValue(event.current_track - 1)
            try_str = f" (try {event.try_number})" if event.try_number > 1 else ""
            self.rip_progress_bar.setFormat(
                f"{event.phase} Track {event.current_track} of {event.total_tracks}{try_str}... ({int(event.percent)}%)"
            )
            self.phase_detail_lbl.setText(
                f"{event.phase} Track {event.current_track} / {event.total_tracks}{try_str} via cd-paranoia..."
            )

        elif isinstance(event, AlbumHeaderEvent):
            if not self._ripping_started:
                if event.artist:
                    self.session.artist = event.artist
                if event.title:
                    self.session.title = event.title
                self.album_title_lbl.setText(self.session.display_title)

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
                QTimer.singleShot(150, lambda: show_message_dialog(self, QMessageBox.Icon.Information, "Check for Updates", message))
        else:
            self._append_console(f"[ERROR] {message}")
            QTimer.singleShot(150, lambda: show_message_dialog(self, QMessageBox.Icon.Warning, "Update Failed", message))

    def _prompt_restart_dialog(self, message: str) -> None:
        if prompt_restart_dialog(self, message):
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
        show_about_dialog(self)

    # Delegating methods for backward compatibility with existing tests
    def _dialog_empty_multi_drives(self, drives_with_hw: List[Tuple[str, str]]) -> DiscPromptResult:
        return prompt_empty_multi_drives_dialog(self, drives_with_hw)

    def _dialog_insert_disc(
        self,
        device: str,
        hw_id: str,
        available_drives: Optional[List[Tuple[str, str]]],
        allow_main_menu: bool,
    ) -> DiscPromptResult:
        return prompt_insert_disc_dialog(self, device, hw_id, available_drives, allow_main_menu)

    def _dialog_calibration_start(self, device: str) -> CalibrationChoice:
        return prompt_calibration_start_dialog(self, device)

    def _dialog_calibration_retry(self, device: str) -> CalibrationChoice:
        return prompt_calibration_retry_dialog(self, device)

    def _dialog_extraction_error(
        self,
        device: str,
        available_drives: List[Tuple[str, str]],
    ) -> ExtractionErrorChoice:
        return prompt_extraction_error_dialog(self, device, available_drives)

    def _dialog_conflict_resolution(
        self,
        flac_dir: Optional[Path],
        opus_dir: Optional[Path],
        warning: Optional[str] = None,
    ) -> ConflictChoice:
        return prompt_conflict_resolution_dialog(self, flac_dir, opus_dir, warning)

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
            res = show_message_dialog(
                self,
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
            res = show_message_dialog(
                self,
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
                    det.append(f"Cat: {r.cat_no}")
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
                chosen = releases[idx]
                self.session.select_release(chosen)
                self.album_title_lbl.setText(self.session.display_title)
                self._update_links_display()
                if chosen.tracks:
                    self._populate_track_table(chosen.tracks)
                if not self.session.art_is_user_selected:
                    self._fetch_cover_art(chosen.mbid)
                return chosen.mbid
            return None

        if prompt_type == "prompt_empty_multi_drives":
            drives_with_hw = payload
            return prompt_empty_multi_drives_dialog(self, drives_with_hw)

        if prompt_type == "prompt_insert_disc":
            device, hw_id, available_drives, allow_main_menu = payload
            return prompt_insert_disc_dialog(self, device, hw_id, available_drives, allow_main_menu)

        if prompt_type == "wait_for_user_reload":
            device, message = payload
            res = show_message_dialog(
                self,
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
            return prompt_calibration_start_dialog(self, device)

        if prompt_type == "prompt_calibration_retry":
            device = payload
            return prompt_calibration_retry_dialog(self, device)

        if prompt_type == "prompt_cdr_detected":
            device = payload
            res = show_message_dialog(
                self,
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
            choice = prompt_extraction_error_dialog(self, device, available_drives)
            if choice.action == ExtractionErrorAction.RETRY_SLOW_4X:
                self.speed_combo.blockSignals(True)
                idx = self.speed_combo.findData(4)
                if idx >= 0:
                    self.speed_combo.setCurrentIndex(idx)
                self.speed_combo.blockSignals(False)
                self.rip_progress_bar.setRange(0, 0)
                self.rip_progress_bar.setFormat("Retrying extraction at slow speed (4x)...")
                self.phase_detail_lbl.setText("Retrying album extraction at slow speed (4x)...")
            elif choice.action == ExtractionErrorAction.RETRY_MODERATE_8X:
                self.speed_combo.blockSignals(True)
                idx = self.speed_combo.findData(8)
                if idx >= 0:
                    self.speed_combo.setCurrentIndex(idx)
                self.speed_combo.blockSignals(False)
                self.rip_progress_bar.setRange(0, 0)
                self.rip_progress_bar.setFormat("Retrying extraction at moderate speed (8x)...")
                self.phase_detail_lbl.setText("Retrying album extraction at moderate speed (8x)...")
            elif choice.action == ExtractionErrorAction.KEEP_GOING:
                self.keep_going_chk.setChecked(True)
                self.phase_detail_lbl.setText("Rescuing remaining readable tracks (--keep-going)...")
            return choice

        if prompt_type == "prompt_conflict_resolution":
            if isinstance(payload, tuple) and len(payload) == 3:
                flac_dir, opus_dir, warning = payload
            else:
                flac_dir, opus_dir = payload
                warning = None
            return prompt_conflict_resolution_dialog(self, flac_dir, opus_dir, warning)

        return GuiInteractionAdapter._default_for_prompt(prompt_type)

    def closeEvent(self, event) -> None:
        try:
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
                if hasattr(self.worker, "cancel") and callable(self.worker.cancel):
                    self.worker.cancel()

            self.adapter.shutdown()
            if hasattr(self, "drive_poll_timer") and self.drive_poll_timer.isActive():
                self.drive_poll_timer.stop()

            for w in (self.startup_worker, self.details_worker, self.cover_worker):
                if _is_worker_running(w) and hasattr(w, "cancel") and callable(w.cancel):
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
                while any(_is_worker_running(w) for w in workers) and (time.time() - start_wait < max_wait):
                    QApplication.processEvents()
                    time.sleep(0.05)

            for w in workers:
                if _is_worker_running(w):
                    try:
                        w.disconnect()
                    except Exception:
                        pass
        except Exception:
            pass

        event.accept()
