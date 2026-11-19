"""Centralized Pro-Audio application styling, palette constants, and QSS for WhipIt GUI."""

from __future__ import annotations

# Pro-Audio Signal Palette
COLOR_BG_BASE = "#18181C"
COLOR_BG_CARD = "#222228"
COLOR_BG_RECESS = "#141418"
COLOR_BORDER_RACK = "#33333C"

COLOR_ACCENT_CYAN = "#00E5FF"
COLOR_ACCENT_GREEN = "#30D158"
COLOR_ACCENT_AMBER = "#FF9F0A"
COLOR_ACCENT_RED = "#FF453A"
COLOR_TEXT_PRIMARY = "#ECECF1"
COLOR_TEXT_MUTED = "#9494A3"

# Link accent tokens
LINK_COLOR_MB = COLOR_ACCENT_CYAN
LINK_COLOR_DISCOGS = COLOR_ACCENT_GREEN
LINK_COLOR_SUBMIT = COLOR_ACCENT_CYAN
LINK_COLOR_COPY = COLOR_ACCENT_AMBER

# Shared inline component styles
STYLE_COMPACT_BTN = "padding: 2px 8px; font-size: 11px;"
STYLE_HEADER_BTN = "padding: 0 12px; font-size: 11px;"
STYLE_SUBTITLE_MUTED = f"color: {COLOR_TEXT_MUTED}; font-size: 11px;"
STYLE_HEADER_CYAN = f"font-weight: bold; color: {COLOR_ACCENT_CYAN}; font-size: 11px;"
STYLE_LABEL_MUTED_BOLD = f"font-weight: bold; font-size: 11px; color: {COLOR_TEXT_MUTED};"
STYLE_BATCH_INFO = f"color: {COLOR_ACCENT_CYAN}; font-weight: 500; font-size: 11px;"
STYLE_STATUS_OK = f"color: {COLOR_ACCENT_GREEN}; font-weight: bold; font-size: 11px;"
STYLE_STATUS_INFO = f"color: {COLOR_ACCENT_CYAN}; font-weight: bold; font-size: 11px;"
STYLE_EXTERNAL_LINKS = "padding: 2px 0px; font-size: 11px;"
STYLE_CONFLICT_WARNING = (
    f"color: #FFD1CE; background-color: #3B1618; border: 1px solid {COLOR_ACCENT_RED}; "
    "border-radius: 5px; padding: 8px; font-weight: bold;"
)

# Badge style snippets
STATUS_BADGE_AUDIO = (
    f"background-color: #143320; color: {COLOR_ACCENT_GREEN}; border: 1px solid #246B3D; "
    "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
)
STATUS_BADGE_DATA = (
    f"background-color: #36250B; color: {COLOR_ACCENT_AMBER}; border: 1px solid #8A5808; "
    "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
)
STATUS_BADGE_DISC = (
    f"background-color: #0E2F3A; color: {COLOR_ACCENT_CYAN}; border: 1px solid #1E6E82; "
    "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
)
STATUS_BADGE_EMPTY = (
    f"background-color: {COLOR_BG_CARD}; color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER_RACK}; "
    "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
)
STATUS_BADGE_NO_DRIVE = (
    f"background-color: #3B1618; color: {COLOR_ACCENT_RED}; border: 1px solid #992B25; "
    "border-radius: 4px; padding: 4px 8px; font-weight: bold;"
)

