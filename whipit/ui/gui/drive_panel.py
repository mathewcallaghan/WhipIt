"""Standalone UI panel encapsulating the lifecycle, session, and controls for an optical drive."""

from __future__ import annotations

import copy
import html
import re
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from whipit import __version__
from whipit.audio.metadata import _MBID_RE
from whipit.bootstrap import get_missing_dependencies
from whipit.config import (
    CDS_AUDIO,
    CDS_DATA_1,
    CDS_DATA_2,
    CDS_DISC_OK,
    CDS_DRIVE_NOT_READY,
    CDS_MIXED,
    CDS_TRAY_OPEN,
    CDS_XA_2_1,
    CDS_XA_2_2,
    UserPreferences,
    get_drive_hardware_id,
    get_drive_max_speed,
    get_stored_drive_offset,
    get_whipper_defeats_cache,
    is_usb_drive,
    load_preferences,
    parse_duration_seconds,
    store_whipper_defeats_cache,
)
from whipit.metadata.protection import is_copy_protected_disc
from whipit.models import JobManifest, ReleaseOption, TrackInfo, WorkflowState
from whipit.net import is_strict_https_url
from whipit.ripping.identify import DiscInspectionResult
from whipit.ripping.whipper import (
    AccurateRipSummaryEvent,
    AlbumHeaderEvent,
    ChecksumMatchEvent,
    DiscTOCEvent,
    DriveStallEvent,
    PreExtractionEvent,
    ReleasePickedEvent,
    SectorFixupEvent,
    TrackProgressEvent,
    WhipperEvent,
    WhipperStreamParser,
)
from whipit.ui.base import (
    LogLevel,
)
from whipit.ui.gui.adapter import GuiInteractionAdapter
from whipit.ui.gui.dialogs import (
    EditMetadataDialog,
    prompt_calibration_retry_dialog,
    prompt_calibration_start_dialog,
    prompt_conflict_resolution_dialog,
    prompt_edit_metadata_dialog,
    prompt_empty_multi_drives_dialog,
    prompt_extraction_error_dialog,
    prompt_extraction_speed_dialog,
    prompt_insert_disc_dialog,
    prompt_select_loaded_drive_dialog,
    prompt_wait_for_reload_dialog,
    prompt_yes_no_dialog,
    show_message_dialog,
)
from whipit.ui.gui.session import ActiveSession
from whipit.ui.gui.styles import (
    LED_PILL_AMBER,
    LED_PILL_CYAN,
    LED_PILL_GREEN,
    LED_PILL_MUTED,
    LED_PILL_UNCATALOGED,
    LINK_COLOR_COPY,
    LINK_COLOR_DISCOGS,
    LINK_COLOR_MB,
    LINK_COLOR_SUBMIT,
    STATUS_BADGE_AUDIO,
    STATUS_BADGE_DATA,
    STATUS_BADGE_DISC,
    STATUS_BADGE_EMPTY,
    STATUS_BADGE_NO_DRIVE,
    STYLE_COMPACT_BTN,
    STYLE_EXTERNAL_LINKS,
    STYLE_HEADER_BTN,
    STYLE_HEADER_CYAN,
    STYLE_LABEL_MUTED_BOLD,
    STYLE_SUBTITLE_MUTED,
    VERIFICATION_BADGE_ACCURATE,
    VERIFICATION_BADGE_ERROR,
    VERIFICATION_BADGE_UNVERIFIED,
)
from whipit.ui.gui.widgets import SectorHealthHeatmap, TrackTableWidget
from whipit.ui.gui.worker import (
    CloseWorker,
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


class DriveState(str, Enum):
    EMPTY = "EMPTY"
    READY = "READY"
    IDENTIFYING = "IDENTIFYING"
    RIPPING = "RIPPING"
    COMMITTED = "COMMITTED"
    ERROR = "ERROR"


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
    disc_loaded = pyqtSignal(str)
    device_switch_requested = pyqtSignal(str, str)

    def __init__(
        self,
        device: Optional[str] = None,
        prefs: Optional[UserPreferences] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("drivePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.device_path: Optional[str] = device
        self.prefs: UserPreferences = prefs or load_preferences()
        self.adapter = GuiInteractionAdapter()
        self.session = ActiveSession()
        self.drive_state: DriveState = DriveState.EMPTY

        self.worker: Optional[RipWorker] = None
        self.calibrate_worker: Optional[Any] = None
        self.identify_worker: Optional[IdentifyWorker] = None
        self.details_worker: Optional[ReleaseDetailsWorker] = None
        self.cover_worker: Optional[CoverArtWorker] = None
        self.poll_worker: Optional[DrivePollWorker] = None
        self.eject_worker: Optional[EjectWorker] = None
        self.close_worker: Optional[CloseWorker] = None
        self.is_tray_open: bool = False
        self.is_extracting_audio: bool = False

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
        self._safe_speed_override: Optional[int] = None
        self._copy_protected_from_meta: bool = False
        self._rip_start_time: float = 0.0
        self._speed_sample_time: float = 0.0
        self._speed_sample_percent: float = 0.0
        self._speed_sample_track: int = 0
        self._speed_sample_try: int = 1
        self._speed_sample_phase: str = ""
        self._current_speed_x: Optional[float] = None
        self._hud_retry_keys: set[Tuple[int, int]] = set()
        self._hud_stall_count: int = 0
        self._hud_in_stall_episode: bool = False
        self._hud_stall_track: Optional[int] = None
        self._last_disc_status: Tuple[int, int] = (0, 0)
        self._release_cache: Dict[Tuple[str, Optional[int]], Tuple[Optional[str], List[TrackInfo]]] = {}

        self._setup_ui()
        self._connect_signals()
        self._update_offset_display()
        self._update_hardware_pills()
        self._update_media_spec_strip()
        if device:
            self.set_device(device)

    def _on_calibrate_clicked(self) -> None:
        dev = self.device_path or "/dev/cdrom"
        if not dev:
            return
        if (
            (self.worker and self.worker.isRunning())
            or (self.identify_worker and self.identify_worker.isRunning())
            or (self.calibrate_worker and self.calibrate_worker.isRunning())
        ):
            QMessageBox.warning(
                self,
                "Drive Busy",
                "Cannot calibrate drive while extraction, identification, or calibration is active.",
            )
            return

        self._set_controls_running(True)
        self.calibrate_btn.setEnabled(False)
        self.phase_detail_lbl.setText(f"Starting drive calibration on {dev}...")
        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat("Calibrating: Searching AccurateRip database...")
        from whipit.ui.gui.worker import CalibrationWorker
        WorkerClass = _get_override("CalibrationWorker", CalibrationWorker)
        self.calibrate_worker = WorkerClass(dev, self.adapter, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.calibrate_worker)
        self.calibrate_worker.calibration_finished.connect(self._on_calibration_finished)
        self.calibrate_worker.finished.connect(self._clear_calibrate_worker)
        self.calibrate_worker.start()

    def _clear_calibrate_worker(self) -> None:
        self.calibrate_worker = None
        self._set_controls_running(False)
        self.calibrate_btn.setEnabled(True)
        self.start_btn.setEnabled(self.is_ready_to_rip)

    def _on_calibration_finished(self, success: bool, offset: Optional[int], msg: str) -> None:
        self._set_controls_running(False)
        self.calibrate_btn.setEnabled(True)
        self.start_btn.setEnabled(self.is_ready_to_rip)
        self.rip_progress_bar.setRange(0, 100)
        self.rip_progress_bar.setValue(0)
        self.rip_progress_bar.setFormat("Idle")
        self._update_offset_display()
        self._update_hardware_pills()
        if success and offset is not None:
            self.phase_detail_lbl.setText(f"✓ Drive calibration successful: Offset {offset:+d} saved.")
            self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Calibration successful: offset {offset:+d} stored.")
        elif success:
            self.phase_detail_lbl.setText("✓ Drive calibration completed.")
        else:
            self.phase_detail_lbl.setText(f"Drive calibration stopped: {msg}")

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
            and not (self.calibrate_worker and self.calibrate_worker.isRunning())
        )

    def transition_to(self, state: DriveState) -> None:
        self.drive_state = state

    def _format_eject_btn_text(self) -> str:
        dev_short = Path(self.device_path).name if self.device_path else ""
        if self.is_tray_open:
            return f"📥 Close Tray ({dev_short})" if dev_short else "📥 Close Tray"
        return f"⏏ Eject ({dev_short})" if dev_short else "⏏ Eject Drive"

    def set_device(self, dev: Optional[str]) -> None:
        self.device_path = dev
        self.session.device = dev
        self._last_disc_status = (0, 0)
        dev_short = Path(dev).name if dev else ""
        if dev:
            hw = get_drive_hardware_id(dev)
            self.drive_info_lbl.setText(f"Drive: <b>{dev}</b> ({hw})")
            self.session.hardware_id = hw
            self.start_btn.setText(f"Start Rip ({dev_short})")
            self.start_btn.setToolTip(f"Start archival extraction for the disc in {dev} only")
            self.start_btn.setEnabled(self.is_ready_to_rip)
            self.eject_btn.setText(self._format_eject_btn_text())
            self.cancel_btn.setText(f"Cancel ({dev_short})")
        else:
            self.is_tray_open = False
            self.drive_state = DriveState.EMPTY
            self.drive_info_lbl.setText("No optical drive assigned")
            self.disc_status_badge.setText("No Drive")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_NO_DRIVE)
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Start Rip")
            self.eject_btn.setText(self._format_eject_btn_text())
            self.cancel_btn.setText("Cancel")
        self._update_offset_display()
        self._update_hardware_pills()
        self._update_media_spec_strip()

    def _update_offset_display(self) -> None:
        dev = self.device_path or "/dev/cdrom"
        offset = get_stored_drive_offset(dev) if dev else None
        if offset is not None:
            self.offset_badge.setText(f"● Offset: {offset:+d}")
            self.offset_badge.setStyleSheet(LED_PILL_GREEN)
            self.calibrate_btn.setText("Recalibrate")
        else:
            self.offset_badge.setText("○ Offset: Not Configured")
            self.offset_badge.setStyleSheet(LED_PILL_MUTED)
            self.calibrate_btn.setText("Calibrate Drive")

    def _update_hardware_pills(self) -> None:
        dev = self.device_path
        if not dev:
            self.interface_badge.setText("○ Bus: --")
            self.interface_badge.setStyleSheet(LED_PILL_MUTED)
            self.speed_badge.setText("○ Max: --")
            self.speed_badge.setStyleSheet(LED_PILL_MUTED)
            self.cache_badge.setText("○ Cache: --")
            self.cache_badge.setStyleSheet(LED_PILL_MUTED)
            return

        usb = is_usb_drive(dev)
        if usb:
            self.interface_badge.setText("● USB")
            self.interface_badge.setStyleSheet(LED_PILL_AMBER)
            self.interface_badge.setToolTip("External USB optical drive (cache-defeat safely managed)")
        else:
            self.interface_badge.setText("● SATA")
            self.interface_badge.setStyleSheet(LED_PILL_CYAN)
            self.interface_badge.setToolTip("Internal SATA / ATAPI optical drive")

        max_spd = get_drive_max_speed(dev)
        if max_spd:
            self.speed_badge.setText(f"● Max {max_spd}x")
            self.speed_badge.setStyleSheet(LED_PILL_CYAN)
            self.speed_badge.setToolTip(f"Hardware-rated maximum CD read speed: {max_spd}x")
        else:
            self.speed_badge.setText("○ Max --")
            self.speed_badge.setStyleSheet(LED_PILL_MUTED)
            self.speed_badge.setToolTip("Maximum read speed not reported by kernel")

        defeats = get_whipper_defeats_cache(dev)
        if defeats is True:
            self.cache_badge.setText("● Cache: Defeated")
            self.cache_badge.setStyleSheet(LED_PILL_AMBER if usb else LED_PILL_GREEN)
            self.cache_badge.setToolTip("whipper.conf defeats_cache = True")
        elif defeats is False:
            self.cache_badge.setText("● Cache: Bypass")
            self.cache_badge.setStyleSheet(LED_PILL_GREEN if usb else LED_PILL_CYAN)
            self.cache_badge.setToolTip("whipper.conf defeats_cache = False (recommended for USB stability)")
        else:
            self.cache_badge.setText("○ Cache: Auto")
            self.cache_badge.setStyleSheet(LED_PILL_MUTED)
            self.cache_badge.setToolTip("defeats_cache not yet configured in whipper.conf")

    def _update_media_spec_strip(self) -> None:
        opus_rate = getattr(self.prefs, "opus_bitrate", 192)
        out_fmt = f"FLAC + Opus {opus_rate}k" if self.prefs.generate_opus else "FLAC Lossless"
        if not self.device_path:
            self.media_spec_lbl.setText(f"○ No Drive Assigned · 16-bit / 44.1 kHz · {out_fmt}")
            self.media_spec_lbl.setStyleSheet(LED_PILL_MUTED)
            return

        drive_st, disc_st = self._last_disc_status
        if drive_st == CDS_DISC_OK and disc_st == CDS_MIXED:
            self.media_spec_lbl.setText(f"● Enhanced CD (Mixed Mode) · 16-bit / 44.1 kHz · {out_fmt}")
            self.media_spec_lbl.setStyleSheet(LED_PILL_CYAN)
        elif (drive_st == CDS_DISC_OK and disc_st == CDS_AUDIO) or self.session.is_disc_loaded:
            self.media_spec_lbl.setText(f"● Red Book CDDA · 16-bit / 44.1 kHz · {out_fmt}")
            self.media_spec_lbl.setStyleSheet(LED_PILL_GREEN)
        elif drive_st == CDS_DISC_OK and disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2):
            self.media_spec_lbl.setText(f"⚠ Data / Non-Audio Disc · Unsupported · {out_fmt}")
            self.media_spec_lbl.setStyleSheet(LED_PILL_AMBER)
        else:
            self.media_spec_lbl.setText(f"○ Standby · 16-bit / 44.1 kHz · {out_fmt}")
            self.media_spec_lbl.setStyleSheet(LED_PILL_MUTED)

    def handle_drive_status(self, drive_st: int, disc_st: int) -> None:
        from whipit.ui.gui.main_window import _is_worker_running

        if not self.device_path or _is_worker_running(self.worker):
            return
        if _is_worker_running(self.eject_worker) or _is_worker_running(self.close_worker):
            return
        if self.identify_worker and self.identify_worker.isRunning():
            if drive_st in (0, CDS_DRIVE_NOT_READY) or (drive_st == CDS_DISC_OK and disc_st == 0):
                return

        prev_drive_st = self._last_disc_status[0]
        self._last_disc_status = (drive_st, disc_st)
        has_audio = (drive_st == CDS_DISC_OK) and (disc_st in (CDS_AUDIO, CDS_MIXED))
        is_data_disc = (drive_st == CDS_DISC_OK) and (
            disc_st in (CDS_DATA_1, CDS_DATA_2, CDS_XA_2_1, CDS_XA_2_2)
        )

        if has_audio:
            self.is_tray_open = False
            self.eject_btn.setText(self._format_eject_btn_text())
            self.session.is_disc_loaded = True
            self.drive_state = DriveState.READY
            is_new = self._disc_was_ejected or not self._disc_identified
            if is_new:
                self._disc_was_ejected = False
                self._disc_identified = True
                self._reset_disc_state(clear_user_art=True)
                self._reset_pipeline_badges()
                self.disc_loaded.emit(self.device_path or "")
            disc_name = "Enhanced CD" if disc_st == CDS_MIXED else "Audio CD"
            self.disc_status_badge.setText(f"{disc_name} Loaded")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
            if disc_st == CDS_MIXED:
                if self.protected_badge.isHidden():
                    self.protected_badge.setVisible(True)
                    self.apply_safe_extraction_preset()
            elif not self._copy_protected_from_meta:
                self._safe_speed_override = None
                self.protected_badge.setVisible(False)
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
            self.is_tray_open = False
            self.eject_btn.setText(self._format_eject_btn_text())
            self.start_btn.setEnabled(False)
            self.drive_state = DriveState.EMPTY
            self.session.is_disc_loaded = False
            self._disc_was_ejected = True
            self._disc_identified = False
            self.disc_status_badge.setText("Data / DVD Disc")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DATA)
            self.status_changed.emit(self.device_path, "Data Disc")
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()
        elif drive_st in (CDS_DISC_OK, CDS_DRIVE_NOT_READY) and self._disc_was_ejected:
            self.is_tray_open = False
            self.eject_btn.setText(self._format_eject_btn_text())
            self.start_btn.setEnabled(False)
            self.drive_state = DriveState.READY
            self.session.is_disc_loaded = False
            badge_txt = "Spinning up..." if drive_st == CDS_DRIVE_NOT_READY else "Disc Loaded"
            self.disc_status_badge.setText(badge_txt)
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_DISC)
            self.status_changed.emit(self.device_path or "", badge_txt)
            self.disc_loaded.emit(self.device_path or "")
        else:
            if drive_st == CDS_TRAY_OPEN:
                self.is_tray_open = True
                self.disc_status_badge.setText("Tray Open")
            else:
                if prev_drive_st == CDS_TRAY_OPEN:
                    self.is_tray_open = False
                self.disc_status_badge.setText("Tray Empty")
            self.eject_btn.setText(self._format_eject_btn_text())
            self.start_btn.setEnabled(False)
            self.drive_state = DriveState.EMPTY
            self.session.is_disc_loaded = False
            self._disc_was_ejected = True
            self._disc_identified = False
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_EMPTY)
            self.status_changed.emit(self.device_path, "Tray Open" if self.is_tray_open else "Empty")
            if self._current_step_key != "COMMITTED":
                self._reset_pipeline_badges()
        self._update_media_spec_strip()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Drive Info Header & Hardware LED Pills
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self.drive_info_lbl = QLabel("Drive: Initializing...")
        self.drive_info_lbl.setTextFormat(Qt.TextFormat.RichText)
        header_row.addWidget(self.drive_info_lbl, stretch=1)

        self.disc_status_badge = QLabel("Checking...")
        self.disc_status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.disc_status_badge.setFixedHeight(28)
        self.disc_status_badge.setMinimumWidth(130)
        self.disc_status_badge.setObjectName("statusBadge")
        header_row.addWidget(self.disc_status_badge)
        layout.addLayout(header_row)

        hw_pills_row = QHBoxLayout()
        hw_pills_row.setSpacing(6)

        pill_font = QFont("Monospace")
        pill_font.setStyleHint(QFont.StyleHint.TypeWriter)
        pill_font.setPointSize(8)

        self.interface_badge = QLabel("○ Bus: --")
        self.interface_badge.setFont(pill_font)
        self.interface_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.interface_badge.setFixedHeight(22)
        self.interface_badge.setObjectName("interfaceBadge")
        hw_pills_row.addWidget(self.interface_badge)

        self.speed_badge = QLabel("○ Max: --")
        self.speed_badge.setFont(pill_font)
        self.speed_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.speed_badge.setFixedHeight(22)
        self.speed_badge.setObjectName("speedBadge")
        hw_pills_row.addWidget(self.speed_badge)

        self.cache_badge = QLabel("○ Cache: --")
        self.cache_badge.setFont(pill_font)
        self.cache_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cache_badge.setFixedHeight(22)
        self.cache_badge.setObjectName("cacheBadge")
        hw_pills_row.addWidget(self.cache_badge)

        self.offset_badge = QLabel("○ Offset: Not Configured")
        self.offset_badge.setFont(pill_font)
        self.offset_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.offset_badge.setFixedHeight(22)
        self.offset_badge.setObjectName("offsetBadge")
        hw_pills_row.addWidget(self.offset_badge)

        self.uncataloged_badge = QLabel("ℹ Uncataloged Disc")
        self.uncataloged_badge.setObjectName("uncatalogedBadge")
        self.uncataloged_badge.setFont(pill_font)
        self.uncataloged_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.uncataloged_badge.setFixedHeight(22)
        self.uncataloged_badge.setStyleSheet(LED_PILL_UNCATALOGED)
        self.uncataloged_badge.setToolTip(
            "This disc TOC was not found in MusicBrainz.\n"
            "Ripping will proceed with generic or custom metadata tags."
        )
        self.uncataloged_badge.setVisible(False)
        hw_pills_row.addWidget(self.uncataloged_badge)

        self.protected_badge = QLabel("🛡️ Copy-Protected CD")
        self.protected_badge.setObjectName("protectedBadge")
        self.protected_badge.setFont(pill_font)
        self.protected_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.protected_badge.setFixedHeight(22)
        self.protected_badge.setStyleSheet(LED_PILL_UNCATALOGED)
        self.protected_badge.setToolTip(
            "Non-standard Q-subchannel errors or Copy Control metadata detected.\n"
            "Safe Extraction Mode (4x Speed + Cache Bypass) is recommended."
        )
        self.protected_badge.setVisible(False)
        hw_pills_row.addWidget(self.protected_badge)

        hw_pills_row.addStretch(1)

        self.calibrate_btn = QPushButton("Calibrate Drive")
        self.calibrate_btn.setFixedHeight(24)
        self.calibrate_btn.setStyleSheet(STYLE_HEADER_BTN)
        self.calibrate_btn.clicked.connect(self._on_calibrate_clicked)
        hw_pills_row.addWidget(self.calibrate_btn)
        layout.addLayout(hw_pills_row)

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

        # Extraction Options (optional overrides; automatic prompts handle defaults)
        chk_row = QHBoxLayout()
        chk_row.setSpacing(12)
        opts_lbl = QLabel("Optional Overrides:")
        opts_lbl.setStyleSheet(STYLE_LABEL_MUTED_BOLD)
        opts_lbl.setToolTip(
            "These options are optional — WhipIt automatically prompts you if a disc is "
            "scratched, uncataloged, or already exists in your library."
        )
        chk_row.addWidget(opts_lbl)

        self.keep_going_chk = QCheckBox("Skip unreadable sectors")
        self.keep_going_chk.setToolTip(
            "Only needed for visibly scratched discs.\n"
            "Continues extracting remaining tracks if a sector cannot be read cleanly.\n"
            "By default, WhipIt pauses on errors so you can retry slower, clean the disc, or switch drives."
        )
        self.unknown_chk = QCheckBox("Allow uncataloged disc")
        self.unknown_chk.setToolTip(
            "Rips discs not found in MusicBrainz using generic or custom-edited tags.\n"
            "Automatically enabled when an uncataloged CD is detected or when you edit metadata."
        )
        self.overwrite_chk = QCheckBox("Replace existing archive")
        self.overwrite_chk.setToolTip(
            "Replaces an existing destination folder for this album without prompting\n"
            "(a backup is preserved during publication).\n"
            "By default, WhipIt asks whether to save alongside or replace."
        )
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
        art_container.setFixedSize(140, 140)

        art_stack_layout = QVBoxLayout(art_container)
        art_stack_layout.setContentsMargins(0, 0, 0, 0)
        art_stack_layout.setSpacing(0)

        self.cover_art_label = QLabel(art_container)
        self.cover_art_label.setFixedSize(140, 140)
        self.cover_art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_art_label.setObjectName("coverArtLabel")
        self.cover_art_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cover_art_label.setToolTip("Click to select custom artwork, or right-click for options")
        self.cover_art_label.mousePressEvent = self._on_cover_art_mouse_press
        self._render_cover_art_placeholder()

        overlay_bar = QHBoxLayout()
        overlay_bar.setContentsMargins(6, 6, 6, 6)
        overlay_bar.setSpacing(4)
        overlay_bar.addStretch(1)

        self.change_art_btn = QPushButton("📷 Change", art_container)
        self.change_art_btn.setObjectName("coverOverlayBtn")
        self.change_art_btn.setFixedHeight(24)
        self.change_art_btn.setToolTip("Select custom cover art file")
        self.change_art_btn.clicked.connect(self._choose_custom_art)
        self.change_art_btn.raise_()
        overlay_bar.addWidget(self.change_art_btn)

        self.reset_art_btn = QPushButton("✕", art_container)
        self.reset_art_btn.setObjectName("coverOverlayBtn")
        self.reset_art_btn.setFixedSize(24, 24)
        self.reset_art_btn.setToolTip("Reset to original Cover Art Archive image")
        self.reset_art_btn.setVisible(False)
        self.reset_art_btn.clicked.connect(self._reset_cover_art)
        self.reset_art_btn.raise_()
        overlay_bar.addWidget(self.reset_art_btn)

        art_stack_layout.addStretch(1)
        art_stack_layout.addLayout(overlay_bar)
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
        rel_lbl.setStyleSheet(STYLE_LABEL_MUTED_BOLD)
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
        self.tracklist_header_lbl.setStyleSheet(STYLE_HEADER_CYAN)
        table_header.addWidget(self.tracklist_header_lbl)
        table_header.addStretch()

        self.edit_meta_btn = QPushButton("✏️ Edit Info...")
        self.edit_meta_btn.setFixedHeight(26)
        self.edit_meta_btn.setStyleSheet(STYLE_COMPACT_BTN)
        self.edit_meta_btn.clicked.connect(self._open_edit_metadata_dialog)
        table_header.addWidget(self.edit_meta_btn)
        right_layout.addLayout(table_header)

        self.track_table = TrackTableWidget(self)
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
        self.external_links_lbl.setStyleSheet(STYLE_EXTERNAL_LINKS)
        self.external_links_lbl.setVisible(False)
        album_layout.addWidget(self.external_links_lbl)

        self.media_spec_lbl = QLabel("○ Standby · 16-bit / 44.1 kHz · FLAC + Opus 192k")
        spec_font = QFont("Monospace")
        spec_font.setStyleHint(QFont.StyleHint.TypeWriter)
        spec_font.setPointSize(8)
        self.media_spec_lbl.setFont(spec_font)
        self.media_spec_lbl.setFixedHeight(22)
        self.media_spec_lbl.setObjectName("mediaSpecBadge")
        self.media_spec_lbl.setStyleSheet(LED_PILL_MUTED)
        album_layout.addWidget(self.media_spec_lbl)

        form_prog = QFormLayout()
        form_prog.setContentsMargins(0, 4, 0, 0)
        form_prog.setSpacing(6)

        ext_container = QWidget()
        ext_layout = QVBoxLayout(ext_container)
        ext_layout.setContentsMargins(0, 0, 0, 0)
        ext_layout.setSpacing(4)

        self.hud_stats_lbl = QLabel("Speed: --  |  Throughput: --  |  Retries: 0  |  Stalls: 0")
        hud_font = QFont("Monospace")
        hud_font.setStyleHint(QFont.StyleHint.TypeWriter)
        hud_font.setPointSize(8)
        self.hud_stats_lbl.setFont(hud_font)
        self.hud_stats_lbl.setStyleSheet(STYLE_SUBTITLE_MUTED)
        ext_layout.addWidget(self.hud_stats_lbl)

        self.sector_heatmap = SectorHealthHeatmap(self)
        ext_layout.addWidget(self.sector_heatmap)

        self.rip_progress_bar = QProgressBar()
        self.rip_progress_bar.setRange(0, 100)
        self.rip_progress_bar.setValue(0)
        self.rip_progress_bar.setFormat("Idle")
        ext_layout.addWidget(self.rip_progress_bar)
        form_prog.addRow("CD Extraction:", ext_container)

        self.opus_progress_bar = QProgressBar()
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        # Action Controls Row (Anchored prominently inside Album layout)
        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 2, 0, 4)
        controls_row.setSpacing(8)

        self.identify_btn = QPushButton("Identify Disc")
        self.identify_btn.setMinimumHeight(34)
        self.identify_btn.clicked.connect(lambda: self.identify_disc(silent=False))
        controls_row.addWidget(self.identify_btn, stretch=1)

        self.start_btn = QPushButton("Start Rip")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setMinimumHeight(34)
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_rip)
        controls_row.addWidget(self.start_btn, stretch=2)

        self.eject_btn = QPushButton("⏏ Eject Drive")
        self.eject_btn.setMinimumHeight(34)
        self.eject_btn.clicked.connect(self.toggle_eject_close)
        controls_row.addWidget(self.eject_btn, stretch=1)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("dangerButton")
        self.cancel_btn.setMinimumHeight(34)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_rip)
        controls_row.addWidget(self.cancel_btn, stretch=1)

        album_layout.addLayout(controls_row)

        self.opus_progress_bar.setFormat("Waiting...")
        opus_rate = getattr(self.prefs, "opus_bitrate", 192)
        self.opus_progress_label_text = f"Opus {opus_rate}k Mirror:"
        self.opus_progress_lbl = QLabel(self.opus_progress_label_text)
        form_prog.addRow(self.opus_progress_lbl, self.opus_progress_bar)
        album_layout.addLayout(form_prog)

        layout.addWidget(album_group)

    def _connect_signals(self) -> None:
        sig = self.adapter.signaler
        sig.log_emitted.connect(self._on_log)
        sig.state_changed.connect(self._on_state_changed)
        sig.whipper_output.connect(self._on_whipper_output)
        sig.whipper_event.connect(self._on_whipper_event)
        sig.transcode_progress.connect(self._on_transcode_progress)
        sig.disc_spinup_poll.connect(self._on_disc_spinup_poll)
        sig.active_device_changed.connect(self._on_adapter_device_changed)
        sig.prompt_requested.connect(self._on_prompt_requested)

    def _on_adapter_device_changed(self, old_dev: str, new_dev: str) -> None:
        self.device_switch_requested.emit(old_dev, new_dev)

    def _disconnect_adapter_signals(self) -> None:
        sig = self.adapter.signaler
        for signal_obj, slot in (
            (sig.log_emitted, self._on_log),
            (sig.state_changed, self._on_state_changed),
            (sig.whipper_output, self._on_whipper_output),
            (sig.whipper_event, self._on_whipper_event),
            (sig.transcode_progress, self._on_transcode_progress),
            (sig.disc_spinup_poll, self._on_disc_spinup_poll),
            (sig.active_device_changed, self._on_adapter_device_changed),
            (sig.prompt_requested, self._on_prompt_requested),
        ):
            try:
                signal_obj.disconnect(slot)
            except Exception:
                pass

    def _cancel_aux_workers(self) -> None:
        if self.identify_worker:
            for sig_obj, slot in (
                (getattr(self.identify_worker, "line_received", None), self.log_emitted.emit),
                (getattr(self.identify_worker, "finished", None), self._clear_identify_worker),
            ):
                if sig_obj is not None:
                    try:
                        sig_obj.disconnect(slot)
                    except Exception:
                        pass
            try:
                if hasattr(self.identify_worker, "inspection_completed"):
                    self.identify_worker.inspection_completed.disconnect()
            except Exception:
                pass
            try:
                if hasattr(self.identify_worker, "cancel"):
                    self.identify_worker.cancel()
                else:
                    self.identify_worker.terminate()
            except Exception:
                pass
            self.identify_worker = None

        if self.details_worker:
            try:
                self.details_worker.details_found.disconnect(self._on_details_resolved)
                self.details_worker.finished.disconnect(self._clear_details_worker)
            except Exception:
                pass
            try:
                self.details_worker.cancel()
            except Exception:
                pass
            self.details_worker = None

        if self.cover_worker:
            for sig_obj, slot in (
                (getattr(self.cover_worker, "cover_found", None), self._on_cover_art_found),
                (getattr(self.cover_worker, "cover_failed", None), self._on_cover_art_failed),
                (getattr(self.cover_worker, "finished", None), self._clear_cover_worker),
            ):
                if sig_obj is not None:
                    try:
                        sig_obj.disconnect(slot)
                    except Exception:
                        pass
            try:
                self.cover_worker.cancel()
            except Exception:
                pass
            self.cover_worker = None

    def release_rip_on_switch(self) -> None:
        self._disconnect_adapter_signals()
        if self.worker is not None:
            try:
                self.worker.workflow_finished.disconnect(self._on_workflow_finished)
            except Exception:
                pass
            try:
                self.worker.finished.disconnect(self._clear_rip_worker)
            except Exception:
                pass
            self.worker = None

        self.adapter = GuiInteractionAdapter()
        self._connect_signals()

        self.is_extracting_audio = False
        self.transition_to(DriveState.EMPTY)
        self.session.is_disc_loaded = False
        self._last_disc_status = (0, 0)
        self._disc_was_ejected = True
        self._disc_identified = False
        self._art_chosen_since_commit = False
        self._current_speed_x = None
        self._speed_sample_time = 0.0
        self._speed_sample_try = 1
        self._speed_sample_phase = ""
        self._whipper_parser = WhipperStreamParser()
        self._reset_disc_state(clear_user_art=True)
        self._reset_telemetry_hud()
        self._reset_pipeline_badges()
        self._set_controls_running(False)
        self.start_btn.setEnabled(False)
        self.rip_progress_bar.setRange(0, 100)
        self.rip_progress_bar.setValue(0)
        self.rip_progress_bar.setFormat("Idle")
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat("Waiting..." if self.prefs.generate_opus else "Disabled in Preferences")
        self.phase_detail_lbl.setText("Insert an Audio CD and click Identify Disc or Start Rip.")
        self.disc_status_badge.setText("Tray Empty")
        self.disc_status_badge.setStyleSheet(STATUS_BADGE_EMPTY)
        self._update_media_spec_strip()
        self.status_changed.emit(self.device_path or "", "Empty")

    def adopt_rip_on_switch(
        self,
        worker: Any,
        adapter: GuiInteractionAdapter,
        source_session: ActiveSession,
        last_inspection: Optional[DiscInspectionResult],
        rip_start_time: float,
    ) -> None:
        self._cancel_aux_workers()
        self._disconnect_adapter_signals()
        self.adapter = adapter
        self._connect_signals()

        self.session = copy.deepcopy(source_session)
        self.session.device = self.device_path
        self.session.hardware_id = get_drive_hardware_id(self.device_path) if self.device_path else None
        self.session.is_disc_loaded = False

        self._last_inspection = copy.deepcopy(last_inspection)
        self._ripping_started = False
        self.is_extracting_audio = True
        self._rip_start_time = rip_start_time
        self._speed_sample_time = 0.0
        self._speed_sample_percent = 0.0
        self._speed_sample_track = 0
        self._speed_sample_try = 1
        self._speed_sample_phase = ""
        self._current_speed_x = None
        self._whipper_parser = WhipperStreamParser()
        self._disc_was_ejected = False
        self._disc_identified = True
        self.keep_going_chk.setChecked(False)

        self.album_title_lbl.setText(self.session.display_title)
        self._populate_track_table(self.session.tracks)
        self._update_links_display()
        self.verification_lbl.setText("")
        self.verification_lbl.setVisible(False)
        if self.session.art_path and self.session.art_path.exists():
            self._render_cover_art(self.session.art_path)
            self.reset_art_btn.setVisible(
                self.session.art_is_user_selected and bool(self.session.downloaded_art_path)
            )
        else:
            self._render_cover_art_placeholder()
            self.reset_art_btn.setVisible(False)

        if not self.session.mbid and self.session.artist != "Unknown Artist":
            self.uncataloged_badge.setText("✏ Custom Metadata")
            self.uncataloged_badge.setStyleSheet(LED_PILL_CYAN)
            self.uncataloged_badge.setVisible(True)
        else:
            self.uncataloged_badge.setVisible(False)

        self._reset_telemetry_hud()
        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat(f"Waiting for disc in {Path(self.device_path or '').name}...")
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat(
            "Waiting for FLAC extraction..." if self.prefs.generate_opus else "Disabled in Preferences"
        )
        self._set_pipeline_step("RIPPING")
        self.disc_status_badge.setText("Waiting for Disc...")
        self.disc_status_badge.setStyleSheet(STATUS_BADGE_DISC)
        self.phase_detail_lbl.setText(f"Switching extraction to {self.device_path}...")

        self.worker = worker
        if self.worker is not None:
            self.worker.workflow_finished.connect(self._on_workflow_finished)
            self.worker.finished.connect(self._clear_rip_worker)
        self._update_hardware_pills()
        self._update_media_spec_strip()
        self.transition_to(DriveState.RIPPING)
        self._set_controls_running(True)
        self.status_changed.emit(self.device_path or "", "Ripping")

    def apply_preferences(self, prefs: UserPreferences) -> None:
        self.prefs = prefs
        opus_rate = getattr(prefs, "opus_bitrate", 192)
        self.opus_progress_label_text = f"Opus {opus_rate}k Mirror:"
        self.opus_progress_lbl.setText(self.opus_progress_label_text)
        if not prefs.generate_opus:
            self.opus_progress_bar.setFormat("Disabled in Preferences")
        else:
            self.opus_progress_bar.setFormat("Waiting...")
        self._update_media_spec_strip()

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
            196,
            196,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.cover_art_label.setPixmap(scaled)
        self.cover_art_label.setToolTip(f"Cover Art: {art_path.name}\n(Click to replace)")

    def inherit_cover_art(self, art_path: Path, from_desc: str = "Disc 1") -> None:
        if not art_path or not art_path.exists() or self.session.art_is_user_selected:
            return
        self.session.set_downloaded_art(art_path)
        self._render_cover_art(art_path)
        self.adapter.set_cover_art(art_path)
        self.reset_art_btn.setVisible(False)
        self.phase_detail_lbl.setText(f"✓ Inherited cover art from {from_desc}.")
        self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Inherited cover art from {from_desc} ({art_path.name})")

    def _find_existing_album_art(self) -> Optional[Path]:
        if not self.session.artist or self.session.artist == "Unknown Artist":
            return None
        if not self.session.title or self.session.title == "Unknown Album":
            return None

        from whipit.audio.metadata import find_cover_art
        from whipit.naming import format_album_stem

        flac_root = self.prefs.flac_root
        stem = format_album_stem(self.session.artist, self.session.title)
        candidates = [
            flac_root / stem,
            flac_root / f"{stem} (FLAC)",
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                art = find_cover_art(c)
                if art:
                    return art
        return None

    def _clear_cover_worker(self) -> None:
        self.cover_worker = None

    def _fetch_cover_art_impl(self, mbid: str, alternate_mbids: Optional[List[str]] = None) -> None:
        if self.session.art_is_user_selected or not mbid:
            return
        from whipit.ui.gui.main_window import _is_worker_running
        if _is_worker_running(self.cover_worker):
            if getattr(self.cover_worker, "mbid", None) == mbid:
                return
            try:
                self.cover_worker.cancel()
            except Exception:
                pass
            self.cover_worker = None

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
        self.cover_worker.finished.connect(self._clear_cover_worker)
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
            win = self.window()
            if win and hasattr(win, "_sync_multi_disc_cover_art"):
                win._sync_multi_disc_cover_art()

    def _on_cover_art_failed(self, mbid: str) -> None:
        if self.session.mbid and mbid != self.session.mbid:
            return
        if not self.session.art_is_user_selected:
            self.session.clear_art()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)
            self._render_cover_art_placeholder("No Cover Art\nin Archive\n\nClick to add")
            self.log_emitted.emit(f"[{self.device_path or 'Drive'}] No cover art found in Cover Art Archive for release {mbid}")

    def _on_cover_art_mouse_press(self, event: QMouseEvent) -> None:
        """Handles left-click browsing and right-click context menu for cover art."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._choose_custom_art()
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            act_change = menu.addAction("📁 Choose Custom Cover Art...")
            act_paste = menu.addAction("📋 Paste Image from Clipboard")
            act_reset = menu.addAction("↺ Reset to Original Cover Art")
            act_reset.setEnabled(bool(self.session.art_is_user_selected))

            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen == act_change:
                self._choose_custom_art()
            elif chosen == act_paste:
                self._paste_cover_art_from_clipboard()
            elif chosen == act_reset:
                self._reset_cover_art()

    def _apply_user_cover_art(self, target_path: Path, display_name: str) -> None:
        self.session.set_user_art(target_path)
        self._art_chosen_since_commit = True
        self._render_cover_art(target_path)
        self.adapter.set_cover_art(target_path)
        self.reset_art_btn.setVisible(bool(self.session.downloaded_art_path))
        self.log_emitted.emit(f"[{self.device_path or 'Drive'}] Cover art set to: {display_name}")
        self.phase_detail_lbl.setText(f"✓ Cover art selected: {display_name}")
        win = self.window()
        if win and hasattr(win, "_sync_multi_disc_cover_art"):
            win._sync_multi_disc_cover_art()

    def _load_custom_art_file(self, chosen_path: Path) -> bool:
        if not chosen_path.exists() or not chosen_path.is_file():
            return False

        if chosen_path.stat().st_size > 30 * 1024 * 1024:
            QMessageBox.warning(self, "Invalid Image", "Cover art must be 30 MB or smaller.")
            return False

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
            return False

        pix = QPixmap.fromImageReader(reader) if hasattr(QPixmap, "fromImageReader") else QPixmap(str(chosen_path))
        if pix.isNull():
            QMessageBox.warning(self, "Invalid Image", "The selected file is not a valid image format.")
            return False

        needs_convert = sniffed is None or sniffed != suffix or chosen_path.stat().st_size > _MAX_ART_BYTES
        if needs_convert:
            from whipit.config import get_cache_dir
            covers_dir = get_cache_dir() / "covers"
            covers_dir.mkdir(parents=True, exist_ok=True)
            conv_path = covers_dir / f"custom_{uuid.uuid4().hex[:8]}_{chosen_path.stem}.jpg"
            img = pix.toImage()
            if img.hasAlphaChannel():
                flat = QImage(img.size(), QImage.Format.Format_RGB32)
                flat.fill(QColor("white"))
                painter = QPainter(flat)
                painter.drawImage(0, 0, img)
                painter.end()
                img = flat
            img.save(str(conv_path), "JPEG", 95)
            target_path = conv_path
        else:
            target_path = chosen_path

        self._apply_user_cover_art(target_path, chosen_path.name)
        return True

    def _choose_custom_art(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Album Cover Art",
            "",
            "Image Files (*.jpg *.jpeg *.png *.webp);;All Files (*)",
        )
        if not file_path:
            return
        self._load_custom_art_file(Path(file_path))

    def _paste_cover_art_from_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return

        mime = clipboard.mimeData()
        if mime is not None and mime.hasImage():
            img = clipboard.image()
            if not img.isNull() and img.width() * img.height() <= 50_000_000:
                from whipit.config import get_cache_dir
                if img.hasAlphaChannel():
                    flat = QImage(img.size(), QImage.Format.Format_RGB32)
                    flat.fill(QColor("white"))
                    painter = QPainter(flat)
                    painter.drawImage(0, 0, img)
                    painter.end()
                    img = flat
                covers_dir = get_cache_dir() / "covers"
                covers_dir.mkdir(parents=True, exist_ok=True)
                dest_path = covers_dir / f"clipboard_{uuid.uuid4().hex[:8]}.jpg"
                if img.save(str(dest_path), "JPEG", 95):
                    self._apply_user_cover_art(dest_path, "Clipboard Image")
                    return

        if mime is not None and mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    local_path = Path(url.toLocalFile())
                    if local_path.is_file() and self._load_custom_art_file(local_path):
                        return

        show_message_dialog(
            self,
            QMessageBox.Icon.Information,
            "No Image Found",
            "No valid image was found in the clipboard.",
        )

    def _reset_cover_art(self) -> None:
        if self.session.downloaded_art_path and self.session.downloaded_art_path.exists():
            self.session.reset_art_to_original()
            if self.session.art_path:
                self._render_cover_art(self.session.art_path)
                self.adapter.set_cover_art(self.session.art_path)
            self.reset_art_btn.setVisible(False)
            self.phase_detail_lbl.setText("✓ Restored Cover Art Archive image.")
        else:
            self.session.art_is_user_selected = False
            self.session.clear_art()
            self._render_cover_art_placeholder()
            self.adapter.set_cover_art(None)
            self.reset_art_btn.setVisible(False)

    def _populate_track_table(self, tracks: List[TrackInfo]) -> None:
        if not tracks:
            self.track_table.clear_tracks()
            self.track_table.setVisible(False)
            self.track_placeholder.setVisible(True)
            self.tracklist_header_lbl.setText("Track Listing")
            return

        self.track_table.populate_tracks(tracks, album_artist=self.session.artist)
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

        if not tracks_to_edit and not self.session.is_disc_loaded:
            show_message_dialog(
                self,
                QMessageBox.Icon.Information,
                "No Disc Loaded",
                "Please insert an Audio CD and wait for it to be detected before editing track titles.",
            )
            return

        orig_identity = (
            self.session.artist,
            self.session.title,
            self.session.year,
            self.session.disc_number,
            self.session.total_discs,
        )
        clear_mb_override: Optional[bool] = None

        dialog_fn = _get_override("prompt_edit_metadata_dialog", prompt_edit_metadata_dialog)
        if dialog_fn is prompt_edit_metadata_dialog:
            dlg = EditMetadataDialog(
                self,
                artist=self.session.artist if self.session.artist != "Unknown Artist" else "",
                album=self.session.title if self.session.title != "Unknown Album" else "",
                year=self.session.year,
                genre=self.session.genre,
                disc_number=self.session.disc_number,
                total_discs=self.session.total_discs,
                tracks=tracks_to_edit,
                has_mbid=bool(self.session.mbid),
                clear_mb_release_id=self.session.mb_release_overridden,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            res = dlg.get_metadata()
            clear_mb_override = dlg.clear_mb_release_id
        else:
            try:
                res = dialog_fn(
                    self,
                    artist=self.session.artist if self.session.artist != "Unknown Artist" else "",
                    album=self.session.title if self.session.title != "Unknown Album" else "",
                    year=self.session.year,
                    genre=self.session.genre,
                    disc_number=self.session.disc_number,
                    total_discs=self.session.total_discs,
                    tracks=tracks_to_edit,
                )
            except TypeError:
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

        if len(res) == 7:
            art, alb, yr, gnr, disc_num, tot_discs, updated_tracks = res
            self.session.disc_number = disc_num
            self.session.total_discs = tot_discs
        else:
            art, alb, yr, gnr, updated_tracks = res

        self.session.artist = art
        self.session.title = alb
        self.session.year = yr
        self.session.genre = gnr
        self.session.tracks = list(updated_tracks)

        if clear_mb_override is not None:
            self.session.mb_release_overridden = clear_mb_override
            self.session.auto_mb_release_overridden = False
        elif self.session.mbid:
            new_identity = (
                self.session.artist,
                self.session.title,
                self.session.year,
                self.session.disc_number,
                self.session.total_discs,
            )
            if new_identity != orig_identity:
                self.session.mb_release_overridden = True
                self.session.auto_mb_release_overridden = False

        self.album_title_lbl.setText(self.session.display_title)
        self._populate_track_table(self.session.tracks)
        if not self.session.mbid:
            self.unknown_chk.setChecked(True)
            self.uncataloged_badge.setText("✏ Custom Metadata")
            self.uncataloged_badge.setStyleSheet(LED_PILL_CYAN)
            self.uncataloged_badge.setToolTip(
                "Custom album and track metadata entered for this uncataloged disc."
            )
            self.uncataloged_badge.setVisible(True)
        self._update_links_display()
        disc_msg = (
            f" (Disc {self.session.disc_number} of {self.session.total_discs})"
            if self.session.is_multi_disc and self.session.disc_number
            else ""
        )
        self.phase_detail_lbl.setText(f"✓ Custom metadata updated{disc_msg}. Ready to rip.")
        self.log_emitted.emit(
            f"[{self.device_path or 'Drive'}] Custom metadata set: {self.session.artist} — {self.session.title}{disc_msg}"
        )
        win = self.window()
        if win and hasattr(win, "_update_batch_bar"):
            win._update_batch_bar()

    def _update_links_display(self) -> None:
        links = []
        if self.session.mb_url and is_strict_https_url(self.session.mb_url, ("musicbrainz.org",)):
            esc_mb = html.escape(self.session.mb_url, quote=True)
            edition_tag = f" ({self.session.mbid[:8]})" if self.session.mbid else ""
            links.append(
                f'<a href="{esc_mb}" style="color: {LINK_COLOR_MB}; text-decoration: none; font-weight: 500;">'
                f'↗ MusicBrainz{edition_tag}</a>'
            )
        if self.session.discogs_url and is_strict_https_url(self.session.discogs_url, ("discogs.com",)):
            esc_discogs = html.escape(self.session.discogs_url, quote=True)
            links.append(
                f'<a href="{esc_discogs}" style="color: {LINK_COLOR_DISCOGS}; text-decoration: none; font-weight: 500;">'
                '↗ Discogs</a>'
            )
        if not self.session.mb_url and self.session.mb_lookup_url and is_strict_https_url(self.session.mb_lookup_url, ("musicbrainz.org",)):
            esc_lookup = html.escape(self.session.mb_lookup_url, quote=True)
            links.append(
                f'<a href="{esc_lookup}" style="color: {LINK_COLOR_SUBMIT}; text-decoration: none; font-weight: 500;">'
                '↗ Add this Disc to MusicBrainz</a>'
            )
            links.append(
                f'<a href="#copy-mb-note" style="color: {LINK_COLOR_COPY}; text-decoration: none; font-weight: 500;">'
                '📋 Copy Submission Note</a>'
            )

        if links:
            sep = '<span style="color: #52525E;"> &nbsp;•&nbsp; </span>'
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

    def _update_hud_label(self, speed_x: Optional[float]) -> None:
        if speed_x is not None:
            kb_s = speed_x * 176.4
            tput_str = f"{kb_s / 1000.0:.2f} MB/s" if kb_s >= 1000.0 else f"{kb_s:.0f} KB/s"
            spd_str = f"{speed_x:.1f}x"
        else:
            spd_str = "--"
            tput_str = "--"
        retries = len(self._hud_retry_keys)
        stalls = self._hud_stall_count
        self.hud_stats_lbl.setText(
            f"Speed: {spd_str}  |  Throughput: {tput_str}  |  Retries: {retries}  |  Stalls: {stalls}"
        )

    def _reset_telemetry_hud(self) -> None:
        self._hud_retry_keys.clear()
        self._hud_stall_count = 0
        self._hud_in_stall_episode = False
        self._hud_stall_track = None
        self.sector_heatmap.reset()
        self._update_hud_label(None)

    def _reset_disc_state(self, clear_user_art: bool = True) -> None:
        self.session.reset(keep_user_art=not clear_user_art and self.session.art_is_user_selected)
        self.unknown_chk.setChecked(False)
        self._safe_speed_override = None
        self._copy_protected_from_meta = False
        self.protected_badge.setVisible(False)
        self._reset_telemetry_hud()
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
        self.track_table.clear_tracks()
        self.track_table.setVisible(False)
        self.track_placeholder.setVisible(True)
        self.tracklist_header_lbl.setText("Track Listing")
        self.album_title_lbl.setText("Ready to rip Audio CD")
        self.verification_lbl.setText("")
        self.verification_lbl.setVisible(False)
        self.uncataloged_badge.setText("ℹ Uncataloged Disc")
        self.uncataloged_badge.setStyleSheet(LED_PILL_UNCATALOGED)
        self.uncataloged_badge.setToolTip(
            "This disc TOC was not found in MusicBrainz.\n"
            "Ripping will proceed with generic or custom metadata tags."
        )
        self.uncataloged_badge.setVisible(False)

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

    def apply_safe_extraction_preset(self) -> None:
        """Configures defensive extraction settings to safely bypass firmware deadlocks."""
        self.keep_going_chk.setChecked(True)
        self._safe_speed_override = 4
        dev = self.device_path or "/dev/cdrom"
        if self.device_path:
            try:
                store_whipper_defeats_cache(self.device_path, False)
            except Exception:
                pass
            self._update_hardware_pills()
        self.log_emitted.emit(
            f"[{dev}] Applied Copy-Protected CD preset: 4x speed limit + Cache Bypass enabled."
        )

    def _evaluate_copy_protection(
        self,
        raw_stdout: str = "",
        disambiguation: str = "",
        barcode: Optional[str] = None,
        update_detail_label: bool = True,
    ) -> None:
        meta_protected = is_copy_protected_disc(
            whipper_info_stdout=raw_stdout,
            release_title=self.session.display_title,
            disambiguation=disambiguation,
            barcode=barcode,
            is_mixed_mode=False,
        )
        self._copy_protected_from_meta = meta_protected
        protected = meta_protected or (self._last_disc_status[1] == CDS_MIXED)
        if protected:
            was_hidden = self.protected_badge.isHidden()
            self.protected_badge.setVisible(True)
            if was_hidden:
                self.apply_safe_extraction_preset()
            if update_detail_label or was_hidden:
                self.phase_detail_lbl.setText(
                    "🛡️ Copy-Protected CD (CDS200 / Key2Audio) detected. "
                    "Automatically applied Safe Extraction Mode (4x Speed + Skip Errors)."
                )
        else:
            self._safe_speed_override = None
            self.protected_badge.setVisible(False)

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
            self.unknown_chk.setChecked(False)
            self.uncataloged_badge.setVisible(False)
            self.album_title_lbl.setText(self.session.display_title)
            self.phase_detail_lbl.setText(f"✓ Disc identified: {self.session.display_title}")
            self._set_pipeline_step("DISC_DETECTED")
            self.status_changed.emit(self.device_path or "", "Audio CD")
            self._update_links_display()
            if result.tracks and result.mbid:
                cache_key = (result.mbid, self.session.disc_number)
                self._release_cache[cache_key] = (result.discogs_url or None, list(result.tracks))
            self._populate_track_table(result.tracks)
            if not result.tracks and result.mbid:
                cache_key = (result.mbid, self.session.disc_number)
                if cache_key in self._release_cache and self._release_cache[cache_key][1]:
                    cached_discogs, cached_tracks = self._release_cache[cache_key]
                    self.session.discogs_url = cached_discogs or self.session.discogs_url
                    self.session.tracks = list(cached_tracks)
                    self._update_links_display()
                    self._populate_track_table(cached_tracks)
                elif _MBID_RE.fullmatch(result.mbid.strip()):
                    self._trigger_release_details_lookup(
                        result.mbid,
                        barcode=result.barcode,
                        artist=result.artist if result.artist != "Unknown Artist" else None,
                        title=result.title if result.title != "Unknown Album" else None,
                        expected_tracks=result.tracks_count,
                        disc_number=self.session.disc_number,
                        disc_id=result.disc_id,
                    )
            win = self.window()
            if win and hasattr(win, "_update_batch_bar"):
                win._update_batch_bar()
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

            if self.session.is_multi_disc and not self.session.art_is_user_selected:
                existing_art = self._find_existing_album_art()
                if existing_art:
                    self.inherit_cover_art(existing_art, from_desc="existing album archive")

            if not self.session.art_path and result.mbid and not self.session.art_is_user_selected:
                alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != result.mbid]
                self._fetch_cover_art(result.mbid, alternate_mbids=alt_mbids)
            elif not self.session.art_is_user_selected and not self.session.art_path:
                self._render_cover_art_placeholder("No Cover Art\n\nClick to add")

            raw_stdout = getattr(result, "raw_info_stdout", "") or getattr(result, "raw_output", "")
            first_disam = (
                self.session.matching_releases[0].disambiguation or ""
                if self.session.matching_releases
                else ""
            )
            disambiguation = getattr(result, "disambiguation", None) or first_disam
            barcode = getattr(result, "barcode", None) or self.session.barcode
            self._evaluate_copy_protection(
                raw_stdout=raw_stdout,
                disambiguation=disambiguation,
                barcode=barcode,
            )
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
            is_uncataloged = bool(result.mb_lookup_url) or any(
                k in err.lower() for k in ("not catalogued", "not cataloged", "not found in musicbrainz")
            )
            if is_uncataloged:
                self.unknown_chk.setChecked(True)
                self.uncataloged_badge.setText("ℹ Uncataloged Disc")
                self.uncataloged_badge.setStyleSheet(LED_PILL_UNCATALOGED)
                self.uncataloged_badge.setToolTip(
                    "This disc TOC was not found in MusicBrainz.\n"
                    "Ripping will proceed with generic or custom metadata tags."
                )
                self.uncataloged_badge.setVisible(True)
                self.phase_detail_lbl.setText(
                    f"ℹ {err} Uncataloged mode enabled (or click Edit Info to enter titles)."
                )
            else:
                self.uncataloged_badge.setVisible(False)
                self.phase_detail_lbl.setText(f"⚠ {err}")
            raw_stdout = getattr(result, "raw_info_stdout", "") or getattr(result, "raw_output", "")
            self._evaluate_copy_protection(
                raw_stdout=raw_stdout,
                disambiguation=getattr(result, "disambiguation", None) or "",
                barcode=getattr(result, "barcode", None) or self.session.barcode,
            )
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
        if self._safe_speed_override is not None:
            speed = min(speed, self._safe_speed_override) if speed else self._safe_speed_override
        manual_offset = self.prefs.offset_override
        flac_root = self.prefs.flac_root
        opus_root = self.prefs.opus_root
        generate_opus = self.prefs.generate_opus
        opus_bitrate = getattr(self.prefs, "opus_bitrate", 192)
        max_retries = getattr(self.prefs, "max_retries", 10)
        auto_eject = self.prefs.auto_eject
        selected_release_id = self.session.mbid

        self._whipper_parser = WhipperStreamParser(max_tries=max_retries)
        self._reset_telemetry_hud()
        self.track_table.reset_statuses()
        self.rip_progress_bar.setRange(0, 0)
        self.rip_progress_bar.setFormat("Initializing Whipper...")
        self.opus_progress_bar.setRange(0, 100)
        self.opus_progress_bar.setValue(0)
        self.opus_progress_bar.setFormat("Waiting for FLAC extraction...")
        self.verification_lbl.setText("")
        self.verification_lbl.setVisible(False)
        self._ripping_started = False
        self.is_extracting_audio = True
        self._rip_start_time = time.monotonic()
        self.phase_detail_lbl.setText(f"Starting extraction on {dev}...")
        self.status_changed.emit(self.device_path or "", "Ripping")

        has_custom = bool(
            self.session.artist != "Unknown Artist"
            or self.session.title != "Unknown Album"
            or self.session.year
            or self.session.genre
            or self.session.mb_release_overridden
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
            opus_bitrate=opus_bitrate,
            max_retries=max_retries,
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
        self.transition_to(DriveState.RIPPING)
        self.disc_status_badge.setText("Ripping Audio...")
        self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
        self._set_controls_running(True)

    def _restore_disc_status_badge(self) -> None:
        if not self.device_path:
            self.disc_status_badge.setText("No Drive")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_NO_DRIVE)
        elif self.session.is_disc_loaded:
            self.disc_status_badge.setText("Audio CD Loaded")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
        else:
            self.disc_status_badge.setText("Tray Empty")
            self.disc_status_badge.setStyleSheet(STATUS_BADGE_EMPTY)

    def _clear_rip_worker(self) -> None:
        self.worker = None
        self.is_extracting_audio = False
        self._set_controls_running(False)
        self._restore_disc_status_badge()

    def cancel_active_operation(self, confirm: bool = True) -> None:
        active_rip = bool(self.worker and self.worker.isRunning())
        active_cal = bool(self.calibrate_worker and self.calibrate_worker.isRunning())
        if not (active_rip or active_cal):
            return
        if confirm:
            label = "rip" if active_rip else "calibration"
            ans = QMessageBox.question(
                self,
                "Cancel Operation",
                f"Are you sure you want to cancel the active {label}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.phase_detail_lbl.setText("Cancelling active task and cleaning up...")
        self.cancel_btn.setEnabled(False)
        if active_rip and self.worker:
            self.worker.cancel()
        if active_cal and self.calibrate_worker and hasattr(self.calibrate_worker, "cancel"):
            self.calibrate_worker.cancel()

    def cancel_rip(self) -> None:
        self.cancel_active_operation(confirm=True)

    def _refresh_eject_btn_enabled(self) -> None:
        from whipit.ui.gui.main_window import _is_worker_running

        busy_with_tray = (
            self.is_extracting_audio
            or _is_worker_running(self.identify_worker)
            or _is_worker_running(self.calibrate_worker)
            or _is_worker_running(self.eject_worker)
            or _is_worker_running(self.close_worker)
        )
        self.eject_btn.setEnabled(not busy_with_tray)

    def eject_drive(self) -> None:
        from whipit.ui.gui.main_window import _is_worker_running

        if self.is_extracting_audio:
            return

        post_processing = _is_worker_running(self.worker)
        self.session.is_disc_loaded = False
        self._last_disc_status = (0, 0)
        self._disc_was_ejected = True
        self._disc_identified = False

        if not post_processing:
            self.transition_to(DriveState.EMPTY)
            self._reset_disc_state(clear_user_art=True)
            self._update_media_spec_strip()
            self.start_btn.setEnabled(False)
            self.identify_btn.setEnabled(True)

            if self.identify_worker:
                for sig_obj, slot in (
                    (getattr(self.identify_worker, "line_received", None), self.log_emitted.emit),
                    (getattr(self.identify_worker, "finished", None), self._clear_identify_worker),
                ):
                    if sig_obj is not None:
                        try:
                            sig_obj.disconnect(slot)
                        except Exception:
                            pass
                try:
                    if hasattr(self.identify_worker, "inspection_completed"):
                        self.identify_worker.inspection_completed.disconnect()
                except Exception:
                    pass
                try:
                    if hasattr(self.identify_worker, "cancel"):
                        self.identify_worker.cancel()
                    else:
                        self.identify_worker.terminate()
                except Exception:
                    pass
                self.identify_worker = None

            if self.cover_worker:
                for sig_obj, slot in (
                    (getattr(self.cover_worker, "cover_found", None), self._on_cover_art_found),
                    (getattr(self.cover_worker, "cover_failed", None), self._on_cover_art_failed),
                    (getattr(self.cover_worker, "finished", None), self._clear_cover_worker),
                ):
                    if sig_obj is not None:
                        try:
                            sig_obj.disconnect(slot)
                        except Exception:
                            pass
                try:
                    self.cover_worker.cancel()
                except Exception:
                    pass
                self.cover_worker = None

        self.is_tray_open = True
        self.eject_btn.setText(self._format_eject_btn_text())
        win = self.window()
        if win and hasattr(win, "_update_batch_bar"):
            win._update_batch_bar()

        dev = self.device_path or "/dev/cdrom"
        if not dev or _is_worker_running(self.eject_worker) or _is_worker_running(self.close_worker):
            return
        self.eject_btn.setEnabled(False)
        if not post_processing:
            self.phase_detail_lbl.setText(f"Ejecting {dev}...")

        WorkerClass = _get_override("EjectWorker", EjectWorker)
        self.eject_worker = WorkerClass(dev, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.eject_worker)
        if not post_processing:
            self.eject_worker.eject_finished.connect(lambda d: self.phase_detail_lbl.setText(f"Ejected {d}."))
        self.eject_worker.finished.connect(self._clear_eject_worker)
        self.eject_worker.start()

    def _clear_eject_worker(self) -> None:
        self.eject_worker = None
        self._refresh_eject_btn_enabled()

    def toggle_eject_close(self) -> None:
        """Toggles between ejecting and closing the drive tray based on current state."""
        if self.is_tray_open:
            self.close_drive_tray()
        else:
            self.eject_drive()

    def close_drive_tray(self) -> None:
        """Issues hardware command to pull optical tray shut."""
        dev = self.device_path or "/dev/cdrom"
        from whipit.ui.gui.main_window import _is_worker_running
        if (
            not dev
            or self.is_extracting_audio
            or _is_worker_running(self.identify_worker)
            or _is_worker_running(self.calibrate_worker)
            or _is_worker_running(self.close_worker)
            or _is_worker_running(self.eject_worker)
        ):
            return

        post_processing = _is_worker_running(self.worker)
        self.eject_btn.setEnabled(False)
        if not post_processing:
            self.phase_detail_lbl.setText(f"Closing tray for {dev}...")

        WorkerClass = _get_override("CloseWorker", CloseWorker)
        self.close_worker = WorkerClass(dev, parent=None)
        tracker = _get_override("_track_detached_worker", lambda w: w)
        tracker(self.close_worker)
        self.close_worker.close_finished.connect(self._on_close_finished)
        self.close_worker.finished.connect(self._clear_close_worker)
        self.close_worker.start()

    def _on_close_finished(self, dev: str) -> None:
        from whipit.ui.gui.main_window import _is_worker_running

        self.is_tray_open = False
        self.eject_btn.setText(self._format_eject_btn_text())
        if not _is_worker_running(self.worker):
            self.phase_detail_lbl.setText(f"Tray closed for {dev}. Polling disc...")
        win = self.window()
        if win and hasattr(win, "_update_batch_bar"):
            win._update_batch_bar()

    def _clear_close_worker(self) -> None:
        self.close_worker = None
        self._refresh_eject_btn_enabled()

    def _set_controls_running(self, running: bool) -> None:
        from whipit.ui.gui.main_window import _is_worker_running

        self.identify_btn.setEnabled(not running)
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.edit_meta_btn.setEnabled(not running)
        if running and not (_is_worker_running(self.worker) and not self.is_extracting_audio):
            self.eject_btn.setEnabled(False)
        else:
            self._refresh_eject_btn_enabled()

    def _on_log(self, level: LogLevel, message: str) -> None:
        prefix = f"[{level.value}]"
        clean = message.strip("═ \n")
        if clean and level in (LogLevel.INFO, LogLevel.OK, LogLevel.WARN, LogLevel.HEADER):
            self.phase_detail_lbl.setText(clean)
            if any(k in clean.lower() for k in ("calibrat", "accuraterip", "table of contents", "spin-up")):
                if self.rip_progress_bar.maximum() == 0 or "whipper" in self.rip_progress_bar.format().lower():
                    self.rip_progress_bar.setFormat(clean)
        dev_tag = f"[{self.device_path}] " if self.device_path else ""
        self.log_emitted.emit(f"{dev_tag}{prefix} {message}")

    def _start_release_details_lookup(
        self,
        mbid: str,
        barcode: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        expected_tracks: Optional[int] = None,
        disc_number: Optional[int] = None,
        disc_id: Optional[str] = None,
    ) -> None:
        if self.details_worker and self.details_worker.isRunning():
            self.details_worker.cancel()

        target_disc_no = disc_number if disc_number is not None else self.session.disc_number
        target_disc_id = disc_id or getattr(self._last_inspection, "disc_id", None)

        WorkerClass = _get_override("ReleaseDetailsWorker", ReleaseDetailsWorker)
        self.details_worker = WorkerClass(
            mbid,
            barcode=barcode,
            artist=artist,
            title=title,
            expected_tracks=expected_tracks,
            disc_number=target_disc_no,
            disc_id=target_disc_id,
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

    def _on_details_resolved(
        self,
        mbid: str,
        discogs_url: str,
        tracks: List[TrackInfo],
        disc_number: Optional[int] = None,
        total_discs: Optional[int] = None,
    ) -> None:
        if not self.session.mb_release_overridden:
            if disc_number is not None and (self.session.disc_number is None or self.session.disc_number == disc_number):
                self.session.disc_number = disc_number
            if total_discs is not None and (self.session.total_discs is None or self.session.total_discs == total_discs):
                self.session.total_discs = total_discs

        effective_disc_no = self.session.disc_number if self.session.disc_number is not None else disc_number
        cache_key = (mbid, effective_disc_no)
        if tracks:
            self._release_cache[cache_key] = (discogs_url or None, list(tracks))

        for rel in self.session.matching_releases:
            if rel.mbid == mbid:
                if effective_disc_no is not None and rel.disc_number is None:
                    rel.disc_number = effective_disc_no
                if total_discs is not None and rel.total_discs is None:
                    rel.total_discs = total_discs
                rel.tracks = list(tracks)
                break

        if self.session.mbid == mbid:
            self.session.discogs_url = discogs_url or self.session.discogs_url
            if not self.session.mb_release_overridden:
                self.session.tracks = list(tracks)
                self.album_title_lbl.setText(self.session.display_title)
                self._update_links_display()
                if tracks:
                    self._populate_track_table(tracks)
            else:
                self._update_links_display()
            rel_match = next((r for r in self.session.matching_releases if r.mbid == mbid), None)
            raw_stdout = (
                getattr(self._last_inspection, "raw_info_stdout", "")
                or getattr(self._last_inspection, "raw_output", "")
                if self._last_inspection
                else ""
            )
            self._evaluate_copy_protection(
                raw_stdout=raw_stdout,
                disambiguation=(getattr(rel_match, "disambiguation", None) or ""),
                barcode=getattr(rel_match, "barcode", None) or self.session.barcode,
                update_detail_label=False,
            )
            win = self.window()
            if win and hasattr(win, "_update_batch_bar"):
                win._update_batch_bar()

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
            cache_key = (mbid, self.session.disc_number)
            if self.track_table.rowCount() == 0 or not self.session.tracks:
                if cache_key in self._release_cache and self._release_cache[cache_key][1]:
                    cached_discogs, cached_tracks = self._release_cache[cache_key]
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
                        disc_number=self.session.disc_number,
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
            if manifest.verification.is_accurate:
                self.track_table.mark_all_accurip()
            if manifest.tracks.missing_tracks:
                self.track_table.mark_missing_tracks(manifest.tracks.missing_tracks)

        if state in (
            WorkflowState.RIP_EXTRACTED,
            WorkflowState.RIP_VALIDATED,
            WorkflowState.CANDIDATE_STAGED,
            WorkflowState.OPUS_ENCODED,
            WorkflowState.REPLAYGAIN_APPLIED,
            WorkflowState.CANDIDATE_VERIFIED,
            WorkflowState.PUBLISHING,
            WorkflowState.COMMITTED,
            WorkflowState.PUBLISH_FAILED,
            WorkflowState.ROLLBACK_FAILED,
            WorkflowState.ABORTED,
        ):
            was_extracting = self.is_extracting_audio
            self.is_extracting_audio = False
            if state == WorkflowState.RIP_EXTRACTED and was_extracting and self.prefs.auto_eject:
                self.is_tray_open = True
                self._disc_was_ejected = True
                self._disc_identified = False
                self.session.is_disc_loaded = False
                self.eject_btn.setText(self._format_eject_btn_text())
                win = self.window()
                if win and hasattr(win, "_update_batch_bar"):
                    win._update_batch_bar()
            self._refresh_eject_btn_enabled()

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
            if self.drive_state == DriveState.RIPPING and not self._ripping_started:
                self.disc_status_badge.setText("Ripping Audio...")
                self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
                msg = "Reading track pregaps & indexing audio sectors..."
                self.rip_progress_bar.setFormat(msg)
                self.phase_detail_lbl.setText(msg)
        elif isinstance(event, PreExtractionEvent):
            if self.drive_state == DriveState.RIPPING and not self._ripping_started:
                self.disc_status_badge.setText("Ripping Audio...")
                self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
            if not self._ripping_started:
                msg = event.phase or "Reading track pregaps & indexing audio sectors..."
                self.rip_progress_bar.setFormat(msg)
                self.phase_detail_lbl.setText(msg)
        elif isinstance(event, SectorFixupEvent):
            self.sector_heatmap.record_fixup(event.track, event.severity)
        elif isinstance(event, DriveStallEvent):
            if not self._hud_in_stall_episode or event.track_number != self._hud_stall_track:
                self._hud_stall_count += 1
                self._hud_in_stall_episode = True
                self._hud_stall_track = event.track_number
            self.sector_heatmap.record_stall(event.track_number)
            self.track_table.mark_track_stall(event.track_number)
            self._update_hud_label(None)
            sec = int(event.seconds_stalled)
            if event.track_number is not None:
                self.rip_progress_bar.setFormat(f"Track {event.track_number} Stalled ({sec}s)")
                self.phase_detail_lbl.setText(
                    f"Drive read stalled on Track {event.track_number} ({sec}s) — awaiting hardware response..."
                )
            else:
                self.rip_progress_bar.setFormat(f"Stalled ({sec}s)")
                self.phase_detail_lbl.setText(f"Drive read stalled ({sec}s) — awaiting hardware response...")
        elif isinstance(event, ReleasePickedEvent):
            self.session.mbid = event.mbid
            self.session.mb_url = f"https://musicbrainz.org/release/{event.mbid}"
            self._update_links_display()
            if not self.session.art_is_user_selected and not self.session.art_path:
                alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != event.mbid]
                self._fetch_cover_art(event.mbid, alternate_mbids=alt_mbids)
            cache_key = (event.mbid, self.session.disc_number)
            if self.track_table.rowCount() == 0 or not self.session.tracks:
                if cache_key in self._release_cache and self._release_cache[cache_key][1]:
                    cached_discogs, cached_tracks = self._release_cache[cache_key]
                    self.session.discogs_url = cached_discogs or self.session.discogs_url
                    self.session.tracks = list(cached_tracks)
                    self._update_links_display()
                    self._populate_track_table(cached_tracks)
                else:
                    self._trigger_release_details_lookup(
                        event.mbid,
                        artist=self.session.artist if self.session.artist != "Unknown Artist" else None,
                        title=self.session.title if self.session.title != "Unknown Album" else None,
                        expected_tracks=self._last_inspection.tracks_count if self._last_inspection else None,
                        disc_number=self.session.disc_number,
                        disc_id=getattr(self._last_inspection, "disc_id", None),
                    )
        elif isinstance(event, ChecksumMatchEvent):
            self.track_table.mark_track_crc(event.track, event.matched)
        elif isinstance(event, AccurateRipSummaryEvent):
            if event.is_accurate:
                self.track_table.mark_all_accurip()
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
            self._hud_in_stall_episode = False
            self._hud_stall_track = None
            if event.current_track > 0:
                self.disc_status_badge.setText(f"Ripping Track {event.current_track:02d}")
                self.disc_status_badge.setStyleSheet(STATUS_BADGE_AUDIO)
            if event.try_number > 1:
                self._hud_retry_keys.add((event.current_track, event.try_number))
            self.sector_heatmap.record_progress(
                track=event.current_track,
                total_tracks=event.total_tracks,
                percent=event.percent,
                try_number=event.try_number,
                phase=event.phase,
            )
            self.track_table.mark_track_progress(
                event.current_track,
                event.phase,
                event.percent,
                try_number=event.try_number,
                retry_reason=event.retry_reason,
            )
            self.rip_progress_bar.setRange(0, event.total_tracks)
            self.rip_progress_bar.setValue(event.current_track - 1)
            row = event.current_track - 1
            if 0 <= row < self.track_table.rowCount():
                self.track_table.selectRow(row)

            pct = int(event.percent)
            spd = event.speed_x
            now = time.monotonic()
            if self._rip_start_time == 0.0:
                self._rip_start_time = now

            max_ceiling = 48.0
            if self.device_path:
                detected_max = get_drive_max_speed(self.device_path)
                if detected_max:
                    max_ceiling = float(detected_max)
                elif is_usb_drive(self.device_path):
                    max_ceiling = 24.0

            same_sample_scope = (
                self._speed_sample_track == event.current_track
                and self._speed_sample_try == event.try_number
                and (not self._speed_sample_phase or self._speed_sample_phase == event.phase)
            )
            if not same_sample_scope:
                self._current_speed_x = None

            if spd is None and same_sample_scope and self._speed_sample_time > 0:
                delta_t = now - self._speed_sample_time
                delta_pct = (event.percent - self._speed_sample_percent) / 100.0
                track_dur = None
                if self.session.tracks and 0 <= row < len(self.session.tracks):
                    track_dur = parse_duration_seconds(self.session.tracks[row].duration)
                if not track_dur and self.session.duration and event.total_tracks > 0:
                    disc_dur = parse_duration_seconds(self.session.duration)
                    if disc_dur:
                        track_dur = disc_dur / event.total_tracks

                if track_dur and delta_t >= 0.8 and delta_pct > 0:
                    raw_spd = (delta_pct * track_dur) / delta_t
                    clamped = min(raw_spd, max_ceiling)
                    if 0.5 <= clamped <= max_ceiling:
                        spd = clamped
                        self._speed_sample_time = now
                        self._speed_sample_percent = event.percent

            if not same_sample_scope or self._speed_sample_time == 0.0:
                self._speed_sample_time = now
                self._speed_sample_percent = event.percent
                self._speed_sample_track = event.current_track
                self._speed_sample_try = event.try_number
                self._speed_sample_phase = event.phase
            elif (event.percent - self._speed_sample_percent) > 0:
                self._speed_sample_time = now
                self._speed_sample_percent = event.percent

            if spd is not None:
                spd = min(spd, max_ceiling)
                if self._current_speed_x is not None and same_sample_scope:
                    self._current_speed_x = round(0.4 * spd + 0.6 * self._current_speed_x, 1)
                else:
                    self._current_speed_x = round(spd, 1)

            display_spd = self._current_speed_x if self._current_speed_x is not None else spd
            self._update_hud_label(display_spd)
            spd_str = f" @ {display_spd:.1f}x" if display_spd is not None else ""
            max_tries = getattr(event, "max_tries", 10)
            if event.try_number > 1:
                retry_reason = getattr(event, "retry_reason", None)
                reason_txt = f" — {retry_reason}" if retry_reason else ""
                self.rip_progress_bar.setFormat(
                    f"{event.phase} Track {event.current_track} of {event.total_tracks} "
                    f"(Try {event.try_number} of {max_tries})... ({pct}%{spd_str})"
                )
                self.phase_detail_lbl.setText(
                    f"Retrying Track {event.current_track} / {event.total_tracks} "
                    f"(Try {event.try_number} of {max_tries}{reason_txt}) — {event.phase}...{spd_str}"
                )
                self.status_changed.emit(self.device_path or "", f"Retry {event.try_number}/{max_tries} ({pct}%)")
            else:
                self.rip_progress_bar.setFormat(
                    f"{event.phase} Track {event.current_track} of {event.total_tracks}... ({pct}%{spd_str})"
                )
                self.phase_detail_lbl.setText(
                    f"{event.phase} Track {event.current_track} / {event.total_tracks}...{spd_str}"
                )
                self.status_changed.emit(self.device_path or "", f"Ripping {pct}%{spd_str}")
        elif isinstance(event, AlbumHeaderEvent):
            if not self._ripping_started:
                if event.artist:
                    self.session.artist = event.artist
                if event.title:
                    self.session.title = event.title
                self.album_title_lbl.setText(self.session.display_title)

    def _on_transcode_progress(self, completed: int, total: int, track_name: str, success: bool) -> None:
        opus_rate = getattr(self.prefs, "opus_bitrate", 192)
        self.opus_progress_bar.setRange(0, total)
        self.opus_progress_bar.setValue(completed)
        status = "OK" if success else "ERR"
        self.opus_progress_bar.setFormat(f"{completed}/{total} tracks ({track_name} [{status}])")
        self.phase_detail_lbl.setText(f"Transcoding Opus {opus_rate}k: {completed} of {total} complete")

    def _on_disc_spinup_poll(self, device: str, hardware_id: str, elapsed: float, ready: Optional[bool]) -> None:
        if ready is True:
            self.phase_detail_lbl.setText(f"Disc ready in {device}.")
        elif ready is False:
            self.phase_detail_lbl.setText(f"Timeout waiting for disc in {device}.")

    def _on_workflow_finished(self, success: bool, message: str) -> None:
        self.is_extracting_audio = False
        self._set_controls_running(False)
        self.rip_progress_bar.setRange(0, 100)
        all_acc = "Verified (All tracks" in self.verification_lbl.text()
        self.track_table.finalize_statuses(success=success, all_accurate=all_acc)
        self.track_table.clearSelection()
        if success:
            self.sector_heatmap.mark_complete()
            self.transition_to(DriveState.COMMITTED)
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
            self.transition_to(DriveState.ERROR)
            self.rip_progress_bar.setFormat("Stopped")
            self.phase_detail_lbl.setText(message)
            self.status_changed.emit(self.device_path or "", "Stopped")
        self._current_speed_x = None
        self._speed_sample_time = 0.0
        self._speed_sample_try = 1
        self._speed_sample_phase = ""
        self._restore_disc_status_badge()
        self._update_hud_label(None)
        self._update_hardware_pills()

    def _on_release_combo_changed(self, idx: int) -> None:
        if not self.session.matching_releases or idx < 0 or idx >= len(self.session.matching_releases):
            return
        rel = self.session.matching_releases[idx]
        self.session.select_release(rel)
        self.album_title_lbl.setText(self.session.display_title)
        self._update_links_display()
        cache_key = (rel.mbid, self.session.disc_number)
        if rel.tracks:
            self._populate_track_table(rel.tracks)
        elif cache_key in self._release_cache and self._release_cache[cache_key][1]:
            cached_discogs, cached_tracks = self._release_cache[cache_key]
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
                disc_number=self.session.disc_number,
                disc_id=getattr(self._last_inspection, "disc_id", None),
            )
        if rel.mbid and not self.session.art_is_user_selected:
            alt_mbids = [r.mbid for r in self.session.matching_releases if r.mbid and r.mbid != rel.mbid]
            self._fetch_cover_art(rel.mbid, alternate_mbids=alt_mbids)
        raw_stdout = (
            getattr(self._last_inspection, "raw_info_stdout", "")
            or getattr(self._last_inspection, "raw_output", "")
            if self._last_inspection
            else ""
        )
        self._evaluate_copy_protection(
            raw_stdout=raw_stdout,
            disambiguation=getattr(rel, "disambiguation", None) or "",
            barcode=getattr(rel, "barcode", None) or self.session.barcode,
        )

    def _on_prompt_requested(self, *args) -> None:
        if len(args) == 1 and hasattr(args[0], "prompt_type"):
            request = args[0]
            if request.is_resolved:
                return
            try:
                res = self._dispatch_modal_prompt(request.prompt_type, request.payload)
            except Exception:
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
            dev, err_reason = payload if isinstance(payload, tuple) and len(payload) == 2 else (payload, None)
            try:
                return prompt_calibration_retry_dialog(self, dev, error_reason=err_reason)
            except TypeError:
                return prompt_calibration_retry_dialog(self, dev)
        if prompt_type == "prompt_extraction_error":
            dev = payload[0]
            avail = payload[1]
            err_reason = payload[2] if len(payload) > 2 else None
            has_partial = payload[3] if len(payload) > 3 else True
            is_stall = bool(payload[4]) if len(payload) > 4 else False
            stalled_tr = payload[5] if len(payload) > 5 else None
            kwargs: Dict[str, Any] = {
                "error_reason": err_reason,
                "has_partial_tracks": has_partial,
                "is_hardware_stall": is_stall,
            }
            if stalled_tr is not None:
                kwargs["stalled_track"] = stalled_tr
            try:
                return prompt_extraction_error_dialog(self, dev, avail, **kwargs)
            except TypeError:
                kwargs.pop("stalled_track", None)
                try:
                    return prompt_extraction_error_dialog(self, dev, avail, **kwargs)
                except TypeError:
                    kwargs.pop("is_hardware_stall", None)
                    try:
                        return prompt_extraction_error_dialog(self, dev, avail, **kwargs)
                    except TypeError:
                        return prompt_extraction_error_dialog(self, dev, avail)
        if prompt_type == "prompt_conflict_resolution":
            flac_dir = payload[0] if len(payload) > 0 else None
            opus_dir = payload[1] if len(payload) > 1 else None
            warn = payload[2] if len(payload) > 2 else None
            suggested_suffix = payload[3] if len(payload) > 3 else None
            try:
                return prompt_conflict_resolution_dialog(
                    self, flac_dir, opus_dir, warn, suggested_suffix=suggested_suffix
                )
            except TypeError:
                return prompt_conflict_resolution_dialog(self, flac_dir, opus_dir, warn)
        if prompt_type == "wait_for_user_reload":
            dev, msg = payload if isinstance(payload, tuple) and len(payload) == 2 else (self.device_path or "", str(payload))
            return prompt_wait_for_reload_dialog(self, dev, msg)
        if prompt_type == "prompt_cdr_detected":
            return prompt_yes_no_dialog(
                self,
                "CD-R Disc Detected",
                f"The disc in {payload} appears to be a recordable CD-R.\n"
                "AccurateRip checksums may not apply.\n\nContinue ripping with --cdr?",
            )
        if prompt_type == "prompt_unknown_disc":
            return prompt_yes_no_dialog(
                self,
                "Uncataloged Disc",
                "This Audio CD was not found in the MusicBrainz database.\n\n"
                "Continue ripping with generic track metadata (-U)?",
            )
        if prompt_type == "select_loaded_drive":
            return prompt_select_loaded_drive_dialog(self, payload)
        if prompt_type == "prompt_extraction_speed":
            return prompt_extraction_speed_dialog(self)
        if prompt_type == "confirm_install_dependencies":
            missing_tools, install_cmd, manager = payload
            return prompt_yes_no_dialog(
                self,
                "Install Missing Dependencies",
                f"Missing required system dependencies:\n  {', '.join(missing_tools)}\n\n"
                f"Install using {manager} now?\n\nCommand:\n  {install_cmd}",
            )
        if prompt_type == "confirm_purge_stage":
            stage_dir, flac_count = payload
            return prompt_yes_no_dialog(
                self,
                "Orphaned Staging Directory",
                f"Found {flac_count} FLAC track(s) in an orphaned staging folder:\n  {stage_dir}\n\n"
                "Permanently delete this orphaned staging folder?",
            )
        return GuiInteractionAdapter._default_for_prompt(prompt_type)
