"""Entrypoint for the whipit desktop application and CLI command interface."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional, Sequence

from whipit import __version__


def _install_excepthooks() -> None:
    def _hook(t, v, tb):
        sys.stderr.write("".join(traceback.format_exception(t, v, tb)))

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)


def _configure_logging(level_name: Optional[str] = None) -> None:
    lvl = (level_name or os.environ.get("WHIPIT_LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, lvl, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger().setLevel(numeric_level)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whipit",
        description="WhipIt: AccurateRip + MusicBrainz Archival CD Ripper & Opus Transcoder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-d", "--device",
        metavar="PATH",
        help="Specify CD-ROM drive path (e.g. /dev/sr0)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        metavar="PATH",
        help="Override destination directory",
    )
    parser.add_argument(
        "--calibrate-drive",
        action="store_true",
        help="Run the drive read offset calibration wizard",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show application version",
    )
    return parser


def _run_cli_calibration(device: Optional[str] = None) -> int:
    from whipit.config import resolve_optical_drive
    from whipit.ripping.calibration import CalibrationStatus, calibrate_drive
    from whipit.ui.base import CliInteractionAdapter

    adapter = CliInteractionAdapter()
    target_dev = resolve_optical_drive(device, ui=adapter)
    if not target_dev:
        sys.stderr.write("[ERROR] No optical drive available for calibration.\n")
        return 1

    sys.stdout.write(f"Starting drive calibration on {target_dev}...\n")
    res = calibrate_drive(dev=target_dev, ui=adapter)
    if res.status == CalibrationStatus.SUCCESS:
        if res.offset is not None:
            sys.stdout.write(f"[OK] Calibration successful: offset {res.offset:+d} stored.\n")
        else:
            sys.stdout.write("[OK] Calibration successful.\n")
        return 0
    elif res.status == CalibrationStatus.SKIPPED:
        sys.stdout.write("[INFO] Calibration skipped.\n")
        return 0
    else:
        sys.stderr.write("[ERROR] Calibration aborted or failed.\n")
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    _install_excepthooks()

    if argv is None:
        args_list = sys.argv[1:]
    elif argv and not argv[0].startswith("-") and (argv[0].endswith(".py") or "whipit" in argv[0]):
        args_list = list(argv)[1:]
    else:
        args_list = list(argv)

    parser = _build_arg_parser()
    try:
        args, qt_args = parser.parse_known_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    if args.output_dir:
        os.environ["WHIPIT_OUTPUT_DIR"] = str(Path(args.output_dir).expanduser().resolve())

    if args.device:
        os.environ["WHIPIT_DRIVE"] = args.device

    _configure_logging()

    if args.calibrate_drive:
        return _run_cli_calibration(device=args.device)

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

    app_args = [sys.argv[0]] + qt_args
    app = QApplication.instance()
    if app is None:
        app = QApplication(app_args)
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
