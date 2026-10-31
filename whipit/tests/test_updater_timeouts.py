import subprocess
from pathlib import Path
import pytest
from whipit.ui.gui.updater import UpdateWorker, run_git


def test_run_git_passes_options_and_enforces_timeout(tmp_path, monkeypatch):
    called = []

    def mock_run(cmd, **kwargs):
        called.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", mock_run)
    res = run_git(["status"], tmp_path, timeout=12.5)

    assert res.returncode == 0
    assert len(called) == 1
    cmd, kwargs = called[0]
    assert "-C" in cmd
    assert str(tmp_path) in cmd
    assert "status" in cmd
    assert kwargs.get("timeout") == 12.5


def test_update_via_git_handles_fetch_timeout(tmp_path, monkeypatch):
    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd, 30.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "timed out while fetching" in msg


def test_update_via_git_handles_tag_timeout(tmp_path, monkeypatch):
    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        if "tag" in cmd and "--list" in cmd:
            raise subprocess.TimeoutExpired(cmd, 15.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "timed out while checking release tags" in msg


def test_update_via_git_handles_pip_timeout(tmp_path, monkeypatch):
    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        if "tag" in cmd and "--list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v99.0.0\n", stderr="")
        elif "cat-file" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"object deadbeef\ntype commit\ntag v99.0.0\n\nRelease\n-----BEGIN PGP SIGNATURE-----\nmock\n",
                stderr="",
            )
        elif "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef1234\n", stderr="")
        elif "merge" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        elif "pip" in cmd:
            raise subprocess.TimeoutExpired(cmd, 90.0)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda p, s: True)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "Timed out installing updated package" in msg


def test_update_via_git_fails_if_import_check_fails(tmp_path, monkeypatch):
    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        if "tag" in cmd and "--list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v99.0.0\n", stderr="")
        elif "cat-file" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"object deadbeef\ntype commit\ntag v99.0.0\n\nRelease\n-----BEGIN PGP SIGNATURE-----\nmock\n",
                stderr="",
            )
        elif "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef1234\n", stderr="")
        elif "merge" in cmd or "pip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        elif any("import whipit" in str(arg) for arg in cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ModuleNotFoundError: No module named 'whipit'\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda p, s: True)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "could not import whipit" in msg


def test_update_via_git_fails_if_tag_signature_verification_fails(tmp_path, monkeypatch):
    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        if "tag" in cmd and "--list" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="v99.0.0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_tag", lambda s, t: False)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, restart = emitted[0]
    assert success is False
    assert "Tag signature verification failed" in msg
    assert restart is False
