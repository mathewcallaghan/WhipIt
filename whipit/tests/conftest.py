import os
import shutil
import tempfile
from pathlib import Path
import pytest
from whipit.config import get_cache_dir
from whipit.models import AlbumMetadata, DeviceInfo, JobManifest, PathLayout, TransactionJournal

# Session-wide test sandbox ensuring tests/background threads never escape to ~/.config
_SESSION_SANDBOX = Path(tempfile.mkdtemp(prefix="whipit_pytest_session_"))
_SESSION_CONFIG = _SESSION_SANDBOX / "config"
_SESSION_CACHE = _SESSION_SANDBOX / "cache"
_SESSION_STATE = _SESSION_SANDBOX / "state"
_SESSION_OUTPUT = _SESSION_SANDBOX / "output"

for _p in (_SESSION_CONFIG, _SESSION_CACHE, _SESSION_STATE, _SESSION_OUTPUT):
    _p.mkdir(parents=True, exist_ok=True)

os.environ["WHIPIT_OUTPUT_DIR"] = str(_SESSION_OUTPUT)
os.environ["XDG_CACHE_HOME"] = str(_SESSION_CACHE)
os.environ["XDG_STATE_HOME"] = str(_SESSION_STATE)
os.environ["XDG_CONFIG_HOME"] = str(_SESSION_CONFIG)


@pytest.fixture(scope="session", autouse=True)
def cleanup_session_sandbox():
    yield
    shutil.rmtree(_SESSION_SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def temp_env():
    root = Path(tempfile.mkdtemp(prefix="whipit_test_"))
    output = root / "WhipIt"
    output.mkdir()
    cache = root / "cache"
    cache.mkdir()
    state = root / "state"
    state.mkdir()
    config = root / "config"
    config.mkdir()

    old_env = os.environ.copy()
    os.environ["WHIPIT_OUTPUT_DIR"] = str(output)
    os.environ["XDG_CACHE_HOME"] = str(cache)
    os.environ["XDG_STATE_HOME"] = str(state)
    os.environ["XDG_CONFIG_HOME"] = str(config)

    yield {"root": root, "output": output, "cache": cache, "state": state, "config": config}

    os.environ.clear()
    os.environ.update(old_env)
    os.environ["WHIPIT_OUTPUT_DIR"] = str(_SESSION_OUTPUT)
    os.environ["XDG_CACHE_HOME"] = str(_SESSION_CACHE)
    os.environ["XDG_STATE_HOME"] = str(_SESSION_STATE)
    os.environ["XDG_CONFIG_HOME"] = str(_SESSION_CONFIG)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def sample_manifest(temp_env):
    output = temp_env["output"]
    cache = get_cache_dir()
    job_id = "test-job-uuid-1234"
    return JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="HL-DT-ST_BD-RE_WH16NS40", read_offset=6),
        metadata=AlbumMetadata(artist="Pink Floyd", album="The Wall", album_stem="Pink Floyd - The Wall"),
        paths=PathLayout(
            output_root=output,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=output / f".whipit-stage.{job_id}",
            final_flac_dir=output / "Pink Floyd - The Wall (FLAC)",
            final_opus_dir=output / "Pink Floyd - The Wall (Opus)",
        ),
        transaction=TransactionJournal(),
    )


@pytest.fixture
def win(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from whipit.ui.gui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "_trigger_drive_poll", lambda self: None)
    monkeypatch.setattr(MainWindow, "_startup_checks", lambda self: None)
    monkeypatch.setattr("whipit.ui.gui.main_window.prompt_unconfigured_offset_dialog", lambda *a, **kw: ("zero", 0))

    _ = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.drive_poll_timer.stop()
    yield w
    w.close()
    w.deleteLater()


_DRIVE_OPERATION_TARGETS = (
    "whipit.workflow",
    "whipit.ripping.engine",
    "whipit.ripping.calibration",
    "whipit.ripping.identify",
    "whipit.ripping.whipper",
    "whipit.ui.gui.worker",
    "whipit.ui.gui.main_window",
    "whipit.ui.gui.drive_panel",
)


@pytest.fixture(autouse=True)
def no_real_drive_hardware_actions(monkeypatch):
    """Stop tests and background threads from touching physical optical drive hardware.

    release_drive_locks(), eject_drive(), and close_tray() are stubbed out across
    pipeline and GUI consumer modules. Tests needing to test the real functions
    import them directly from whipit.config, which is intentionally left unpatched.
    """
    import importlib

    for mod_name in _DRIVE_OPERATION_TARGETS:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:  # e.g. PyQt6 not installed
            continue
        if hasattr(mod, "release_drive_locks"):
            monkeypatch.setattr(mod, "release_drive_locks", lambda *a, **k: None)
        if hasattr(mod, "eject_drive"):
            monkeypatch.setattr(mod, "eject_drive", lambda *a, **k: None)
        if hasattr(mod, "close_tray"):
            monkeypatch.setattr(mod, "close_tray", lambda *a, **k: None)
