"""Tests for Knoppix-style ConsoleHighlighter on MainWindow.console_box."""

from __future__ import annotations

import pytest


def test_console_highlighter_colors_tokens_and_preserves_plain_text(win) -> None:
    pytest.importorskip("PyQt6")

    from whipit.ui.gui.styles import (
        COLOR_ACCENT_AMBER,
        COLOR_ACCENT_CYAN,
        COLOR_ACCENT_GREEN,
        COLOR_ACCENT_RED,
        COLOR_TEXT_PRIMARY,
    )
    from whipit.ui.gui.widgets import ConsoleHighlighter

    assert isinstance(win.console_highlighter, ConsoleHighlighter)
    assert win.console_box.objectName() == "consoleBox"

    win.console_box.clear()
    sample_lines = [
        "[/dev/sr0] [OK] Extraction completed at 12.4x",
        "[/dev/sr0] [WARN] Optical drive read stalled",
        "[ERROR] Failed publishing FLAC archive",
        "[BATCH] Starting concurrent extraction https://musicbrainz.org/release/123",
    ]
    for ln in sample_lines:
        win._append_console(ln)

    plain = win.console_box.toPlainText()
    for ln in sample_lines:
        assert ln in plain

    doc = win.console_box.document()
    assert doc is not None

    def _range_colors(block_idx: int) -> list[tuple[str, str]]:
        blk = doc.findBlockByNumber(block_idx)
        layout = blk.layout()
        assert layout is not None
        txt = blk.text()
        return [
            (txt[r.start : r.start + r.length], r.format.foreground().color().name().upper())
            for r in layout.formats()
        ]

    ok_spans = _range_colors(0)
    assert ("[/dev/sr0]", COLOR_ACCENT_AMBER) in ok_spans
    assert ("[OK]", COLOR_ACCENT_GREEN) in ok_spans
    assert ("12.4x", COLOR_TEXT_PRIMARY) in ok_spans

    warn_spans = _range_colors(1)
    assert ("[WARN]", COLOR_ACCENT_AMBER) in warn_spans

    err_spans = _range_colors(2)
    assert ("[ERROR]", COLOR_ACCENT_RED) in err_spans

    batch_spans = _range_colors(3)
    assert ("[BATCH]", COLOR_ACCENT_CYAN) in batch_spans
    assert any(tok.startswith("https://") and col == COLOR_ACCENT_CYAN for tok, col in batch_spans)
