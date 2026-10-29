"""Standalone UI panel encapsulating the lifecycle, session, and controls for an optical drive."""

from __future__ import annotations

import html
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QPixmap
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
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.bootstrap import get_missing_dependencies
from whipit.config import (
    CDS_AUDIO,
    CDS_DATA_1,
    CDS_DATA_2,
    CDS_DISC_OK,
    CDS_MIXED,
    CDS_XA_2_1,
    CDS_XA_2_2,
    UserPreferences,
    get_drive_hardware_id,
    get_drive_max_speed,
    load_preferences,
    parse_duration_seconds,
)
from whipit.models import JobManifest, ReleaseOption, TrackInfo, WorkflowState
from whipit.net import is_strict_https_url
from whipit.ripping.identify import DiscInspectionResult, inspect_disc
from whipit.ripping.whipper import (
    AccurateRipSummaryEvent,
    AlbumHeaderEvent,
    DiscTOCEvent,
    DriveStallEvent,
    ReleasePickedEvent,
    TrackProgressEvent,
    WhipperEvent,
    WhipperStreamParser,
)
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
    prompt_edit_metadata_dialog,
    prompt_empty_multi_drives_dialog,
    prompt_extraction_error_dialog,
    prompt_insert_disc_dialog,
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
    VERIFICATION_BADGE_ERROR,
    VERIFICATION_BADGE_UNVERIFIED,
)
from whipit.ui.gui.worker import (
    CoverArtWorker,
    DrivePollWorker,
    EjectWorker,
    IdentifyWorker,
    ReleaseDetailsWorker,
    RipWorker,
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


def format_accuraterip_verdict(
    summary: str,
    is_accurate: bool = False,
    health_status: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    Translates raw technical Whipper verification reports into clear, user-friendly
    badge text, stylesheet constants, and explanatory tooltips.
    """
    clean = (summary or "").strip()
    is_acc = is_accurate or clean.lower() == "all tracks accurately ripped"

    if is_acc:
        text = "✓ AccurateRip: Verified (All tracks matched)"
        style = VERIFICATION_BADGE_ACCURATE
        tip = (
            "All tracks match existing checksums in the AccurateRip database with bit-perfect accuracy.\n"
            "Your rip is 100% authentic and verified identical to community rips."
        )
        return text, style, tip

    m_no_match = re.search(r"(\d+)/(\d+)\s+got no match", clean, re.IGNORECASE)
    m_mismatch = re.search(r"(\d+)/(\d+)\s+had mismatches", clean, re.IGNORECASE)

    no_match_count = int(m_no_match.group(1)) if m_no_match else 0
    mismatch_count = int(m_mismatch.group(1)) if m_mismatch else 0
    total = int(m_no_match.group(2)) if m_no_match else (int(m_mismatch.group(2)) if m_mismatch else 0)

    if total > 0:
        matched = max(0, total - no_match_count - mismatch_count)
        if mismatch_count == 0 and matched > 0:
            text = f"✓ AccurateRip: {matched}/{total} Verified ({no_match_count} not in DB)"
            style = VERIFICATION_BADGE_UNVERIFIED
            tip = (
                f"{matched} of {total} tracks matched the AccurateRip database bit-for-bit.\n\n"
                f"{no_match_count} track(s) have not yet been submitted to AccurateRip for this specific pressing\n"
                "(often due to minor silence or pregap mastering differences between CD manufacturing plants).\n"
                "Both extraction passes (Test & Copy) matched identically with zero read errors."
            )
            return text, style, tip
        elif mismatch_count > 0:
            plural = "es" if mismatch_count > 1 else ""
            text = f"⚠ AccurateRip: {matched}/{total} Verified ({mismatch_count} CRC mismatch{plural})"
            style = VERIFICATION_BADGE_ERROR
            tip = (
                f"{matched} of {total} tracks matched the AccurateRip database.\n\n"
                f"{mismatch_count} track(s) differed from the database. This could indicate a scratched sector\n"
                "or an alternate mastering/pressing. Review the activity log for track details."
            )
            return text, style, tip

    if "not present" in clean.lower():
        text = "ℹ AccurateRip: Disc not present in database (Unverified)"
        style = VERIFICATION_BADGE_UNVERIFIED
        tip = (
            "This CD pressing has not yet been submitted to the AccurateRip database.\n"
            "Both extraction passes (Test & Copy) produced identical audio with zero read errors."
        )
        return text, style, tip

    if clean and clean != "Unknown":
        text = f"⚠ AccurateRip: {clean}"
        style = VERIFICATION_BADGE_ERROR if "error" in clean.lower() else VERIFICATION_BADGE_UNVERIFIED
        tip = f"AccurateRip status: {clean}"
        return text, style, tip

    if health_status and health_status != "Unknown":
        if health_status == "No errors occurred":
            return "✓ Health: No errors occurred", VERIFICATION_BADGE_ACCURATE, "Extraction completed with zero errors."
        else:
            return f"⚠ Health: {health_status}", VERIFICATION_BADGE_UNVERIFIED, f"Report: {health_status}"

    return "", "", ""


def _get_override(name: str, fallback: Any) -> Any:
    try:
        import whipit.ui.gui.main_window as mw
        return getattr(mw, name, fallback)
    except Exception:
        return fallback


class DrivePanel(QWidget):
    log_emitted = pyqtSignal(str)
    status_changed = pyqtSignal(str, str)

    def __init__(
        self,
        device: Optional[str] = None,
        prefs: Optional[UserPreferences] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.device_path: Optional[str] = device
        self.prefs: UserPreferences = prefs or load_preferences()
        self.adapter = GuiInteractionAdapter()
        self.session = ActiveSession()

        self.worker: Optional[RipWorker] = None
        self.identify_worker: Optional[IdentifyWorker] = None
        self.details_worker: Optional[ReleaseDetailsWorker] = None
        self.cover_worker: Optional[CoverArtWorker] = None
        self.poll_worker: Optional[DrivePollWorker] = None
        self.eject_worker: Optional[EjectWorker] = None

        self._scraped_artist: Optional[str] = None
        self._scraped_title: Optional[str] = None
        self._ripping_started: bool = False
        self._last_inspection: Optional[DiscInspectionResult] = None
        self.badge_labels: Dict[str, QLabel] = {}
        self._current_step_key: Optional[str] = None
        self._whipper_parser = WhipperStreamParser()
        self._disc_was_ejected: bool = True
        self._art_chosen_since_commit: bool = False
        self._disc_identified: bool = False
        self._rip_start_time: float = 0.0
        self._speed_sample_time: float = 0.0
        self._speed_sample_percent: float = 0.0
        self._speed_sample_track: int = 0
        self._current_speed_x: Optional[float] = None
        self._release_cache: Dict[str, Tuple[Optional[str], List[TrackInfo]]] = {}

        self._setup_ui()
        self._connect_signals()
        if device:
            self.set_device(device)

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

    @property
    def is_ready_to_rip(self) -> bool:
        return (
            bool(self.session.is_disc_loaded)
            and not (self.worker and self.worker.isRunning())
            and not (self.identify_worker and self.identify_worker.isRunning())
        )

    def set_device(self, dev: Optional[str]) -> None:
        self.device_path = dev
        self.session.device = dev
        dev_short = Path(dev).name if dev else ""
        if dev:
            hw = get_drive_hardware_id(dev)
            max_spd = get_drive_max_speed(dev)
            spd_str = f" [Max {max_spd}x]" if max_spd else ""
            self.drive_info_lbl.setText(f"Drive: <b>{dev}</b> ({hw}{spd_str})")
            self.session.hardware_id = hw
            self.start_btn.setText(f"Start Rip ({dev_short})")
            self.start_btn.setToolTip(f"Start archival extraction for the disc in {dev} only")
            self.start_btn.setEnabled(self.is_ready_to_rip)
            self.eject_btn.setText(f"Eject ({dev_short})")
            self.cancel_btn.setText(f"Cancel ({dev_short})")
        else:
            self.drive_info_lbl.setText("No optical drive assigned")
            self.disc_status_badge.setText("No Drive")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_NO_DRIVE)
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Start Rip")
            self.eject_btn.setText("Eject Drive")
            self.cancel_btn.setText("Cancel")

    def handle_drive_status(self, drive_st: int, disc_st: int) -> None:
        if not self.device_path:
            return

        has_audio = (drive_st == CDS_DISC_OK) and (disc_st in (CDS_AUDIO, CDS_MIXED))
        is_data_disc = (drive_st == CDS_DISC_OK) and (
            disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2)
        )

        if has_audio:
            self.session.is_disc_loaded = True
            is_new = self._disc_was_ejected or not self._disc_identified
            if is_new:
                self._disc_was_ejected = False
                self._disc_identified = True
                self._reset_disc_state(clear_user_art=True)
                self._reset_pipeline_badges()
            disc_name = "Enhanced CD" if disc_st == CDS_MIXED else "Audio CD"
            self.disc_status_badge.setText(f"{disc_name} Loaded")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
            if self._current_step_key != "COMMITTED":
                self._set_pipeline_step("DISC_DETECTED")
            if is_new and not (self.worker and self.worker.isRunning()) and not (self.identify_worker and self.identify_worker.isRunning()):
                self.identify_disc(silent=True)
            elif not (self.identify_worker and self.identify_worker.isRunning()):
                self.status_changed.emit(self.device_path or "", "Audio CD")
                if not (self.worker and self.worker.isRunning()):
                    dev_short = Path(self.device_path).name if self.device_path else ""
                    self.start_btn.setText(f"Start Rip ({dev_short})" if dev_short else "Start Rip")
                    self.start_btn.setEnabled(self.is_ready_to_rip)
        elif is_data_disc:
            self.start_btn.setEnabled(False)
            self.session.is_disc_loaded = False
            self._disc_was_ejected = True
            self._disc_identified = False
            self.disc_status_badge.setText("Data / DVD Disc")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DATA)
            self.status_changed.emit(self.device_path, "Data Disc")
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()
        elif drive_st == CDS_DISC_OK:
            self.start_btn.setEnabled(False)
            self.session.is_disc_loaded = False
            self.disc_status_badge.setText("Disc Loaded")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DISC)
            self.status_changed.emit(self.device_path, "Disc Loaded")
        else:
            self.start_btn.setEnabled(False)
            self.session.is_disc_loaded = False
            self._disc_was_ejected = True
            self._disc_identified = False
            self.disc_status_badge.setText("Tray Empty")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_EMPTY)
            self.status_changed.emit(self.device_path, "Empty")
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Drive Info Header
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self.drive_info_lbl = QLabel("Drive: Initializing...")
        self.drive_info_lbl.setTextFormat(Qt.TextFormat.RichText)
        header_row.addWidget(self.drive_info_lbl, stretch=1)

        self.disc_status_badge = QLabel("Checking...")
        self.disc_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disc_status_badge.setMinimumWidth(130)
        self.disc_status_badge.setObjectName("statusBadge")
        header_row.addWidget(self.disc_status_badge)
        layout.addLayout(header_row)

        # Pipeline Badges Frame
        pipeline_frame = QFrame()
        pipeline_frame.setObjectName("pipelineFrame")
        pipeline_layout = QHBoxLayout(pipeline_frame)
        pipeline_layout.setContentsMargins(10, 6, 10, 6)
        pipeline_layout.setSpacing(8)

        for key, label_text, _ in PIPELINE_STEPS:
            badge = QLabel(label_text)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedHeight(28)
            badge.setObjectName("stepInactive")
            self.badge_labels[key] = badge
            pipeline_layout.addWidget(badge, stretch=1)
        layout.addWidget(pipeline_frame)

        # Extraction Options
        chk_row = QHBoxLayout()
        chk_row.setSpacing(14)
        self.keep_going_chk = QCheckBox("Rescue readable tracks (--keep-going)")
        self.unknown_chk = QCheckBox("Allow unknown disc (-U)")
        self.overwrite_chk = QCheckBox("Overwrite existing")
        chk_row.addWidget(self.keep_going_chk)
        chk_row.addWidget(self.unknown_chk)
        chk_row.addWidget(self.overwrite_chk)
        chk_row.addStretch(1)
        layout.addLayout(chk_row)

        # Album Box
        album_group = QGroupBox("Album")
        album_layout = QVBoxLayout(album_group)
        album_layout.setContentsMargins(12, 10, 12, 10)
        album_layout.setSpacing(8)

        columns_layout = QHBoxLayout()
        columns_layout.setSpacing(14)

        # Left Column: Cover Art & Metadata
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
        self.cover_art_label.mousePressEvent = lambda ev: self._choose_custom_art()
        self._render_cover_art_placeholder()
        art_layout.addWidget(self.cover_art_label, 0, Qt.AlignmentFlag.AlignHCenter)

        art_btn_row = QHBoxLayout()
        art_btn_row.setSpacing(6)
        art_btn_row.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.change_art_btn = QPushButton("Change...")
        self.change_art_btn.setFixedHeight(26)
        self.change_art_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.change_art_btn.clicked.connect(self._choose_custom_art)
        art_btn_row.addWidget(self.change_art_btn)

        self.reset_art_btn = QPushButton("Reset")
        self.reset_art_btn.setFixedHeight(26)
        self.reset_art_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
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
        self.verification_lbl.setVisible(False)
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

        # Right Column: Track Listing
        right_card = QWidget()
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        table_header = QHBoxLayout()
        self.tracklist_header_lbl = QLabel("Track Listing")
        self.tracklist_header_lbl.setStyleSheet("font-weight: bold; color: #22d3ee; font-size: 11px;")
        table_header.addWidget(self.tracklist_header_lbl)
        table_header.addStretch()

        self.edit_meta_btn = QPushButton("✏️ Edit Info...")
        self.edit_meta_btn.setFixedHeight(26)
        self.edit_meta_btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        self.edit_meta_btn.clicked.connect(self._open_edit_metadata_dialog)
        table_header.addWidget(self.edit_meta_btn)
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
        album_layout.addLayout(columns_layout, stretch=1)

        self.external_links_lbl = QLabel("")
        self.external_links_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.external_links_lbl.setOpenExternalLinks(False)
        self.external_links_lbl.setWordWrap(True)
        self.external_links_lbl.linkActivated.connect(self._on_external_link_clicked)
        self.external_links_lbl.setStyleSheet("padding: 2px 0px; font-size: 11px;")
        self.external_links_lbl.setVisible(False)
        album_layout.addWidget(self.external_links_lbl)

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
        album_layout.addLayout(form_prog)

        layout.addWidget(album_group)

        # Action Controls Row
        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

        self.identify_btn = QPushButton("Identify Disc")
        self.identify_btn.setMinimumHeight(38)
        self.identify_btn.clicked.connect(lambda: self.identify_disc(silent=False))
        controls_row.addWidget(self.identify_btn, stretch=1)

        self.start_btn = QPushButton("Start Rip")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_rip)
        controls_row.addWidget(self.start_btn, stretch=2)

        self.eject_btn = QPushButton("Eject Drive")
        self.eject_btn.setMinimumHeight(38)
        self.eject_btn.clicked.connect(self.eject_drive)
        controls_row.addWidget(self.eject_btn, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerButton")
        self.cancel_btn.setMinimumHeight(38)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_rip)
        controls_row.addWidget(self.cancel_btn, stretch=1)

        layout.addLayout(controls_row)

    def _connect_signals(self) -> None:
        sig = self.adapter.signaler
        sig.log_emitted.connect(self._on_log)
        sig.state_changed.connect(self._on_state_changed)
        sig.whipper_output.connect(self._on_whipper_output)
        sig.whipper_event.connect(self._on_whipper_event)
        sig.transcode_progress.connect(self._on_transcode_progress)
        sig.disc_spinup_poll.connect(self._on_disc_spinup_poll)
        sig.prompt_requested.connect(self._on_prompt_requested)

    def apply_preferences(self, prefs: UserPreferences) -> None:
        self.prefs = prefs
        if not prefs.generate_opus:
            self.opus_progress_bar.setFormat("Disabled in Preferences")
        else:
            self.opus_progress_bar.setFormat("Waiting...")

    def _render_cover_art_placeholder(self, text: str = "No Cover Art\n\nClick to add") -> None:
        self.cover_art_label.clear()
        self.cover_art_label.setText(text)
        tip = "Click to select custom cover art"
        if "in archive" in text.lower():
            tip = "No cover art found in Cover Art Archive. Click to select a local image."
        elif "searching" in text.lower():
            tip = "Querying Cover Art Archive..."
        elif "reading" in text.lower():
            tip = "Reading optical disc Table of Contents..."
        self.cover_art_label.setToolTip(tip)

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

    def _fetch_cover_art_impl(self, mbid: str, alternate_mbids: Optional[List[str]] = None) -> None:
        if self.session.art_is_user_selected or not mbid:
            return
        if self.cover_worker and self.cover_worker.isRunning():
            if getattr(self.cover_worker, "mbid", None) == mbid:
                return
            self.cover_worker.cancel()

        self._render_cover_art_placeholder("Searching\nCover Art Archive...")
        WorkerClass = _get_override("CoverArtWorker", CoverArtWorker)
        try:
            self.cover_worker = WorkerClass(mbid, alternate_mbids=alternate_mbids, parent=None)
        except TypeError:
            self.cover_worker = WorkerClass(mbid, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.cover_worker)
        self.cover_worker.cover_found.connect(self._on_cover_art_found)
        self.cover_worker.cover_failed.connect(self._on_cover_art_failed)
        self.cover_worker.start()

    def _fetch_cover_art(self, mbid: str, alternate_mbids: Optional[List[str]] = None) -> None:
        win = self.window()
        if win is not None and getattr(win, "_fetch_cover_art_hook", None) is not None:
            hook = win._fetch_cover_art_hook
            try:
                hook(mbid, alternate_mbids=alternate_mbids)
            except TypeError:
                hook(mbid)
            return
        self._fetch_cover_art_impl(mbid, alternate_mbids=alternate_mbids)

    def _on_cover_art_found(self, mbid: str, image_path: str) -> None:
        if self.session.mbid and mbid != self.session.mbid:
            return
        path = Path(image_path)
        self.session.set_downloaded_art(path)
        if not self.session.art_is_user_selected:
            self._render_cover_art(path)
            self.adapter.set_cover_art(path)
            self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Cover Art Archive thumbnail loaded ({path.name})")

    def _on_cover_art_failed(self, mbid: str) -> None:
        if self.session.mbid and mbid != self.session.mbid:
            return
        if not self.session.art_is_user_selected:
            self.session.clear_art()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._render_cover_art_placeholder("No Cover Art\nin Archive\n\nClick to add")
            self.log_emitted.emit(f"[{self.device_path or 'Drive'}] No cover art found in Cover Art Archive for release {mbid}")

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

        from whipit.audio.metadata import _MAX_ART_BYTES, _image_ext
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
            QMessageBox.warning(self, "Invalid Image", "The selected file is not a valid image or exceeds limits.")
            return

        pix = QPixmap.fromImageReader(reader) if hasattr(QPixmap, "fromImageReader") else QPixmap(str(chosen_path))
        if pix.isNull():
            QMessageBox.warning(self, "Invalid Image", "The selected file is not a valid image format.")
            return

        needs_convert = sniffed is None or sniffed != suffix or chosen_path.stat().st_size > _MAX_ART_BYTES
        if needs_convert:
            from whipit.config import get_cache_dir
            covers_dir = get_cache_dir() / "covers"
            covers_dir.mkdir(parents=True, exist_ok=True)
            conv_path = covers_dir / f"custom_{chosen_path.stem}.jpg"
            img = pix.toImage()
            img.save(str(conv_path), "JPEG", 95)
            target_path = conv_path
        else:
            target_path = chosen_path

        self.session.set_user_art(target_path)
        self._art_chosen_since_commit = True
        self._render_cover_art(target_path)
        self.adapter.set_cover_art(target_path)
        self.reset_art_btn.setVisible(bool(self.session.downloaded_art_path))
        self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Cover art set to: {chosen_path.name}")
        self.phase_detail_lbl.setText(f"✓ Cover art selected: {chosen_path.name}")

    def _reset_cover_art(self) -> None:
        if self.session.downloaded_art_path and self.session.downloaded_art_path.exists():
            self.session.reset_art_to_original()
            if self.session.art_path:
                self._render_cover_art(self.session.art_path)
                self.adapter.set_cover_art(self.session.art_path)
            self.reset_art_btn.setVisible(False)
            self.phase_detail_lbl.setText("✓ Restored Cover Art Archive image.")
        else:
            self.session.clear_art()
            self.session.art_is_user_selected = False
            self._render_cover_art_placeholder()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)

    def _populate_track_table(self, tracks: List[TrackInfo]) -> None:
        if not tracks:
            self.track_table.setVisible(False)
            self.track_placeholder.setVisible(True)
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
        self.track_placeholder.setVisible(False)
        self.tracklist_header_lbl.setText(f"Track Listing ({len(tracks)} Tracks)")

    def _open_edit_metadata_dialog(self) -> None:
        tracks_to_edit = list(self.session.tracks)
        if not tracks_to_edit and self._last_inspection and self._last_inspection.tracks_count:
            tracks_to_edit = [
                TrackInfo(number=i, title=f"Track {i:02d}")
                for i in range(1, self._last_inspection.tracks_count + 1)
            ]

        if not tracks_to_edit:
            show_message_dialog(
                self,
                QMessageBox.Icon.Information,
                "No Disc Loaded",
                "Please insert an Audio CD and wait for it to be detected before editing track titles.",
            )
            return

        dialog_fn = _get_override("prompt_edit_metadata_dialog", prompt_edit_metadata_dialog)
        res = dialog_fn(
            self,
            artist=self.session.artist if self.session.artist != "Unknown Artist" else "",
            album=self.session.title if self.session.title != "Unknown Album" else "",
            year=self.session.year,
            genre=self.session.genre,
            tracks=tracks_to_edit,
        )
        if not res:
            return

        art, alb, yr, gnr, updated_tracks = res
        self.session.artist = art
        self.session.title = alb
        self.session.year = yr
        self.session.genre = gnr
        self.session.tracks = list(updated_tracks)

        self.album_title_lbl.setText(self.session.display_title)
        self._populate_track_table(self.session.tracks)
        if not self.session.mbid:
            self.unknown_chk.setChecked(True)
        self._update_links_display()
        self.phase_detail_lbl.setText("✓ Custom metadata updated. Ready to rip.")
        self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Custom metadata set: {self.session.artist} — {self.session.title}")

    def _update_links_display(self) -> None:
        links = []
        if self.session.mb_url and is_strict_https_url(self.session.mb_url, ("musicbrainz.org",)):
            esc_mb = html.escape(self.session.mb_url, quote=True)
            edition_tag = f" ({self.session.mbid[:8]})" if self.session.mbid else ""
            links.append(
                f'<a href="{esc_mb}" style="color: #22d3ee; text-decoration: none; font-weight: 500;">↗ MusicBrainz{edition_tag}</a>'
            )
        if self.session.discogs_url and is_strict_https_url(self.session.discogs_url, ("discogs.com",)):
            esc_discogs = html.escape(self.session.discogs_url, quote=True)
            links.append(
                f'<a href="{esc_discogs}" style="color: #a7f3d0; text-decoration: none; font-weight: 500;">↗ Discogs</a>'
            )
        if not self.session.mb_url and self.session.mb_lookup_url and is_strict_https_url(self.session.mb_lookup_url, ("musicbrainz.org",)):
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
        elif is_strict_https_url(url, ("musicbrainz.org", "discogs.com")):
            QDesktopServices.openUrl(QUrl(url))

    def copy_musicbrainz_note(self) -> None:
        dev = self.device_path or "/dev/cdrom"
        hw_name = get_drive_hardware_id(dev)
        lines = [
            "Physical CD in hand.",
            f"- Disc ID extracted from optical drive TOC via WhipIt v{__version__} (Whipper backend).",
            f"- Optical Drive: {hw_name}.",
        ]
        if self._last_inspection and self._last_inspection.tracks_count:
            dur_str = f", total duration {self._last_inspection.duration}" if self._last_inspection.duration else ""
            lines.append(f"- {self._last_inspection.tracks_count} audio tracks{dur_str}.")
        lines.append("- Track timings matched against disc TOC.")
        if self.session.discogs_url:
            lines.append(f"- Discogs release reference: {self.session.discogs_url}")

        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText("\n".join(lines))
        self.phase_detail_lbl.setText("✓ MusicBrainz edit note copied to clipboard!")

    def _reset_pipeline_badges(self) -> None:
        self._current_step_key = None
        for badge in self.badge_labels.values():
            badge.setObjectName("stepInactive")
            badge.style().unpolish(badge)
            badge.style().polish(badge)

    def _set_pipeline_step(self, active_key: str, error: bool = False) -> None:
        self._current_step_key = active_key
        if active_key == "COMMITTED":
            self._art_chosen_since_commit = False
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

    def _reset_disc_state(self, clear_user_art: bool = True) -> None:
        self.session.reset(keep_user_art=not clear_user_art and self.session.art_is_user_selected)
        self._scraped_artist = None
        self._scraped_title = None
        self._ripping_started = False
        self._last_inspection = None
        self._release_cache.clear()
        self.release_combo.blockSignals(True)
        self.release_combo.clear()
        self.release_combo.blockSignals(False)
        self.release_combo_row.setVisible(False)
        self.external_links_lbl.setText("")
        self.external_links_lbl.setVisible(False)
        self.track_table.setRowCount(0)
        self.track_table.setVisible(False)
        self.track_placeholder.setVisible(True)
        self.tracklist_header_lbl.setText("Track Listing")
        self.album_title_lbl.setText("Ready to rip Audio CD")
        self.verification_lbl.setText("")
        self.verification_lbl.setVisible(False)

        if clear_user_art or not self.session.art_is_user_selected:
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._render_cover_art_placeholder()

    def identify_disc(self, silent: bool = False) -> None:
        if (self.worker and self.worker.isRunning()) or (self.identify_worker and self.identify_worker.isRunning()):
            return

        missing = get_missing_dependencies()
        if missing:
            if not silent:
                QMessageBox.critical(
                    self,
                    "Missing Dependencies",
                    f"Cannot proceed. Missing required system tools:\n  {', '.join(missing)}",
                )
            return

        dev = self.device_path or "/dev/cdrom"
        dev_short = Path(dev).name if dev else ""
        self._reset_disc_state(clear_user_art=False)
        self._disc_identified = True
        self._set_controls_running(True)
        self._render_cover_art_placeholder("Reading CD TOC...")
        self.start_btn.setEnabled(False)
        self.start_btn.setText(f"Identifying ({dev_short})..." if dev_short else "Identifying...")
        self.status_changed.emit(self.device_path or "", "Identifying...")
        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat("Reading Table of Contents & querying MusicBrainz...")
        self.phase_detail_lbl.setText(f"Inspecting Table of Contents on {dev}...")

        WorkerClass = _get_override("IdentifyWorker", IdentifyWorker)
        self.identify_worker = WorkerClass(dev, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.identify_worker)
        self.identify_worker.line_received.connect(self.log_emitted.emit)
        self.identify_worker.inspection_completed.connect(
            lambda res: self._on_identify_finished(res, silent=silent)
        )
        self.identify_worker.finished.connect(self._clear_identify_worker)
        self.identify_worker.start()

    def _clear_identify_worker(self) -> None:
        self.identify_worker = None
        self._set_controls_running(False)
        dev_short = Path(self.device_path).name if self.device_path else ""
        self.start_btn.setText(f"Start Rip ({dev_short})" if dev_short else "Start Rip")
        self.start_btn.setEnabled(self.is_ready_to_rip)
        if self.rip_progress_bar.format() == "Reading Table of Contents & querying MusicBrainz...":
            self.rip_progress_bar.setRange(0, 100)
            self.rip_progress_bar.setValue(0)
            self.rip_progress_bar.setFormat("Idle")

    def _on_identify_finished(self, result: DiscInspectionResult, silent: bool = False) -> None:
        self._set_controls_running(False)
        self._last_inspection = result
        self.session.set_from_inspection(result)
        dev_short = Path(self.device_path).name if self.device_path else ""
        self.start_btn.setText(f"Start Rip ({dev_short})" if dev_short else "Start Rip")
        self.start_btn.setEnabled(self.is_ready_to_rip)
        if self.rip_progress_bar.format() == "Reading Table of Contents & querying MusicBrainz...":
            self.rip_progress_bar.setRange(0, 100)
            self.rip_progress_bar.setValue(0)
            self.rip_progress_bar.setFormat("Idle")

        if result.success and (result.artist != "Unknown Artist" or result.mb_url):
            self.album_title_lbl.setText(self.session.display_title)
            self.phase_detail_lbl.setText(f"✓ Disc identified: {self.session.display_title}")
            self._set_pipeline_step("DISC_DETECTED")
            self.status_changed.emit(self.device_path or "", "Audio CD")
            self._update_links_display()
            self._populate_track_table(result.tracks)
            if len(self.session.matching_releases) > 1:
                self.release_combo.blockSignals(True)
                self.release_combo.clear()
                for r in self.session.matching_releases:
                    det = []
                    if r.country:
                        det.append(r.country)
                    if r.year:
                        det.append(r.year)
                    if r.cat_no:
                        det.append(f"Cat: {r.cat_no}")
                    extra = f" [{', '.join(det)}]" if det else ""
                    self.release_combo.addItem(f"{r.title}{extra}")
                self.release_combo.setCurrentIndex(0)
                self.release_combo.blockSignals(False)
                self.release_combo_row.setVisible(True)
            else:
                self.release_combo_row.setVisible(False)
            if result.mbid and not self.session.art_is_user_selected:
                alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != result.mbid]
                self._fetch_cover_art(result.mbid, alternate_mbids=alt_mbids)
            elif not self.session.art_is_user_selected:
                self._render_cover_art_placeholder("No Cover Art\n\nClick to add")
        else:
            self.release_combo_row.setVisible(False)
            self.track_table.setVisible(False)
            self.track_placeholder.setVisible(True)
            self.album_title_lbl.setText(self.session.display_title)
            self.status_changed.emit(self.device_path or "", "Audio CD")
            self._set_pipeline_step("DISC_DETECTED")
            self._update_links_display()
            self._render_cover_art_placeholder("No Cover Art\n\nClick to add")
            err = result.error_message or "Could not retrieve disc metadata from MusicBrainz."
            self.phase_detail_lbl.setText(f"⚠ {err}")
            if not silent:
                show_message_dialog(self, QMessageBox.Icon.Information, "Disc Inspection", err)

    def start_rip(self) -> None:
        if (self.worker and self.worker.isRunning()) or (self.identify_worker and self.identify_worker.isRunning()):
            return

        missing = get_missing_dependencies()
        if missing:
            QMessageBox.critical(self, "Missing Dependencies", f"Missing tools: {', '.join(missing)}")
            return

        if self._current_step_key == "COMMITTED":
            self._reset_disc_state(clear_user_art=not self._art_chosen_since_commit)
            self._reset_pipeline_badges()

        dev = self.device_path or "/dev/cdrom"
        speed = self.prefs.default_speed
        manual_offset = self.prefs.offset_override
        flac_root = self.prefs.flac_root
        opus_root = self.prefs.opus_root
        generate_opus = self.prefs.generate_opus
        auto_eject = self.prefs.auto_eject
        selected_release_id = self.session.mbid

        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat("Initializing Whipper...")
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat("Waiting for FLAC extraction...")
        self.verification_lbl.setText("")
        self.verification_lbl.setVisible(False)
        self._ripping_started = False
        self._rip_start_time = time.monotonic()
        self.phase_detail_lbl.setText(f"Starting extraction on {dev}...")
        self.status_changed.emit(self.device_path or "", "Ripping")

        has_custom = bool(
            self.session.artist != "Unknown Artist"
            or self.session.title != "Unknown Album"
            or self.session.year
            or self.session.genre
            or any(tr.title for tr in self.session.tracks)
        )
        init_meta = self.session.to_album_metadata() if has_custom else None

        WorkerClass = _get_override("RipWorker", RipWorker)
        self.worker = WorkerClass(
            ui_adapter=self.adapter,
            device=dev,
            speed=speed,
            manual_offset=manual_offset,
            release_id=selected_release_id,
            output_root=flac_root,
            flac_root=flac_root,
            opus_root=opus_root,
            generate_opus=generate_opus,
            auto_eject=auto_eject,
            keep_going=self.keep_going_chk.isChecked(),
            unknown_disc=self.unknown_chk.isChecked(),
            overwrite=self.overwrite_chk.isChecked(),
            initial_metadata=init_meta,
            parent=None,
        )
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.worker)
        self.worker.workflow_finished.connect(self._on_workflow_finished)
        self.worker.finished.connect(self._clear_rip_worker)
        self.worker.start()
        self._set_controls_running(True)

    def _clear_rip_worker(self) -> None:
        self.worker = None
        self._set_controls_running(False)

    def cancel_rip(self) -> None:
        if self.worker and self.worker.isRunning():
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

    def eject_drive(self) -> None:
        dev = self.device_path or "/dev/cdrom"
        if not dev or (self.eject_worker and self.eject_worker.isRunning()):
            return
        self.eject_btn.setEnabled(False)
        self.phase_detail_lbl.setText(f"Ejecting {dev}...")

        WorkerClass = _get_override("EjectWorker", EjectWorker)
        self.eject_worker = WorkerClass(dev, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.eject_worker)
        self.eject_worker.eject_finished.connect(lambda d: self.phase_detail_lbl.setText(f"Ejected {d}."))
        self.eject_worker.finished.connect(lambda: self.eject_btn.setEnabled(True))
        self.eject_worker.start()

    def _set_controls_running(self, running: bool) -> None:
        self.identify_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.eject_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.edit_meta_btn.setEnabled(not running)

    def _on_log(self, level: LogLevel, message: str) -> None:
        prefix = f"[{level.value}]"
        clean = message.strip("═ \n")
        if clean and level in (LogLevel.INFO, LogLevel.OK, LogLevel.WARN, LogLevel.HEADER):
            self.phase_detail_lbl.setText(clean)
        dev_tag = f"[{self.device_path}] " if self.device_path else ""
        self.log_emitted.emit(f"{dev_tag}{prefix} {message}")

    def _start_release_details_lookup(
        self,
        mbid: str,
        barcode: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        expected_tracks: Optional[int] = None,
    ) -> None:
        if self.details_worker and self.details_worker.isRunning():
            self.details_worker.cancel()

        WorkerClass = _get_override("ReleaseDetailsWorker", ReleaseDetailsWorker)
        self.details_worker = WorkerClass(
            mbid,
            barcode=barcode,
            artist=artist,
            title=title,
            expected_tracks=expected_tracks,
            parent=None,
        )
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.details_worker)
        self.details_worker.details_found.connect(self._on_details_resolved)
        self.details_worker.finished.connect(self._clear_details_worker)
        self.details_worker.start()

    def _clear_details_worker(self) -> None:
        self.details_worker = None

    def _trigger_release_details_lookup(self, mbid: str, **kwargs) -> None:
        win = self.window()
        if win is not None and getattr(win, "_details_lookup_hook", None) is not None:
            win._details_lookup_hook(mbid, **kwargs)
            return
        self._start_release_details_lookup(mbid, **kwargs)

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

    def _update_verification_display(self, v) -> None:
        if not v:
            return
        summary = getattr(v, "accuraterip_summary", None) or ""
        is_acc = getattr(v, "is_accurate", False)
        health = getattr(v, "health_status", None)
        text, style, tip = format_accuraterip_verdict(summary, is_accurate=is_acc, health_status=health)
        if text:
            self.verification_lbl.setText(text)
            self.verification_lbl.setStyleSheet(style)
            self.verification_lbl.setToolTip(tip)
            self.verification_lbl.setVisible(True)
            if "Verified" in text:
                if "100%" in text or "All tracks" in text:
                    if self.rip_progress_bar.value() == 100:
                        self.rip_progress_bar.setFormat("AccurateRip Verified (100%)")
                else:
                    m = re.search(r"(\d+/\d+)", text)
                    if self.rip_progress_bar.value() == 100:
                        tag = f"Verified ({m.group(1)} Tracks)" if m else "Verified"
                        self.rip_progress_bar.setFormat(f"AccurateRip {tag}")
            elif "not present" in text.lower():
                if self.rip_progress_bar.value() == 100:
                    self.rip_progress_bar.setFormat("Extraction Complete — Not in AccurateRip (100%)")

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

        mbid = manifest.metadata.musicbrainz_id
        if mbid:
            if not self.session.mbid:
                self.session.mbid = mbid
            if not self.session.art_path and not self.session.art_is_user_selected:
                alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != mbid]
                self._fetch_cover_art(mbid, alternate_mbids=alt_mbids)
            if self.track_table.rowCount() == 0 or not self.session.tracks:
                if mbid in self._release_cache and self._release_cache[mbid][1]:
                    cached_discogs, cached_tracks = self._release_cache[mbid]
                    self.session.discogs_url = cached_discogs or self.session.discogs_url
                    self.session.tracks = list(cached_tracks)
                    self._update_links_display()
                    self._populate_track_table(cached_tracks)
                else:
                    self._trigger_release_details_lookup(
                        mbid,
                        artist=manifest.metadata.artist,
                        title=manifest.metadata.album,
                        expected_tracks=manifest.tracks.expected_count or None,
                    )

        self._update_links_display()

        for key, _, states in PIPELINE_STEPS:
            if state in states:
                self._set_pipeline_step(key)
                break

        if state in (
            WorkflowState.RIP_VALIDATED,
            WorkflowState.CANDIDATE_STAGED,
            WorkflowState.OPUS_ENCODED,
            WorkflowState.REPLAYGAIN_APPLIED,
            WorkflowState.CANDIDATE_VERIFIED,
            WorkflowState.PUBLISHING,
            WorkflowState.COMMITTED,
        ):
            self._update_verification_display(manifest.verification)

        if state == WorkflowState.RIP_EXTRACTED:
            self.rip_progress_bar.setRange(0, 100)
            self.rip_progress_bar.setValue(100)
            self.rip_progress_bar.setFormat("Extraction Complete (100%)")
        elif state == WorkflowState.COMMITTED:
            self._set_pipeline_step("COMMITTED")
            self.status_changed.emit(self.device_path or "", "Committed")
        elif state == WorkflowState.ABORTED:
            step_to_mark = self._current_step_key or "DISC_DETECTED"
            self._set_pipeline_step(step_to_mark, error=True)
            self.status_changed.emit(self.device_path or "", "Aborted")

    def _on_whipper_output(self, line: str) -> None:
        try:
            ev = self._whipper_parser.parse_line(line)
            if ev:
                self._on_whipper_event(ev)
        except Exception:
            pass

    def _on_whipper_event(self, event: WhipperEvent) -> None:
        if isinstance(event, DiscTOCEvent):
            if event.lookup_url:
                self.session.mb_lookup_url = event.lookup_url
            if event.duration and not self.session.duration:
                self.session.duration = event.duration
            self._update_links_display()
        elif isinstance(event, ReleasePickedEvent):
            self.session.mbid = event.mbid
            self.session.mb_url = f"https://musicbrainz.org/release/{event.mbid}"
            self._update_links_display()
            if not self.session.art_is_user_selected and not self.session.art_path:
                alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != event.mbid]
                self._fetch_cover_art(event.mbid, alternate_mbids=alt_mbids)
            if self.track_table.rowCount() == 0 or not self.session.tracks:
                if event.mbid in self._release_cache and self._release_cache[event.mbid][1]:
                    cached_discogs, cached_tracks = self._release_cache[event.mbid]
                    self.session.discogs_url = cached_discogs or self.session.discogs_url
                    self.session.tracks = list(cached_tracks)
                    self._update_links_display()
                    self._populate_track_table(cached_tracks)
                else:
                    self._trigger_release_details_lookup(
                        event.mbid,
                        artist=self.session.artist if self.session.artist != "Unknown Artist" else None,
                        title=self.session.title if self.session.title != "Unknown Album" else None,
                    )
        elif isinstance(event, AccurateRipSummaryEvent):
            text, style, tip = format_accuraterip_verdict(event.summary, is_accurate=event.is_accurate)
            if text:
                self.verification_lbl.setText(text)
                self.verification_lbl.setStyleSheet(style)
                self.verification_lbl.setToolTip(tip)
                self.verification_lbl.setVisible(True)
                if "Verified" in text:
                    if "100%" in text or "All tracks" in text:
                        self.rip_progress_bar.setFormat("AccurateRip Verified (100%)")
                    else:
                        m = re.search(r"(\d+/\d+)", text)
                        tag = f"Verified ({m.group(1)} Tracks)" if m else "Verified"
                        self.rip_progress_bar.setFormat(f"AccurateRip {tag}")
        elif isinstance(event, TrackProgressEvent):
            self._ripping_started = True
            self.rip_progress_bar.setRange(0, event.total_tracks)
            self.rip_progress_bar.setValue(event.current_track - 1)
            pct = int(event.percent)
            self.rip_progress_bar.setFormat(
                f"{event.phase} Track {event.current_track} of {event.total_tracks}... ({pct}%)"
            )
            self.phase_detail_lbl.setText(f"{event.phase} Track {event.current_track} / {event.total_tracks}...")
            self.status_changed.emit(self.device_path or "", f"Ripping {pct}%")
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
        if ready is True:
            self.phase_detail_lbl.setText(f"Disc ready in {device}.")
        elif ready is False:
            self.phase_detail_lbl.setText(f"Timeout waiting for disc in {device}.")

    def _on_workflow_finished(self, success: bool, message: str) -> None:
        self._set_controls_running(False)
        self.rip_progress_bar.setRange(0, 100)
        self.track_table.clearSelection()
        if success:
            self.rip_progress_bar.setValue(100)
            self.opus_progress_bar.setValue(self.opus_progress_bar.maximum() or 100)
            self._set_pipeline_step("COMMITTED")
            lbl_txt = self.verification_lbl.text()
            if "Verified" in lbl_txt:
                if "100%" in lbl_txt or "All tracks" in lbl_txt:
                    self.rip_progress_bar.setFormat("AccurateRip Verified (100%)")
                    self.status_changed.emit(self.device_path or "", "Verified")
                else:
                    m = re.search(r"(\d+/\d+)", lbl_txt)
                    tag = f"Verified {m.group(1)}" if m else "Verified"
                    self.rip_progress_bar.setFormat(f"AccurateRip {tag}")
                    self.status_changed.emit(self.device_path or "", tag)
            else:
                self.status_changed.emit(self.device_path or "", "Committed")
            self.phase_detail_lbl.setText(message)
        else:
            self.rip_progress_bar.setFormat("Stopped")
            self.phase_detail_lbl.setText(message)
            self.status_changed.emit(self.device_path or "", "Stopped")

    def _on_release_combo_changed(self, idx: int) -> None:
        if not self.session.matching_releases or idx < 0 or idx >= len(self.session.matching_releases):
            return
        rel = self.session.matching_releases[idx]
        self.session.select_release(rel)
        self.album_title_lbl.setText(self.session.display_title)
        self._update_links_display()
        if rel.tracks:
            self._populate_track_table(rel.tracks)
        elif rel.mbid in self._release_cache and self._release_cache[rel.mbid][1]:
            cached_discogs, cached_tracks = self._release_cache[rel.mbid]
            self.session.discogs_url = cached_discogs or self.session.discogs_url
            self.session.tracks = list(cached_tracks)
            self._update_links_display()
            self._populate_track_table(cached_tracks)
        elif rel.mbid:
            self._trigger_release_details_lookup(
                rel.mbid,
                barcode=rel.barcode,
                artist=rel.artist if rel.artist != "Unknown Artist" else None,
                title=rel.title if rel.title != "Unknown Album" else None,
                expected_tracks=self._last_inspection.tracks_count if self._last_inspection else None,
            )
        if rel.mbid and not self.session.art_is_user_selected:
            alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != rel.mbid]
            self._fetch_cover_art(rel.mbid, alternate_mbids=alt_mbids)

    def _on_prompt_requested(self, *args) -> None:
        if len(args) == 1 and hasattr(args[0], "prompt_type"):
            request = args[0]
            if request.is_resolved:
                return
            try:
                res = self._dispatch_modal_prompt(request.prompt_type, request.payload)
            except Exception as exc:
                res = GuiInteractionAdapter._default_for_prompt(request.prompt_type)
            request.resolve(res)

    def _dispatch_modal_prompt(self, prompt_type: str, payload: Any) -> Any:
        if prompt_type == "prompt_insert_disc":
            dev, hw_id, avail, menu = payload
            return prompt_insert_disc_dialog(self, dev, hw_id, avail, menu)
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
                if self.release_combo_row.isVisible() and self.release_combo.count() > idx:
                    self.release_combo.blockSignals(True)
                    self.release_combo.setCurrentIndex(idx)
                    self.release_combo.blockSignals(False)
                if not self.session.art_is_user_selected:
                    alt_mbids = [r.mbid for r in releases if r.mbid and r.mbid != chosen.mbid]
                    self._fetch_cover_art(chosen.mbid, alternate_mbids=alt_mbids)
                return chosen.mbid
            return None
        if prompt_type == "prompt_empty_multi_drives":
            return prompt_empty_multi_drives_dialog(self, payload)
        if prompt_type == "prompt_calibration_start":
            return prompt_calibration_start_dialog(self, payload)
        if prompt_type == "prompt_calibration_retry":
            return prompt_calibration_retry_dialog(self, payload)
        if prompt_type == "prompt_extraction_error":
            dev, avail = payload
            return prompt_extraction_error_dialog(self, dev, avail)
        if prompt_type == "prompt_conflict_resolution":
            flac_dir, opus_dir, warn = payload if isinstance(payload, tuple) and len(payload) == 3 else (*payload, None)
            return prompt_conflict_resolution_dialog(self, flac_dir, opus_dir, warn)
        return GuiInteractionAdapter._default_for_prompt(prompt_type)