VERIFICATION_BADGE_ACCURATE = (
    f"background-color: #143320; color: {COLOR_ACCENT_GREEN}; border: 1px solid {COLOR_ACCENT_GREEN}; "
    "border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)
VERIFICATION_BADGE_UNVERIFIED = (
    f"background-color: #36250B; color: {COLOR_ACCENT_AMBER}; border: 1px solid {COLOR_ACCENT_AMBER}; "
    "border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)
VERIFICATION_BADGE_ERROR = (
    f"background-color: #3B1618; color: {COLOR_ACCENT_RED}; border: 1px solid {COLOR_ACCENT_RED}; "
    "border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)

LED_PILL_GREEN = (
    f"background-color: #143320; color: {COLOR_ACCENT_GREEN}; border: 1px solid #246B3D; "
    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 10px;"
)
LED_PILL_CYAN = (
    f"background-color: #0E2F3A; color: {COLOR_ACCENT_CYAN}; border: 1px solid #1E6E82; "
    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 10px;"
)
LED_PILL_AMBER = (
    f"background-color: #36250B; color: {COLOR_ACCENT_AMBER}; border: 1px solid #8A5808; "
    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 10px;"
)
LED_PILL_RED = (
    f"background-color: #3B1618; color: {COLOR_ACCENT_RED}; border: 1px solid #992B25; "
    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 10px;"
)
LED_PILL_MUTED = (
    f"background-color: {COLOR_BG_RECESS}; color: {COLOR_TEXT_MUTED}; border: 1px solid {COLOR_BORDER_RACK}; "
    "border-radius: 4px; padding: 2px 8px; font-weight: bold; font-size: 10px;"
)


def get_application_stylesheet() -> str:
    return """
    QMainWindow, QDialog, QMessageBox {
        background-color: #18181C;
        color: #ECECF1;
    }
    QScrollArea, QWidget#mainScrollContent {
        border: none;
        background-color: #18181C;
    }
    QWidget#drivePanel {
        background-color: #222228;
    }
    QToolTip {
        background-color: #222228;
        color: #ECECF1;
        border: 1px solid #33333C;
        padding: 4px 6px;
    }
    QTabWidget::pane {
        border: 1px solid #33333C;
        border-radius: 6px;
        background-color: #222228;
        top: -1px;
    }
    QTabBar::tab {
        background-color: #18181C;
        color: #9494A3;
        border: 1px solid #33333C;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: 6px 14px;
        font-weight: bold;
    }
    QTabBar::tab:selected {
        background-color: #222228;
        color: #00E5FF;
        border-bottom: 2px solid #00E5FF;
    }
    QTabBar::tab:hover:!selected {
        background-color: #1F1F26;
        color: #ECECF1;
    }
    QMenuBar {
        background-color: #222228;
        color: #ECECF1;
        border-bottom: 1px solid #33333C;
        padding: 2px 4px;
    }
    QMenuBar::item:selected {
        background-color: #2C2C35;
        border-radius: 3px;
    }
    QMenu {
        background-color: #222228;
        color: #ECECF1;
        border: 1px solid #33333C;
    }
    QMenu::item:selected {
        background-color: #0D5C6E;
        color: #FFFFFF;
    }
    QFrame#pipelineFrame {
        background-color: #18181C;
        border: 1px solid #33333C;
        border-radius: 6px;
    }
    QGroupBox {
        background-color: #222228;
        font-weight: bold;
        border: 1px solid #33333C;
        border-radius: 6px;
        margin-top: 8px;
        padding-top: 10px;
        color: #ECECF1;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }
    QLabel, QCheckBox, QRadioButton {
        color: #DFDFE6;
    }
    QCheckBox, QRadioButton {
        background-color: transparent;
        spacing: 6px;
    }
    QCheckBox:hover, QRadioButton:hover {
        color: #FFFFFF;
    }
    QLabel#coverArtLabel {
        background-color: #141418;
        border: 1px solid #33333C;
        border-radius: 6px;
        color: #9494A3;
        font-size: 11px;
    }
    QLabel#coverArtLabel:hover {
        border-color: #00E5FF;
    }
    QLabel#trackPlaceholder {
        background-color: #141418;
        border: 1px solid #33333C;
        border-radius: 6px;
        color: #9494A3;
        font-size: 11px;
        padding: 16px;
    }
    QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
        background-color: #141418;
        color: #ECECF1;
        border: 1px solid #33333C;
        border-radius: 4px;
        padding: 4px 6px;
    }
    QPlainTextEdit#consoleBox {
        background-color: #101014;
        color: #D4D4DC;
        border: 1px solid #33333C;
        border-radius: 4px;
        padding: 6px;
    }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {
        border-color: #00E5FF;
    }
    QTableWidget {
        background-color: #141418;
        alternate-background-color: #1B1B21;
        color: #ECECF1;
        border: 1px solid #33333C;
        border-radius: 4px;
        gridline-color: #282830;
    }
    QTableWidget::item:selected {
        background-color: #0D5C6E;
        color: #FFFFFF;
    }
    QHeaderView::section {
        background-color: #222228;
        color: #00E5FF;
        padding: 4px 8px;
        font-weight: bold;
        border: none;
        border-bottom: 1px solid #33333C;
    }
    QPushButton, QToolButton {
        background-color: #2A2A32;
        color: #ECECF1;
        border: 1px solid #3D3D48;
        border-radius: 5px;
        padding: 6px 12px;
        font-weight: 500;
    }
    QPushButton:hover, QToolButton:hover {
        background-color: #34343E;
        border-color: #4D4D5A;
    }
    QPushButton:disabled {
        background-color: #1C1C22;
        color: #5C5C68;
        border-color: #2B2B34;
    }
    QPushButton#primaryButton {
        background-color: #0D5C6E;
        border-color: #00E5FF;
        color: #FFFFFF;
        font-weight: bold;
    }
    QPushButton#primaryButton:hover {
        background-color: #10748A;
    }
    QPushButton#primaryButton:disabled {
        background-color: #1C2428;
        border-color: #2B3940;
        color: #5C6B73;
    }
    QPushButton#dangerButton {
        background-color: #5C191B;
        border-color: #FF453A;
        color: #FFFFFF;
        font-weight: bold;
    }
    QPushButton#dangerButton:hover {
        background-color: #782023;
    }
    QPushButton#dangerButton:disabled {
        background-color: #241A1C;
        border-color: #3D2729;
        color: #6B5658;
    }
    QProgressBar {
        border: 1px solid #33333C;
        border-radius: 4px;
        background-color: #141418;
        text-align: center;
        color: #ECECF1;
        font-weight: bold;
        min-height: 20px;
    }
    QProgressBar::chunk {
        background-color: #00B8D4;
        border-radius: 3px;
    }
    QLabel#stepInactive {
        background-color: #18181C;
        color: #7A7A88;
        border: 1px solid #33333C;
        border-radius: 5px;
        font-weight: 600;
    }
    QLabel#stepActive {
        background-color: #0D4B59;
        color: #00E5FF;
        border: 1px solid #00E5FF;
        border-radius: 5px;
        font-weight: bold;
    }
    QLabel#stepDone {
        background-color: #143320;
        color: #30D158;
        border: 1px solid #246B3D;
        border-radius: 5px;
        font-weight: bold;
    }
    QLabel#stepError {
        background-color: #3B1618;
        color: #FF453A;
        border: 1px solid #FF453A;
        border-radius: 5px;
        font-weight: bold;
    }
    """
