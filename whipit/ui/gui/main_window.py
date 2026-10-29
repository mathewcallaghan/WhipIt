"""Main application window and multi-drive container for WhipIt."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QProcess, QThread, QTimer, Qt
from PyQt6.QtGui import QFont, QGuiApplication, QIcon, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.config import (
    UserPreferences,
    eject_drive,
    get_drives_with_hardware_ids,
    get_drive_max_speed,
    load_preferences,
)
from whipit.launcher import get_icon_path, install_icon, resolve_executable_path
from whipit.models import JobManifest, ReleaseOption, TrackInfo, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.ripping.identify import DiscInspectionResult
from whipit.ripping.whipper import WhipperEvent
from whipit.state_store import ManifestStore
from whipit.ui.base import LogLevel
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.ui.gui.dialogs import (
    prompt_edit_metadata_dialog,
    prompt_preferences_dialog,
    prompt_recover_job_dialog,
    prompt_restart_dialog,
    show_about_dialog,
    show_diagnostic_dialog,
    show_message_dialog,
)
from whipit.ui.gui.drive_panel import PIPELINE_STEPS, DrivePanel
from whipit.ui.gui.session import ActiveSession
from whipit.ui.gui.styles import (
    STATUS_BADGE_AUDIO,
    STATUS_BADGE_EMPTY,
    STATUS_BADGE_NO_DRIVE,
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
    "_track_detached_worker",
    "prompt_edit_metadata_dialog",
    "eject_drive",
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

        self.prefs: UserPreferences = load_preferences()
        self.poll_worker: Optional[DrivePollWorker] = None
        self.startup_worker: Optional[StartupCheckWorker] = None
        self.update_worker: Optional[UpdateWorker] = None
        self.resume_worker: Optional[ResumeWorker] = None
        self.discard_worker: Optional[DiscardWorker] = None

        self.drive_panels: Dict[str, DrivePanel] = {}
        self._fallback_panel = DrivePanel(device=None, prefs=self.prefs, parent=self)
        self._fallback_panel.log_emitted.connect(self._append_console)

        self._setup_ui()
        self.setStyleSheet(get_application_stylesheet())
        self._apply_preferences(self.prefs)

        self.refresh_drives()
        self.drive_poll_timer = QTimer(self)
        self.drive_poll_timer.setInterval(2500)
        self.drive_poll_timer.timeout.connect(self._trigger_drive_poll)
        self.drive_poll_timer.start()

        QTimer.singleShot(250, self._startup_checks)

    # -------------------------------------------------------------------------
    # Test Hooks (Directly Intercepts win Without Touching Other Tabs)
    # -------------------------------------------------------------------------
    @property
    def _fetch_cover_art_hook(self):
        if "_fetch_cover_art" in self.__dict__:
            return self.__dict__["_fetch_cover_art"]
        return None

    @property
    def _details_lookup_hook(self):
        if "_start_release_details_lookup" in self.__dict__:
            return self.__dict__["_start_release_details_lookup"]
        return None

    def _fetch_cover_art(self, mbid: str, alternate_mbids: Optional[List[str]] = None) -> None:
        pass

    def _start_release_details_lookup(
        self,
        mbid: str,
        barcode: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        expected_tracks: Optional[int] = None,
    ) -> None:
        pass

    # -------------------------------------------------------------------------
    # Multi-Drive Delegation Properties
    # -------------------------------------------------------------------------
    @property
    def drive_panel(self) -> DrivePanel:
        widget = self.tab_widget.currentWidget()
        if isinstance(widget, DrivePanel):
            return widget
        if self.drive_panels:
            return next(iter(self.drive_panels.values()))
        return self._fallback_panel

    @property
    def disc_status_badge(self) -> QLabel:
        return self.drive_panel.disc_status_badge

    @property
    def session(self) -> ActiveSession:
        return self.drive_panel.session

    @property
    def adapter(self) -> GuiInteractionAdapter:
        return self.drive_panel.adapter

    @property
    def track_table(self) -> QTableWidget:
        return self.drive_panel.track_table

    @property
    def album_title_lbl(self) -> QLabel:
        return self.drive_panel.album_title_lbl

    @property
    def verification_lbl(self) -> QLabel:
        return self.drive_panel.verification_lbl

    @property
    def external_links_lbl(self) -> QLabel:
        return self.drive_panel.external_links_lbl

    @property
    def rip_progress_bar(self) -> QProgressBar:
        return self.drive_panel.rip_progress_bar

    @property
    def opus_progress_bar(self) -> QProgressBar:
        return self.drive_panel.opus_progress_bar

    @property
    def keep_going_chk(self) -> QCheckBox:
        return self.drive_panel.keep_going_chk

    @property
    def unknown_chk(self) -> QCheckBox:
        return self.drive_panel.unknown_chk

    @property
    def overwrite_chk(self) -> QCheckBox:
        return self.drive_panel.overwrite_chk

    @property
    def identify_btn(self) -> QPushButton:
        return self.drive_panel.identify_btn

    @property
    def start_btn(self) -> QPushButton:
        return self.drive_panel.start_btn

    @property
    def eject_btn(self) -> QPushButton:
        return self.drive_panel.eject_btn

    @property
    def cancel_btn(self) -> QPushButton:
        return self.drive_panel.cancel_btn

    @property
    def worker(self):
        return self.drive_panel.worker

    @worker.setter
    def worker(self, val):
        self.drive_panel.worker = val

    @property
    def identify_worker(self):
        return self.drive_panel.identify_worker

    @identify_worker.setter
    def identify_worker(self, val):
        self.drive_panel.identify_worker = val

    @property
    def details_worker(self):
        return self.drive_panel.details_worker

    @details_worker.setter
    def details_worker(self, val):
        self.drive_panel.details_worker = val

    @property
    def cover_worker(self):
        return self.drive_panel.cover_worker

    @cover_worker.setter
    def cover_worker(self, val):
        self.drive_panel.cover_worker = val

    @property
    def eject_worker(self):
        return self.drive_panel.eject_worker

    @eject_worker.setter
    def eject_worker(self, val):
        self.drive_panel.eject_worker = val

    @property
    def _art_chosen_since_commit(self) -> bool:
        return self.drive_panel._art_chosen_since_commit

    @_art_chosen_since_commit.setter
    def _art_chosen_since_commit(self, val: bool) -> None:
        self.drive_panel._art_chosen_since_commit = val

    @property
    def _current_art_path(self) -> Optional[Path]:
        return self.drive_panel._current_art_path

    @_current_art_path.setter
    def _current_art_path(self, val: Optional[Path]) -> None:
        self.drive_panel._current_art_path = val

    @property
    def _current_mbid(self) -> Optional[str]:
        return self.drive_panel._current_mbid

    @_current_mbid.setter
    def _current_mbid(self, val: Optional[str]) -> None:
        self.drive_panel._current_mbid = val

    @property
    def _current_mb_url(self) -> Optional[str]:
        return self.drive_panel._current_mb_url

    @_current_mb_url.setter
    def _current_mb_url(self, val: Optional[str]) -> None:
        self.drive_panel._current_mb_url = val

    @property
    def _current_discogs_url(self) -> Optional[str]:
        return self.drive_panel._current_discogs_url

    @_current_discogs_url.setter
    def _current_discogs_url(self, val: Optional[str]) -> None:
        self.drive_panel._current_discogs_url = val

    @property
    def _current_mb_lookup_url(self) -> Optional[str]:
        return self.drive_panel._current_mb_lookup_url

    @_current_mb_lookup_url.setter
    def _current_mb_lookup_url(self, val: Optional[str]) -> None:
        self.drive_panel._current_mb_lookup_url = val

    @property
    def _art_is_user_selected(self) -> bool:
        return self.drive_panel._art_is_user_selected

    @_art_is_user_selected.setter
    def _art_is_user_selected(self, val: bool) -> None:
        self.drive_panel._art_is_user_selected = val

    @property
    def _downloaded_art_path(self) -> Optional[Path]:
        return self.drive_panel._downloaded_art_path

    @_downloaded_art_path.setter
    def _downloaded_art_path(self, val: Optional[Path]) -> None:
        self.drive_panel._downloaded_art_path = val

    @property
    def _matching_releases(self) -> List[ReleaseOption]:
        return self.drive_panel._matching_releases

    @_matching_releases.setter
    def _matching_releases(self, val: List[ReleaseOption]) -> None:
        self.drive_panel._matching_releases = val

    # -------------------------------------------------------------------------
    # UI Setup
    # -------------------------------------------------------------------------
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

        # Menu bar
        menubar = self.menuBar()
        edit_menu = menubar.addMenu("&Edit")
        prefs_act = edit_menu.addAction("&Preferences...")
        prefs_act.setShortcut("Ctrl+,")
        prefs_act.triggered.connect(self.open_preferences)

        help_menu = menubar.addMenu("&Help")
        check_update_act = help_menu.addAction("Check for &Updates...")
        check_update_act.triggered.connect(self.check_for_updates)

        diag_act = help_menu.addAction("System &Diagnostics...")
        diag_act.triggered.connect(self.show_diagnostics)

        help_menu.addSeparator()
        about_act = help_menu.addAction("&About WhipIt")
        about_act.triggered.connect(self.show_about)

        # Unified Batch & Drive Operations Header
        batch_group = QGroupBox("Batch Multi-Drive Operations")
        batch_layout = QHBoxLayout(batch_group)
        batch_layout.setContentsMargins(10, 8, 10, 8)
        batch_layout.setSpacing(10)

        self.rip_all_btn = QPushButton("💿 Rip All Ready Discs")
        self.rip_all_btn.setObjectName("primaryButton")
        self.rip_all_btn.setToolTip("Starts simultaneous extraction across all drives with an Audio CD loaded")
        self.rip_all_btn.clicked.connect(self.rip_all_ready_discs)
        batch_layout.addWidget(self.rip_all_btn)

        self.eject_all_btn = QPushButton("⏏ Eject All Drives")
        self.eject_all_btn.setToolTip("Ejects all optical drive trays simultaneously")
        self.eject_all_btn.clicked.connect(self.eject_all_drives)
        batch_layout.addWidget(self.eject_all_btn)

        self.refresh_btn = QPushButton("🔄 Refresh Drives")
        self.refresh_btn.setToolTip("Re-scans for newly connected or disconnected optical drives")
        self.refresh_btn.clicked.connect(self.refresh_drives)
        batch_layout.addWidget(self.refresh_btn)

        self.batch_info_lbl = QLabel("Detecting optical drives...")
        self.batch_info_lbl.setStyleSheet("color: #38bdf8; font-weight: 500; font-size: 11px;")
        batch_layout.addWidget(self.batch_info_lbl, stretch=1)

        root_layout.addWidget(batch_group)

        # Hidden drive_combo preserved for backwards-compatibility with test suites
        self.drive_combo = QComboBox(self)
        self.drive_combo.setVisible(False)
        self.drive_combo.currentIndexChanged.connect(self._on_drive_combo_changed)

        # Multi-Drive QTabWidget (The tabs ARE the drive selector)
        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(True)
        self.tab_widget.currentChanged.connect(self._on_tab_changed)
        root_layout.addWidget(self.tab_widget)

        # Live Console
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

    def _format_tab_title(self, dev: str, status: str = "Ready") -> str:
        icon_marker = "⚪"
        if "identif" in status.lower():
            icon_marker = "🔍"
        elif "audio" in status.lower() or "disc" in status.lower():
            icon_marker = "🟢"
        elif "rip" in status.lower():
            icon_marker = "💿"
        elif "verified" in status.lower() or "accurate" in status.lower() or "commit" in status.lower():
            icon_marker = "✓"
        elif "error" in status.lower() or "stop" in status.lower():
            icon_marker = "⚠"
        dev_name = Path(dev).name
        clean_status = "Identifying..." if "identif" in status.lower() else status
        return f"{icon_marker} {dev_name} [{clean_status}]"

    def _detect_multi_disc_sets(self) -> Optional[str]:
        ready_panels = [p for p in self.drive_panels.values() if p.session.artist != "Unknown Artist"]
        if len(ready_panels) < 2:
            return None

        by_album: Dict[str, List[DrivePanel]] = {}
        for p in ready_panels:
            key = f"{p.session.artist.lower()} - {p.session.title.lower()}"
            by_album.setdefault(key, []).append(p)

        for key, panels in by_album.items():
            if len(panels) >= 2:
                art = panels[0].session.artist
                alb = panels[0].session.title
                discs_info = [f"Disc {p.session.disc_number or i+1} on {Path(p.device_path or '').name}" for i, p in enumerate(panels)]
                return f"Multi-disc set detected: {art} — {alb} ({', '.join(discs_info)})"
        return None

    def _update_batch_bar(self) -> None:
        total_drives = len(self.drive_panels)
        if total_drives == 0:
            self.rip_all_btn.setEnabled(False)
            self.eject_all_btn.setEnabled(False)
            self.batch_info_lbl.setText("No optical drives connected")
            return

        self.eject_all_btn.setEnabled(True)
        ready_panels = [
            p for p in self.drive_panels.values()
            if p.is_ready_to_rip
        ]
        active_rips = [p for p in self.drive_panels.values() if _is_worker_running(p.worker)]
        active_identifies = [p for p in self.drive_panels.values() if _is_worker_running(p.identify_worker)]

        multi_set = self._detect_multi_disc_sets()
        if multi_set:
            self.batch_info_lbl.setText(f"✓ {multi_set}")
        elif active_rips:
            self.batch_info_lbl.setText(f"Active extractions: {len(active_rips)} ripping, {len(ready_panels)} ready to rip")
        elif active_identifies:
            self.batch_info_lbl.setText(f"Identifying {len(active_identifies)} disc(s)...")
        elif ready_panels:
            self.batch_info_lbl.setText(f"Ready: {len(ready_panels)} of {total_drives} drive(s) have Audio CDs loaded")
        else:
            self.batch_info_lbl.setText(f"{total_drives} drive(s) connected; waiting for Audio CD insertion")

        self.rip_all_btn.setEnabled(len(ready_panels) > 0)

    def rip_all_ready_discs(self) -> None:
        ready_panels = [
            p for p in self.drive_panels.values()
            if p.is_ready_to_rip
        ]
        if not ready_panels:
            QMessageBox.information(
                self,
                "No Ready Discs",
                "No idle drives with Audio CDs loaded were found.",
            )
            return

        dev_names = ", ".join(p.device_path or "" for p in ready_panels)
        self._append_console(f"[BATCH] Starting concurrent extraction across {len(ready_panels)} drive(s): {dev_names}")

        for panel in ready_panels:
            panel.start_rip()
        self._update_batch_bar()

    def eject_all_drives(self) -> None:
        if not self.drive_panels:
            return
        self._append_console(f"[BATCH] Ejecting all {len(self.drive_panels)} drive trays...")
        for panel in self.drive_panels.values():
            panel.eject_drive()
        self._update_batch_bar()

    def _on_panel_status_changed(self, dev: str, status_text: str) -> None:
        for idx in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(idx)
            if isinstance(widget, DrivePanel) and widget.device_path == dev:
                self.tab_widget.setTabText(idx, self._format_tab_title(dev, status_text))
                break
        self._update_batch_bar()

    def _on_tab_changed(self, idx: int) -> None:
        panel = self.drive_panel
        dev = panel.device_path
        if dev and self.drive_combo.count() > 0:
            combo_idx = self.drive_combo.findData(dev)
            if combo_idx >= 0 and self.drive_combo.currentIndex() != combo_idx:
                self.drive_combo.blockSignals(True)
                self.drive_combo.setCurrentIndex(combo_idx)
                self.drive_combo.blockSignals(False)
        self._update_batch_bar()

    def _on_drive_combo_changed(self) -> None:
        dev = self.drive_combo.currentData()
        if not dev:
            return
        for idx in range(self.tab_widget.count()):
            widget = self.tab_widget.widget(idx)
            if isinstance(widget, DrivePanel) and widget.device_path == dev:
                if self.tab_widget.currentIndex() != idx:
                    self.tab_widget.setCurrentIndex(idx)
                break
        self._trigger_drive_poll()

    def refresh_drives(self) -> None:
        current_dev = self.drive_combo.currentData()
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()

        drives = get_drives_with_hardware_ids()
        if not drives:
            self.drive_combo.addItem("No optical drives detected", None)
            if self.tab_widget.count() == 0:
                self.tab_widget.addTab(self._fallback_panel, "No Drives Detected")
        else:
            detected_devs = [d for d, _ in drives]

            for dev in list(self.drive_panels.keys()):
                if dev not in detected_devs:
                    panel = self.drive_panels[dev]
                    if not (_is_worker_running(panel.worker)):
                        for idx in range(self.tab_widget.count()):
                            if self.tab_widget.widget(idx) is panel:
                                self.tab_widget.removeTab(idx)
                                break
                        del self.drive_panels[dev]

            for idx in range(self.tab_widget.count()):
                if self.tab_widget.widget(idx) is self._fallback_panel:
                    self.tab_widget.removeTab(idx)
                    break

            for dev, hw in drives:
                max_spd = get_drive_max_speed(dev)
                spd_str = f" [Max {max_spd}x]" if max_spd else ""
                self.drive_combo.addItem(f"{dev} ({hw}{spd_str})", dev)

                if dev not in self.drive_panels:
                    panel = DrivePanel(device=dev, prefs=self.prefs, parent=self)
                    panel.log_emitted.connect(self._append_console)
                    panel.status_changed.connect(self._on_panel_status_changed)
                    self.drive_panels[dev] = panel
                    self.tab_widget.addTab(panel, self._format_tab_title(dev, "Ready"))

        if current_dev:
            idx = self.drive_combo.findData(current_dev)
            if idx >= 0:
                self.drive_combo.setCurrentIndex(idx)

        self.drive_combo.blockSignals(False)
        self._update_batch_bar()
        self._trigger_drive_poll()

    def _clear_poll_worker(self) -> None:
        self.poll_worker = None

    def _trigger_drive_poll(self) -> None:
        if _is_worker_running(self.poll_worker):
            return

        devices_to_poll = list(self.drive_panels.keys())
        if not devices_to_poll:
            dev = self.drive_combo.currentData()
            if dev:
                devices_to_poll = [dev]

        if not devices_to_poll:
            return

        self.poll_worker = DrivePollWorker(devices_to_poll, parent=None)
        _track_detached_worker(self.poll_worker)
        self.poll_worker.status_polled.connect(self._on_drive_status_polled)
        self.poll_worker.finished.connect(self._clear_poll_worker)
        self.poll_worker.start()

    def _on_drive_status_polled(self, dev: str, drive_st: int, disc_st: int) -> None:
        if dev in self.drive_panels:
            self.drive_panels[dev].handle_drive_status(drive_st, disc_st)
        elif dev == self.drive_combo.currentData():
            self._fallback_panel.handle_drive_status(drive_st, disc_st)
        self._update_batch_bar()

    def _startup_checks(self) -> None:
        out_root = self.prefs.flac_root.resolve()
        self.startup_worker = StartupCheckWorker(self.adapter, out_root, parent=None)
        _track_detached_worker(self.startup_worker)
        self.startup_worker.checks_completed.connect(self._on_startup_checks_completed)
        self.startup_worker.finished.connect(self._clear_startup_worker)
        self.startup_worker.start()

    def _clear_startup_worker(self) -> None:
        self.startup_worker = None

    def _on_startup_checks_completed(
        self,
        all_ok: bool,
        missing: List[str],
        preserved_jobs: List[Tuple[JobManifest, str]],
        cleanup_actions: List[str],
    ) -> None:
        for act in cleanup_actions:
            self._append_console(f"[STARTUP] {act}")

        store = ManifestStore()
        coordinator = RecoveryCoordinator(store, ui=self.adapter)

        for manifest, status in preserved_jobs:
            user_choice = prompt_recover_job_dialog(self, manifest, status)
            if user_choice == "resume":
                self.resume_worker = ResumeWorker(coordinator, manifest.job_id, self.adapter, parent=None)
                _track_detached_worker(self.resume_worker)
                self.resume_worker.resume_finished.connect(
                    lambda ok, acts: [self._append_console(f"[RECOVERY] {a}") for a in acts]
                )
                self.resume_worker.start()
                break
            elif user_choice == "discard":
                self.discard_worker = DiscardWorker(coordinator, manifest.job_id, manifest.metadata.album_stem, parent=None)
                _track_detached_worker(self.discard_worker)
                self.discard_worker.discard_finished.connect(
                    lambda ok, acts, stem: [self._append_console(f"[RECOVERY] {a}") for a in acts]
                )
                self.discard_worker.start()

    def _toggle_console(self, checked: bool) -> None:
        self.console_box.setVisible(checked)
        self.console_toggle_btn.setText(
            "▼ Hide Live Whipper & Activity Console" if checked else "▶ Show Live Whipper & Activity Console"
        )

    def _append_console(self, text: str) -> None:
        self.console_box.moveCursor(QTextCursor.MoveOperation.End)
        self.console_box.insertPlainText(text if text.endswith("\n") else f"{text}\n")
        self.console_box.moveCursor(QTextCursor.MoveOperation.End)

    def _apply_preferences(self, prefs: UserPreferences) -> None:
        self.prefs = prefs
        self._fallback_panel.apply_preferences(prefs)
        for panel in self.drive_panels.values():
            panel.apply_preferences(prefs)
        speed_str = f"{prefs.default_speed}x" if prefs.default_speed else "Full Speed"
        offset_str = f"{prefs.offset_override:+d}" if prefs.offset_override is not None else "Auto"
        self._append_console(
            f"[CONFIG] Preferences updated (FLAC: {prefs.flac_root}, "
            f"Opus: {'Enabled (' + str(prefs.opus_root) + ')' if prefs.generate_opus else 'Disabled'}, "
            f"Speed: {speed_str}, Offset: {offset_str}, Auto-eject: {prefs.auto_eject})"
        )

    # Delegating methods
    def _reset_pipeline_badges(self) -> None:
        self.drive_panel._reset_pipeline_badges()

    def _set_pipeline_step(self, active_key: str, error: bool = False) -> None:
        self.drive_panel._set_pipeline_step(active_key, error=error)

    def _update_links_display(self) -> None:
        self.drive_panel._update_links_display()

    def _on_cover_art_found(self, mbid: str, image_path: str) -> None:
        self.drive_panel._on_cover_art_found(mbid, image_path)

    def _on_cover_art_failed(self, mbid: str) -> None:
        self.drive_panel._on_cover_art_failed(mbid)

    def _choose_custom_art(self) -> None:
        self.drive_panel._choose_custom_art()

    def _reset_cover_art(self) -> None:
        self.drive_panel._reset_cover_art()

    def _open_edit_metadata_dialog(self) -> None:
        self.drive_panel._open_edit_metadata_dialog()

    def _on_identify_finished(self, result: DiscInspectionResult, silent: bool = False) -> None:
        self.drive_panel._on_identify_finished(result, silent=silent)

    def _on_whipper_output(self, line: str) -> None:
        self.drive_panel._on_whipper_output(line)

    def _on_whipper_event(self, event: WhipperEvent) -> None:
        self.drive_panel._on_whipper_event(event)

    def _on_state_changed(self, state: WorkflowState, manifest: JobManifest) -> None:
        self.drive_panel._on_state_changed(state, manifest)

    def _dispatch_modal_prompt(self, prompt_type: str, payload: Any) -> Any:
        return self.drive_panel._dispatch_modal_prompt(prompt_type, payload)

    def identify_disc(self, silent: bool = False) -> None:
        self.drive_panel.identify_disc(silent=silent)

    def start_rip(self) -> None:
        self.drive_panel.start_rip()

    def cancel_rip(self) -> None:
        self.drive_panel.cancel_rip()

    def eject_selected_drive(self) -> None:
        self.drive_panel.eject_drive()

    def check_for_updates(self) -> None:
        if any(_is_worker_running(p.worker) for p in self.drive_panels.values()):
            QMessageBox.warning(self, "Extraction Active", "Cannot check for updates while extraction is active.")
            return
        if _is_worker_running(self.update_worker):
            return
        self._append_console("[INFO] Checking for updates...")
        self.update_worker = UpdateWorker(parent=None)
        _track_detached_worker(self.update_worker)
        self.update_worker.update_finished.connect(self._on_update_finished)
        self.update_worker.start()

    def _on_update_finished(self, success: bool, message: str, restart_needed: bool) -> None:
        self.update_worker = None
        if success:
            self._append_console(f"[OK] {message}")
            if restart_needed and prompt_restart_dialog(self, message):
                exe = resolve_executable_path()
                if exe.endswith(" -m whipit"):
                    QProcess.startDetached(exe[: -len(" -m whipit")], ["-m", "whipit"])
                else:
                    QProcess.startDetached(exe, [])
                app = QApplication.instance()
                if app:
                    app.quit()
            elif not restart_needed:
                show_message_dialog(self, QMessageBox.Icon.Information, "Updates", message)
        else:
            self._append_console(f"[ERROR] {message}")
            show_message_dialog(self, QMessageBox.Icon.Warning, "Update Failed", message)

    def show_about(self) -> None:
        show_about_dialog(self)

    def show_diagnostics(self) -> None:
        show_diagnostic_dialog(self, selected_device=self.drive_panel.device_path)

    def open_preferences(self) -> None:
        updated = prompt_preferences_dialog(self, prefs=self.prefs)
        if updated:
            self._apply_preferences(updated)

    def closeEvent(self, event) -> None:
        try:
            active_panels = [p for p in self.drive_panels.values() if _is_worker_running(p.worker)]
            if active_panels:
                ans = QMessageBox.question(
                    self,
                    "Extraction in Progress",
                    f"{len(active_panels)} rip workflow(s) are currently active.\nDo you want to cancel all extractions and exit?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                for p in active_panels:
                    p.cancel_rip()

            for p in self.drive_panels.values():
                p.adapter.shutdown()
            self._fallback_panel.adapter.shutdown()

            if hasattr(self, "drive_poll_timer") and self.drive_poll_timer.isActive():
                self.drive_poll_timer.stop()

            for w in (self.startup_worker, self.update_worker, self.resume_worker, self.discard_worker):
                if _is_worker_running(w) and hasattr(w, "cancel"):
                    w.cancel()
        except Exception:
            pass
        event.accept()
