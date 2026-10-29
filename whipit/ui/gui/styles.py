"""Centralized application styling, palette constants, and CSS for WhipIt GUI."""

from __future__ import annotations

# Badge style snippets
STATUS_BADGE_AUDIO = (
    "background-color: #065f46; color: #a7f3d0; border-radius: 4px; padding: 4px; font-weight: bold;"
)
STATUS_BADGE_DATA = (
    "background-color: #92400e; color: #fef3c7; border-radius: 4px; padding: 4px; font-weight: bold;"
)
STATUS_BADGE_DISC = (
    "background-color: #1e3a5f; color: #bfdbfe; border-radius: 4px; padding: 4px; font-weight: bold;"
)
STATUS_BADGE_EMPTY = (
    "background-color: #374151; color: #d1d5db; border-radius: 4px; padding: 4px; font-weight: bold;"
)
STATUS_BADGE_NO_DRIVE = (
    "background-color: #7f1d1d; color: #fecaca; border-radius: 4px; padding: 4px; font-weight: bold;"
)

VERIFICATION_BADGE_ACCURATE = (
    "background-color: #064e3b; color: #6ee7b7; border: 1px solid #059669; border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)
VERIFICATION_BADGE_UNVERIFIED = (
    "background-color: #78350f; color: #fde68a; border: 1px solid #d97706; border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)
VERIFICATION_BADGE_ERROR = (
    "background-color: #7f1d1d; color: #fca5a5; border: 1px solid #dc2626; border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px;"
)


def get_application_stylesheet() -> str:
    return """
    QTabWidget::pane {
        border: 1px solid #2b3248;
        border-radius: 6px;
        background-color: transparent;
        top: -1px;
    }
    QTabBar::tab {
        background-color: #161a26;
        color: #94a3b8;
        border: 1px solid #2b3248;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: 6px 14px;
        font-weight: bold;
    }
    QTabBar::tab:selected {
        background-color: #1f273b;
        color: #22d3ee;
        border-bottom: 2px solid #06b6d4;
    }
    QTabBar::tab:hover:!selected {
        background-color: #1e2638;
        color: #e4e8f0;
    }
    QScrollArea {
        border: none;
        background-color: transparent;
    }
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
    QLabel#trackPlaceholder {
        background-color: #161a26;
        border: 1px dashed #2b3248;
        border-radius: 6px;
        color: #64748b;
        font-size: 11px;
        padding: 16px;
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
    QTableWidget::item:selected {
        background-color: #0891b2;
        color: #ffffff;
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
