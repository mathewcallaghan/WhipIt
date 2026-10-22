"""Entrypoint for the whipit desktop application."""

from __future__ import annotations

import sys
import threading
import traceback
from typing import Optional, Sequence

def _install_excepthooks() -> None:
    def _hook(t, v, tb):
        sys.stderr.write("".join(traceback.format_exception(t, v, tb)))

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _install_excepthooks()
    try:
        from PyQt6.QtCore import QLockFile
        from PyQt6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        sys.stderr.write(
            "\033[0;31m[ERROR]\033[0m PyQt6 is not installed.\n"
            "To use the WhipIt desktop GUI, install PyQt6:\n"
            "  pip install 'PyQt6>=6.5.0'\n"
            "or run './install.sh' from the source directory.\n"
        )
        return 1

    from whipit.config import get_log_dir
    from whipit.ui.gui.main_window import MainWindow

    qt_args = list(argv) if argv is not None else sys.argv
    app = QApplication(qt_args)
    app.setApplicationName("WhipIt")
    app.setApplicationDisplayName("WhipIt")

    lock_file = QLockFile(str(get_log_dir() / "whipit.lock"))
    lock_file.setStaleLockTime(0)
    if not lock_file.tryLock(100):
        sys.stderr.write("Another instance of WhipIt is already running.\n")
        QMessageBox.warning(
            None,
            "WhipIt Already Running",
            "Another instance of WhipIt is already running.\n\n"
            "Only one instance can run at a time to prevent optical drive locking conflicts.",
        )
        return 0

    window = MainWindow()
    window.show()
    try:
        return app.exec()
    finally:
        lock_file.unlock()


if __name__ == "__main__":
    sys.exit(main())
