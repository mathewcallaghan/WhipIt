"""Regression tests for Phase 6 updater, bootstrap, launcher, diagnostics, uninstall, and Qt adapter."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from whipit.bootstrap import check_or_install_dependencies
from whipit.diagnostics import _can_read_device, _check_user_groups
from whipit.launcher import install_icon, uninstall_launcher
from whipit.models import JobManifest, WorkflowState
from whipit.state_store import ManifestStore
from whipit.ui.base import NullInteractionAdapter


def test_update_via_git_fails_on_nonzero_fetch_and_resets_head_on_pip_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted: list[tuple[bool, str, bool]] = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: unable to access"),
    )
    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    assert emitted[0][0] is False
    assert "Failed fetching release tags" in emitted[0][1]

    # Test prerelease tag skipped in favor of valid release tag + git reset on pip failure
    emitted.clear()
    git_cmds: list[list[str]] = []

    def mock_run(cmd: list[str], *a: object, **kw: object) -> subprocess.CompletedProcess[str]:
        git_cmds.append(list(cmd))
        if "tag" in cmd and "--list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v99.0.0-rc1\nv98.0.0\n", stderr="")
        if "rev-parse" in cmd and "HEAD" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="1111aaaa\n", stderr="")
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="2222bbbb\n", stderr="")
        if "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="wheel build error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)
    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    assert emitted[0][0] is False
    assert "Package installation failed" in emitted[0][1]
    assert any("reset" in c and "--hard" in c and "1111aaaa" in c for c in git_cmds)


def test_bootstrap_respects_interactive_false(monkeypatch: pytest.MonkeyPatch) -> None:
    prompted: list[bool] = []

    class PromptingAdapter(NullInteractionAdapter):
        def confirm_install_dependencies(self, missing_tools, install_cmd, manager) -> bool:
            prompted.append(True)
            return True

    monkeypatch.setattr("whipit.bootstrap.get_missing_dependencies", lambda: ["flac"])
    monkeypatch.setattr("whipit.bootstrap.get_missing_optional_dependencies", list)
    monkeypatch.setattr(
        "whipit.bootstrap._trusted",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in ("apt-get", "pkexec") else None,
    )

    ok = check_or_install_dependencies(interactive=False, ui=PromptingAdapter())
    assert ok is False
    assert prompted == []


def test_install_icon_handles_missing_asset_and_uninstall_removes_broken_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    icon_dest = tmp_path / "icons" / "whipit.svg"
    icon_dest.parent.mkdir(parents=True)
    desktop_dest = tmp_path / "apps" / "whipit.desktop"
    desktop_dest.parent.mkdir(parents=True)

    monkeypatch.setattr("whipit.launcher.get_icon_path", lambda: icon_dest)
    monkeypatch.setattr("whipit.launcher.get_desktop_file_path", lambda: desktop_dest)
    monkeypatch.setattr("whipit.launcher.get_asset_icon_path", lambda: tmp_path / "missing_asset.svg")
    monkeypatch.setattr("whipit.launcher.refresh_desktop_database", lambda: None)

    assert install_icon() == icon_dest
    assert not icon_dest.exists()

    # Create dangling symlinks and verify uninstall_launcher cleans them up
    desktop_dest.symlink_to(tmp_path / "nonexistent.desktop")
    icon_dest.symlink_to(tmp_path / "nonexistent.svg")
    ok, _msg = uninstall_launcher()
    assert ok is True
    assert not desktop_dest.is_symlink()
    assert not icon_dest.is_symlink()


def test_diagnostics_can_read_device_handles_enoent_and_enomedium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ok_missing, msg_missing = _can_read_device(str(tmp_path / "missing_sr99"))
    assert ok_missing is False
    assert "Unavailable" in msg_missing

    monkeypatch.setattr(
        "whipit.diagnostics.open",
        lambda *a, **kw: (_ for _ in ()).throw(OSError(123, "No medium found")),
        raising=False,
    )
    ok_empty, msg_empty = _can_read_device("/dev/sr0")
    assert ok_empty is True
    assert "No disc loaded" in msg_empty

    import grp
    monkeypatch.setattr(
        grp,
        "getgrall",
        lambda: (_ for _ in ()).throw(AssertionError("getgrall should not be called")),
    )
    ok_grp, _grp_msg = _check_user_groups()
    assert isinstance(ok_grp, bool)


def test_uninstall_script_removes_dev_pytest_symlink(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    fake_home = tmp_path / "home"
    fake_data = tmp_path / "data"
    fake_bin = fake_home / ".local" / "bin"
    venv_bin = fake_data / "whipit" / "venv" / "bin"
    fake_bin.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    (venv_bin / "pytest").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pytest_link = fake_bin / "whipit-pytest"
    pytest_link.symlink_to(venv_bin / "pytest")

    env = {
        **os.environ,
        "HOME": str(fake_home),
        "XDG_DATA_HOME": str(fake_data),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    res = subprocess.run(
        ["bash", str(repo_root / "uninstall.sh"), "--purge", "-y"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0
    assert not pytest_link.is_symlink()


def test_gui_interaction_adapter_cross_thread_prompt_and_shutdown() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.adapter import GuiInteractionAdapter

    _ = QApplication.instance() or QApplication([])
    adapter = GuiInteractionAdapter()

    def resolver(req: object) -> None:
        req.resolve(True)  # type: ignore[attr-defined]

    adapter.signaler.prompt_requested.connect(resolver)
    results: list[bool] = []

    def worker_fn() -> None:
        results.append(adapter.prompt_unknown_disc())

    t = threading.Thread(target=worker_fn)
    t.start()
    while t.is_alive():
        QApplication.processEvents()
        t.join(timeout=0.02)
    assert results == [True]

    # Shutdown unblocks waiting worker immediately with default
    adapter2 = GuiInteractionAdapter()
    unblocked: list[bool] = []
    t2 = threading.Thread(target=lambda: unblocked.append(adapter2.prompt_cdr_detected("/dev/sr0")))
    t2.start()
    while not adapter2._active_requests:
        threading.Event().wait(0.01)
    adapter2.shutdown()
    t2.join(timeout=2.0)
    assert unblocked == [False]


def test_startup_check_worker_preserves_and_cleans_jobs(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.adapter import GuiInteractionAdapter
    from whipit.ui.gui.worker import StartupCheckWorker

    _ = QApplication.instance() or QApplication([])
    store = ManifestStore()
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_AUDIO")
    store.save(sample_manifest)

    empty_job = JobManifest(
        job_id="empty-unrecoverable-job",
        state=WorkflowState.RIPPING,
        device=sample_manifest.device,
        paths=sample_manifest.paths.model_copy(
            update={
                "whipper_stage": sample_manifest.paths.whipper_stage.parent / "stage_empty-unrecoverable-job",
                "candidate_stage": temp_env["output"] / ".whipit-stage.empty-unrecoverable-job",
                "final_flac_dir": None,
                "final_opus_dir": None,
            }
        ),
    )
    store.save(empty_job)

    monkeypatch.setattr("whipit.ui.gui.worker.MediaDialogInhibitor.restore_persisted_state", lambda: False)
    monkeypatch.setattr("whipit.ui.gui.worker.check_or_install_dependencies", lambda **kw: True)
    monkeypatch.setattr("whipit.ui.gui.worker.get_missing_dependencies", list)

    emitted: list[tuple[bool, list, list, list]] = []
    worker = StartupCheckWorker(GuiInteractionAdapter(), temp_env["output"])
    worker.checks_completed.connect(lambda a, b, c, d: emitted.append((a, b, c, d)))
    worker.run()

    assert len(emitted) == 1
    all_ok, missing, preserved, _actions = emitted[0]
    assert all_ok is True
    assert missing == []
    assert [m.job_id for m, _st in preserved] == [sample_manifest.job_id]
    assert not store.manifest_path(empty_job.job_id).exists()
