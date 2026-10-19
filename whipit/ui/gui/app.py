"""Entrypoint for the whipit-gui desktop application."""

from __future__ import annotations

import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        sys.stderr.write(
            "\033[0;31m[ERROR]\033[0m PyQt6 is not installed.\n"
            "To use the WhipIt desktop GUI, install the 'gui' extra:\n"
            "  pip install 'whipit[gui]'\n"
            "or run './install.sh' from the source directory.\n"
        )
        return 1

    from whipit.ui.gui.main_window import MainWindow

    qt_args = list(argv) if argv is not None else sys.argv
    app = QApplication(qt_args)
    app.setApplicationName("WhipIt")
    app.setApplicationDisplayName("WhipIt")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
