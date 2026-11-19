"""Knoppix-inspired ANSI-palette QSyntaxHighlighter for WhipIt's live activity console."""

from __future__ import annotations

import re
from typing import List, Optional, Pattern, Tuple

from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from whipit.ui.gui.styles import (
    COLOR_ACCENT_AMBER,
    COLOR_ACCENT_CYAN,
    COLOR_ACCENT_GREEN,
    COLOR_ACCENT_RED,
    COLOR_TEXT_PRIMARY,
)


def _fmt(color: str, bold: bool = False, underline: bool = False) -> QTextCharFormat:
    char_fmt = QTextCharFormat()
    char_fmt.setForeground(QColor(color))
    if bold:
        char_fmt.setFontWeight(QFont.Weight.Bold)
    if underline:
        char_fmt.setFontUnderline(True)
    return char_fmt


class ConsoleHighlighter(QSyntaxHighlighter):
    """Applies Knoppix-style semantic colorization to WhipIt activity console lines."""

    def __init__(self, document: Optional[QTextDocument] = None) -> None:
        super().__init__(document)

        self._base_default = _fmt("#B4B4C2")
        self._base_ok = _fmt("#86EFAC")
        self._base_warn = _fmt("#FCD34D")
        self._base_error = _fmt("#FCA5A5")
        self._base_banner = _fmt(COLOR_ACCENT_CYAN, bold=True)

        self._fmt_ok_bold = _fmt(COLOR_ACCENT_GREEN, bold=True)
        self._fmt_cyan_bold = _fmt(COLOR_ACCENT_CYAN, bold=True)
        self._fmt_amber_bold = _fmt(COLOR_ACCENT_AMBER, bold=True)
        self._fmt_red_bold = _fmt(COLOR_ACCENT_RED, bold=True)
        self._fmt_white_bold = _fmt(COLOR_TEXT_PRIMARY, bold=True)
        self._fmt_url = _fmt(COLOR_ACCENT_CYAN, underline=True)

        self._re_error_line = re.compile(
            r"\[ERROR\]|^(?:ERROR|CRITICAL):|Traceback|RuntimeError:|stopped responding|halted",
            re.IGNORECASE,
        )
        self._re_warn_line = re.compile(
            r"\[(?:WARN|RECOVERY)\]|^WARNING:|checksums do not match|\(try \d+",
            re.IGNORECASE,
        )
        self._re_ok_line = re.compile(
            r"\[OK\]|Complete!|All tracks accurately ripped|Status:\s*Copy OK|CRCs match",
            re.IGNORECASE,
        )
        self._re_banner_line = re.compile(r"═{4,}|\[HEADER\]")

        self._rules: List[Tuple[Pattern[str], QTextCharFormat]] = [
            (re.compile(r"\[OK\]"), self._fmt_ok_bold),
            (re.compile(r"\[(?:INFO|CONFIG|STARTUP|BATCH|HEADER)\]"), self._fmt_cyan_bold),
            (re.compile(r"\[(?:WARN|RECOVERY)\]"), self._fmt_amber_bold),
            (re.compile(r"\[ERROR\]"), self._fmt_red_bold),
            (re.compile(r"\[/dev/[^\]]+\]|/dev/(?:sr\d+|cdrom)"), self._fmt_amber_bold),
            (re.compile(r"\bINFO:whipper(?:\.[\w-]+)*:"), self._fmt_cyan_bold),
            (re.compile(r"\bWARNING:whipper(?:\.[\w-]+)*:"), self._fmt_amber_bold),
            (re.compile(r"\b(?:CRITICAL|ERROR):whipper(?:\.[\w-]+)*:"), self._fmt_red_bold),
            (re.compile(r"\b\d+(?:\.\d+)?x\b"), self._fmt_white_bold),
            (re.compile(r"https?://\S+"), self._fmt_url),
        ]

    def highlightBlock(self, text: str) -> None:
        if not text:
            return

        if self._re_error_line.search(text):
            self.setFormat(0, len(text), self._base_error)
        elif self._re_warn_line.search(text):
            self.setFormat(0, len(text), self._base_warn)
        elif self._re_ok_line.search(text):
            self.setFormat(0, len(text), self._base_ok)
        elif self._re_banner_line.search(text):
            self.setFormat(0, len(text), self._base_banner)
        else:
            self.setFormat(0, len(text), self._base_default)

        for pattern, char_fmt in self._rules:
            for match in pattern.finditer(text):
                start, end = match.span()
                if end > start:
                    self.setFormat(start, end - start, char_fmt)
