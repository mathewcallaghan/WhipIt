import json
import os
import stat
import time
import urllib.error
from pathlib import Path
import pytest
from whipit.audio.metadata import ReleaseDetails
from whipit.bootstrap import (
    OPTIONAL_TOOLS,
    REQUIRED_TOOLS,
    UNMANAGED_PACKAGES,
    detect_package_manager,
    install_desktop_launcher,
)
from whipit.config import (
    _is_ripper_holding_device,
    drive_has_disc,
    get_disc_status,
    CDS_AUDIO,
    CDS_DATA_1,
    CDS_DISC_OK,
    CDS_MIXED,
)
from whipit.inhibit import MediaDialogInhibitor
from whipit.models import ReleaseOption
from whipit.ripping.identify import inspect_disc
from whipit.ripping.whipper import WhipperRunner


def test_inhibit_state_crash_recovery(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))

    mock_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {"org.gnome.desktop.media-handling": {"automount": "true"}},
    }
    state_file.write_text(json.dumps(mock_payload))

    restored_cmds = []
    monkeypatch.setattr("shutil.which", lambda cmd: "/bin/" + cmd)
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: restored_cmds.append(cmd))

    did_restore = MediaDialogInhibitor.restore_persisted_state()
    assert did_restore is True
    assert not state_file.exists()
    assert any("xfconf-query" in c and "true" in c for c in restored_cmds)
    assert any("gsettings" in c and "true" in c for c in restored_cmds)


def test_get_configured_repo_from_git_remote(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import get_configured_repo

    fake_git = tmp_path / "repo"
    fake_git.mkdir()
    (fake_git / ".git").mkdir()

    class MockRun:
        returncode = 0
        stdout = "https://github.com/mathewcallaghan/WhipIt.git\n"

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MockRun())

    repo = get_configured_repo(source_dir=fake_git)
    assert repo == "mathewcallaghan/WhipIt"


def test_get_configured_repo_rejects_unauthorized_repositories(temp_env, monkeypatch):
    from whipit.ui.gui.updater import get_configured_repo

    monkeypatch.setenv("WHIPIT_GITHUB_REPO", "attacker/WhipIt")
    assert get_configured_repo() is None

    monkeypatch.setenv("WHIPIT_GITHUB_REPO", "https://evil.com/owner/repo")
    assert get_configured_repo() is None

    monkeypatch.setenv("WHIPIT_GITHUB_REPO", "../repo")
    assert get_configured_repo() is None


def test_recorded_source_dir_requires_same_uid(temp_env, tmp_path, monkeypatch):
    from whipit.ui.gui.updater import _recorded_source_dir

    cfg = temp_env["config"] / "whipit" / "config"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"source_dir={repo_dir}\n")
    assert _recorded_source_dir() == repo_dir

    fake_stat = type("Stat", (), {"st_uid": os.getuid() + 1000})()
    monkeypatch.setattr(Path, "stat", lambda self: fake_stat if self == repo_dir else os.stat(self))
    assert _recorded_source_dir() is None


def test_update_via_git_pip_failure_reports_error(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        class Res:
            returncode = 0
            stdout = ""
            stderr = ""

        if "tag" in cmd and "--list" in cmd:
            Res.stdout = "v99.0.0\n"
            Res.returncode = 0
        elif "cat-file" in cmd:
            Res.stdout = b"object deadbeef\ntype commit\ntag v99.0.0\n\nRelease\n-----BEGIN PGP SIGNATURE-----\nmock_sig\n"
            Res.returncode = 0
        elif "rev-parse" in cmd:
            Res.stdout = "deadbeef1234567890\n"
            Res.returncode = 0
        elif "merge" in cmd:
            Res.returncode = 0
        elif "pip" in cmd:
            Res.returncode = 1
            Res.stderr = "ERROR: Failed building wheel for whipit\n"
        return Res()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda p, s: True)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, restart = emitted[0]
    assert success is False
    assert "Package installation failed" in msg
    assert restart is False


def test_update_via_git_uses_require_hashes_when_lockfile_present(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    (tmp_path / "requirements.lock").write_text("whipit==99.0.0 --hash=sha256:dummy\n")

    pip_commands = []
    def mock_run(cmd, *a, **kw):
        class Res:
            returncode = 0
            stdout = ""
            stderr = ""

        if "tag" in cmd and "--list" in cmd:
            Res.stdout = "v99.0.0\n"
        elif "cat-file" in cmd:
            Res.stdout = b"object deadbeef\ntype commit\ntag v99.0.0\n\nRelease\n-----BEGIN PGP SIGNATURE-----\nmock_sig\n"
        elif "rev-parse" in cmd:
            Res.stdout = "deadbeef1234567890\n"
        elif "pip" in cmd:
            pip_commands.append(list(cmd))
        return Res()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda p, s: True)
    monkeypatch.setattr("whipit.ui.gui.updater.install_launcher", lambda: None)

    worker._update_via_git(tmp_path)
    assert len(pip_commands) == 2
    assert "--require-hashes" in pip_commands[0]
    assert "--no-deps" in pip_commands[1]
    assert "--no-build-isolation" in pip_commands[1]


def test_update_via_git_rejects_invalid_tag_format(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    def mock_run(cmd, *a, **kw):
        class Res:
            returncode = 0
            stdout = ""
            stderr = ""

        if "--list" in cmd:
            Res.stdout = "invalid_format_tag\n"
        return Res()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/git" if cmd == "git" else None)
    monkeypatch.setattr("subprocess.run", mock_run)

    worker._update_via_git(tmp_path)
    assert len(emitted) == 1
    success, msg, restart = emitted[0]
    assert success is False
    assert "unexpected format" in msg.lower()


def test_update_via_github_release_refuses_when_no_tagged_release(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    class Mock404Error(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("https://api.github.com/...", 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: (_ for _ in ()).throw(Mock404Error()))

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "tagged releases" in msg.lower() or "no published releases" in msg.lower()


def test_update_via_github_release_rejects_invalid_tag_format(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "../../bad-tag",
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return json.dumps(mock_release).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: MockResponse())

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "unexpected format" in msg.lower()


def test_parse_version_tuple_rejects_prereleases():
    from whipit.ui.gui.updater import parse_version_tuple
    import pytest

    assert parse_version_tuple("2.4.0") == (2, 4, 0)
    assert parse_version_tuple("v2.4.0") == (2, 4, 0)
    assert parse_version_tuple("2.4") == (2, 4)

    for invalid in ("2.4.0-rc1", "2.4.0-beta2", "2.4.0.dev1", "v2.4.0-alpha", "bad"):
        with pytest.raises(ValueError, match="Unsupported version string"):
            parse_version_tuple(invalid)


def test_update_via_github_release_rejects_prerelease_tag(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0-rc1",
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return json.dumps(mock_release).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: MockResponse())

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "unexpected format" in msg.lower()


def test_update_via_github_release_refuses_unsigned_wheel(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-99.0.0-py3-none-any.whl", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl"}
        ],
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return json.dumps(mock_release).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: MockResponse())

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "no signed wheel" in msg.lower()


def test_update_via_github_release_aborts_on_signature_verification_failure(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-99.0.0-py3-none-any.whl", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl"},
            {"name": "whipit-99.0.0-py3-none-any.whl.sig", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl.sig"},
        ],
    }

    class MockResponse:
        status = 200
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return self.payload

    def mock_urlopen(req, timeout=12.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "releases/latest" in url:
            return MockResponse(json.dumps(mock_release).encode("utf-8"))
        return MockResponse(b"MOCK_DATA")

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda pkg, sig: False)

    pip_called = []
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **kw: pip_called.append(cmd))

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "signature verification failed" in msg.lower()
    assert len(pip_called) == 0


def test_update_via_github_release_refuses_untrusted_host(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-99.0.0-py3-none-any.whl", "browser_download_url": "https://malicious.example.com/wheel.whl"},
            {"name": "whipit-99.0.0-py3-none-any.whl.sig", "browser_download_url": "https://malicious.example.com/wheel.whl.sig"},
        ],
    }

    class MockResponse:
        status = 200
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return self.payload

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=12.0: MockResponse(json.dumps(mock_release).encode("utf-8")),
    )

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "host not allowed" in msg.lower() or "untrusted" in msg.lower()


def test_update_via_github_release_installs_signed_wheel_with_no_deps(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-99.0.0-py3-none-any.whl", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl"},
            {"name": "whipit-99.0.0-py3-none-any.whl.sig", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl.sig"},
        ],
    }

    class MockResponse:
        status = 200
        returncode = 0
        def __init__(self, payload):
            self.payload = payload
            self.stdout = ""
            self.stderr = ""
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return self.payload

    import io
    import zipfile

    wheel_buf = io.BytesIO()
    with zipfile.ZipFile(wheel_buf, "w") as zf:
        zf.writestr("whipit-99.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: whipit\nVersion: 99.0.0\n")
    mock_wheel_bytes = wheel_buf.getvalue()

    def mock_urlopen(req, timeout=12.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "releases/latest" in url:
            return MockResponse(json.dumps(mock_release).encode("utf-8"))
        if url.endswith(".sig"):
            return MockResponse(b"SIGNATURE_DATA")
        return MockResponse(mock_wheel_bytes)

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda pkg, sig: True)

    pip_called = []
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **kw: (pip_called.append(cmd), MockResponse(b""))[1])

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, restart = emitted[0]
    assert success is True
    assert restart is True
    assert len(pip_called) >= 1
    pip_cmd = pip_called[0]
    assert "--only-binary=:all:" in pip_cmd
    assert "--no-deps" in pip_cmd


def test_update_via_github_release_rejects_mismatched_wheel_filename(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-1.0.0-py3-none-any.whl", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl"},
            {"name": "whipit-1.0.0-py3-none-any.whl.sig", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl.sig"},
        ],
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return json.dumps(mock_release).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: MockResponse())

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "Wheel version does not match release tag" in msg


def test_update_via_github_release_rejects_replayed_old_wheel_metadata(monkeypatch):
    pytest.importorskip("PyQt6")
    from whipit.ui.gui.updater import UpdateWorker
    import io
    import zipfile

    worker = UpdateWorker()
    emitted = []
    worker.update_finished.connect(lambda s, m, r: emitted.append((s, m, r)))

    mock_release = {
        "tag_name": "v99.0.0",
        "assets": [
            {"name": "whipit-99.0.0-py3-none-any.whl", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl"},
            {"name": "whipit-99.0.0-py3-none-any.whl.sig", "browser_download_url": "https://objects.githubusercontent.com/wheel.whl.sig"},
        ],
    }

    # Create replayed wheel: filename says 99.0.0, but METADATA is actually 1.0.0
    replayed_buf = io.BytesIO()
    with zipfile.ZipFile(replayed_buf, "w") as zf:
        zf.writestr("whipit-99.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: whipit\nVersion: 1.0.0\n")
    replayed_bytes = replayed_buf.getvalue()

    class MockResponse:
        status = 200
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, *a):
            return self.payload

    def mock_urlopen(req, timeout=12.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "releases/latest" in url:
            return MockResponse(json.dumps(mock_release).encode("utf-8"))
        return MockResponse(replayed_bytes)

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("whipit.ui.gui.updater._verify_signature", lambda pkg, sig: True)

    worker._update_via_github_release("mathewcallaghan/WhipIt")
    assert len(emitted) == 1
    success, msg, _ = emitted[0]
    assert success is False
    assert "Signed wheel version mismatch" in msg


def test_unmount_cdda_device_exact_pkill(monkeypatch):
    from whipit.inhibit import unmount_cdda_device

    cmds = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: cmds.append(cmd))
    monkeypatch.setattr("whipit.inhibit.find_optical_drives", lambda: [])

    unmount_cdda_device()
    assert len(cmds) > 0
    assert cmds[0] == ["pkill", "-x", "thunar-volman"]


def test_check_or_install_dependencies_runs_direct_argv(monkeypatch):
    from whipit.bootstrap import check_or_install_dependencies
    from whipit.ui.base import NullInteractionAdapter

    class ConfirmingAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True
        def confirm_install_dependencies(self, missing_tools, install_cmd, manager) -> bool:
            return True

    executed_argvs = []

    class MockProc:
        returncode = 0
        stdout = []
        def wait(self):
            return 0

    call_count = 0
    def mock_missing():
        nonlocal call_count
        call_count += 1
        return ["flac"] if call_count <= 1 else []

    monkeypatch.setattr("whipit.bootstrap.get_missing_dependencies", mock_missing)
    monkeypatch.setattr("whipit.bootstrap.get_missing_optional_dependencies", lambda: [])
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: "/usr/bin/apt-get" if cmd == "apt-get" else ("/usr/bin/sudo" if cmd == "sudo" else None))
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: (executed_argvs.append(argv), MockProc())[1])

    ok = check_or_install_dependencies(interactive=True, ui=ConfirmingAdapter())
    assert ok is True
    assert len(executed_argvs) == 2
    assert executed_argvs[0] == ["/usr/bin/sudo", "/usr/bin/apt-get", "update"]
    assert executed_argvs[1][:4] == ["/usr/bin/sudo", "/usr/bin/apt-get", "install", "-y"]
    assert "flac" in executed_argvs[1]
    assert "sh" not in executed_argvs[0]
    assert "-c" not in executed_argvs[0]


def test_check_or_install_dependencies_rejects_malformed_package_names(monkeypatch):
    from whipit.bootstrap import check_or_install_dependencies
    from whipit.ui.base import NullInteractionAdapter

    monkeypatch.setattr("whipit.bootstrap.get_missing_dependencies", lambda: ["flac;rm -rf /"])
    monkeypatch.setattr("whipit.bootstrap.get_missing_optional_dependencies", lambda: [])
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: "/usr/bin/apt-get" if cmd == "apt-get" else ("/usr/bin/sudo" if cmd == "sudo" else None))

    ok = check_or_install_dependencies(interactive=True, ui=NullInteractionAdapter())
    assert ok is False


def test_send_notification_uses_dash_dash_and_html_escapes(monkeypatch):
    from whipit.notifications import send_notification

    captured = []
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None)
    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: (captured.append(cmd), None)[1])

    send_notification("-DANGEROUS-SUMMARY-", "<b>Track 1 & Track 2</b> <script>alert(1)</script>")

    assert len(captured) == 1
    argv = captured[0]
    assert "--" in argv
    dash_idx = argv.index("--")
    assert argv[dash_idx + 1] == "-DANGEROUS-SUMMARY-"
    assert "&lt;b&gt;" in argv[dash_idx + 2]
    assert "&lt;script&gt;" in argv[dash_idx + 2]


def test_private_directories_have_0700_permissions(temp_env):
    from whipit.config import get_cache_dir, get_config_dir, get_log_dir

    for fn in (get_log_dir, get_cache_dir, get_config_dir):
        d = fn()
        mode = stat.S_IMODE(d.stat().st_mode)
        assert mode == 0o700


def test_saved_manifest_has_0600_permissions(sample_manifest, temp_env):
    from whipit.state_store import ManifestStore

    store = ManifestStore()
    store.save(sample_manifest)
    manifest_file = store.manifest_path(sample_manifest.job_id)
    assert manifest_file.exists()
    mode = stat.S_IMODE(manifest_file.stat().st_mode)
    assert mode == 0o600


def test_whipper_runner_speed_wrapper_hardening(tmp_path, monkeypatch):
    import stat
    from whipit.ripping.whipper import WhipperRunner

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/cd-paranoia" if "paranoia" in cmd else None)

    runner = WhipperRunner(tmp_path, target_device="/dev/sr0", speed=4)
    wrap_dir = runner._setup_speed_wrapper()

    assert wrap_dir.exists()
    assert wrap_dir.name.startswith("wrap_")
    assert stat.S_IMODE(wrap_dir.stat().st_mode) == 0o700

    cd_paranoia = wrap_dir / "cd-paranoia"
    cdparanoia = wrap_dir / "cdparanoia"
    assert cd_paranoia.exists()
    assert cdparanoia.exists()
    assert stat.S_IMODE(cd_paranoia.stat().st_mode) == 0o700
    assert stat.S_IMODE(cdparanoia.stat().st_mode) == 0o700

    content = cd_paranoia.read_text()
    assert "-S 4" in content
    assert "exec /usr/bin/cd-paranoia" in content

    runner.cleanup_wrapper()
    assert not wrap_dir.exists()


def test_required_tools_specification():
    assert "diff" not in REQUIRED_TOOLS
    assert "fuser" not in REQUIRED_TOOLS
    assert "notify-send" not in REQUIRED_TOOLS
    assert "notify-send" in OPTIONAL_TOOLS
    assert "pkill" in REQUIRED_TOOLS
    assert "udisksctl" in REQUIRED_TOOLS
    assert "gio" in REQUIRED_TOOLS


def test_package_manager_maps(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: "/usr/bin/apt-get" if cmd == "apt-get" else None)
    info = detect_package_manager()
    assert info is not None
    assert info["manager"] == "apt-get"
    pkg_map = info["pkg_map"]
    assert pkg_map["cd-paranoia"] == "libcdio-utils"
    assert pkg_map["pkill"] == "procps"
    assert pkg_map["udisksctl"] == "udisks2"
    assert pkg_map["gio"] == "libglib2.0-bin"


def test_trusted_path_resolution(monkeypatch):
    from whipit.bootstrap import _trusted, _SAFE_PATH

    queried_paths = []
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: (queried_paths.append(path), f"/usr/bin/{cmd}")[1])
    res = _trusted("apt-get")
    assert res == "/usr/bin/apt-get"
    assert queried_paths == [_SAFE_PATH]


def test_check_or_install_dependencies_refuses_untrusted_priv_or_manager(monkeypatch):
    from whipit.bootstrap import check_or_install_dependencies
    from whipit.ui.base import NullInteractionAdapter

    monkeypatch.setattr("whipit.bootstrap.get_missing_dependencies", lambda: ["flac"])
    monkeypatch.setattr("whipit.bootstrap.get_missing_optional_dependencies", lambda: [])
    monkeypatch.setattr("whipit.bootstrap._trusted", lambda name: None)

    ok = check_or_install_dependencies(interactive=True, ui=NullInteractionAdapter())
    assert ok is False


def test_unmanaged_packages_filtered_for_pacman_and_dnf():
    assert "loudgain" in UNMANAGED_PACKAGES["pacman"]
    assert "loudgain" in UNMANAGED_PACKAGES["dnf"]


def test_ripper_locking_filters_media_players(tmp_path):
    proc_root = tmp_path / "proc"
    fake_proc = proc_root / "12345"
    fake_proc.mkdir(parents=True)
    (fake_proc / "comm").write_text("vlc\n")
    (fake_proc / "cmdline").write_bytes(b"vlc\x00cdda:///dev/sr0\x00")

    target_devs = {"/dev/sr0"}
    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}

    is_target = _is_ripper_holding_device(12345, target_devs, target_binaries, proc_root=proc_root)
    assert is_target is False


def test_ripper_locking_exact_device_matching(tmp_path):
    proc_root = tmp_path / "proc"
    fake_proc = proc_root / "9999"
    fake_proc.mkdir(parents=True)
    (fake_proc / "comm").write_text("cd-paranoia\n")
    (fake_proc / "cmdline").write_bytes(b"cd-paranoia\x00-d\x00/dev/sr10\x00-S\x004\x00")

    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}

    assert _is_ripper_holding_device(9999, {"/dev/sr1"}, target_binaries, proc_root=proc_root) is False
    assert _is_ripper_holding_device(9999, {"/dev/sr10"}, target_binaries, proc_root=proc_root) is True


def test_ripper_locking_detects_python_whipper(tmp_path):
    proc_root = tmp_path / "proc"
    target_binaries = {"cd-paranoia", "cdparanoia", "cdrdao", "whipper"}
    target_devs = {"/dev/sr0"}

    fake_whipper = proc_root / "2001"
    fake_whipper.mkdir(parents=True)
    (fake_whipper / "comm").write_text("python3\n")
    (fake_whipper / "cmdline").write_bytes(b"python3\x00/usr/bin/whipper\x00cd\x00-d\x00/dev/sr0\x00rip\x00")

    assert _is_ripper_holding_device(2001, target_devs, target_binaries, proc_root=proc_root) is True


def test_release_drive_locks_avoids_killing_recycled_pid(tmp_path, monkeypatch):
    from whipit.config import release_drive_locks
    import signal

    proc_root = tmp_path / "proc"
    target_pid = 3001
    proc_dir = proc_root / str(target_pid)
    proc_dir.mkdir(parents=True)
    (proc_dir / "comm").write_text("cd-paranoia\n")
    (proc_dir / "cmdline").write_bytes(b"cd-paranoia\x00-d\x00/dev/sr0\x00")
    fake_stat = f"{target_pid} (cd-paranoia) R " + " ".join(["0"] * 18) + " 54321 " + " ".join(["0"] * 20)
    (proc_dir / "stat").write_text(fake_stat)

    killed_signals = []
    def mock_kill(pid, sig):
        killed_signals.append((pid, sig))
        if sig == signal.SIGTERM:
            recycled_stat = f"{target_pid} (unrelated) S " + " ".join(["0"] * 18) + " 99999 " + " ".join(["0"] * 20)
            (proc_dir / "stat").write_text(recycled_stat)

    monkeypatch.setattr("os.kill", mock_kill)
    monkeypatch.setattr("whipit.config.unlock_drive", lambda dev: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    release_drive_locks("/dev/sr0", proc_root=proc_root)
    assert (target_pid, signal.SIGTERM) in killed_signals
    assert (target_pid, signal.SIGKILL) not in killed_signals


def test_inspect_disc_timeout_enforced(monkeypatch):
    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)

    class BlockingStdout:
        def readline(self):
            time.sleep(10.0)
            return ""
        def close(self):
            pass

    class HangingProcess:
        def __init__(self, *args, **kwargs):
            self.args = args[0] if args else kwargs.get("args", ["whipper"])
            self.stdin = None
            self.stderr = None
            self.stdout = BlockingStdout()
            self.returncode = None
            self.pid = 99999

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            return ("", "")

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("subprocess.Popen", HangingProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: pid)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)

    t0 = time.time()
    res = inspect_disc("/dev/sr0", timeout=0.15)
    elapsed = time.time() - t0
    assert res.success is False
    assert "Timeout" in (res.error_message or "")
    assert elapsed < 1.0


def test_inspect_disc_does_not_abort_on_early_reading_table_line(monkeypatch):
    whipper_sample_output = """
INFO:whipper.command.cd:checking device /dev/sr0
INFO:whipper.command.cd:Reading table of contents...
CDDB disc id: a10a240b
MusicBrainz disc id 9V8z1c
MusicBrainz lookup URL https://musicbrainz.org/cdtoc/attach?toc=1+11
Disc duration: 00:43:20.000, 11 audio tracks
Matching releases:
Artist: Pink Floyd
Title: The Dark Side of the Moon
Release: https://musicbrainz.org/release/a0000000-0000-0000-0000-000000000001
"""
    class MockInspectProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdout = io.StringIO(whipper_sample_output.strip())
            self.returncode = 0
            self.pid = 4321
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("subprocess.Popen", MockInspectProc)
    from whipit.audio.metadata import ReleaseDetails
    monkeypatch.setattr("whipit.audio.metadata.fetch_release_details", lambda *a, **kw: ReleaseDetails(discogs_url="https://www.discogs.com/release/123"))

    res = inspect_disc("/dev/sr0")
    assert res.success is True
    assert res.artist == "Pink Floyd"
    assert res.title == "The Dark Side of the Moon"
    assert len(res.matching_releases) == 1


def test_whipper_runner_filters_progress_tick_spam(tmp_path, monkeypatch):
    raw_stream = ["INFO: Ripping track 1\n", "##: 0 - - - 0 @ 0000\n", "##: 0 - - - 0 @ 0075\n", "Status: Copy OK\n"]
    filtered = [l for l in raw_stream if not l.startswith("##:")]
    assert len(filtered) == 2
    assert "INFO: Ripping track 1\n" in filtered
    assert "Status: Copy OK\n" in filtered


def test_drive_has_disc_distinguishes_audio_vs_data(monkeypatch):
    monkeypatch.setattr("os.open", lambda *a, **kw: 99)
    monkeypatch.setattr("os.close", lambda fd: None)

    # 1. Red Book Audio CD: Drive OK (4), Disc Audio (100)
    def mock_audio_ioctl(fd, request):
        return CDS_DISC_OK if request == 0x5326 else CDS_AUDIO

    monkeypatch.setattr("fcntl.ioctl", mock_audio_ioctl)
    drive_st, disc_st = get_disc_status("/dev/sr0")
    assert drive_st == CDS_DISC_OK
    assert disc_st == CDS_AUDIO
    assert drive_has_disc("/dev/sr0") is True

    # 2. Enhanced/Mixed CD (Audio + Data tracks): Drive OK (4), Disc Mixed (105)
    def mock_mixed_ioctl(fd, request):
        return CDS_DISC_OK if request == 0x5326 else CDS_MIXED

    monkeypatch.setattr("fcntl.ioctl", mock_mixed_ioctl)
    assert drive_has_disc("/dev/sr0") is True

    # 3. Data CD or DVD: Drive OK (4), Disc Data (101)
    def mock_data_ioctl(fd, request):
        return CDS_DISC_OK if request == 0x5326 else CDS_DATA_1

    monkeypatch.setattr("fcntl.ioctl", mock_data_ioctl)
    # audio_only=True must reject data disc
    assert drive_has_disc("/dev/sr0", audio_only=True) is False
    # audio_only=False detects physical disc present
    assert drive_has_disc("/dev/sr0", audio_only=False) is True


def test_install_launcher_rejects_unsafe_exec_characters(tmp_path, monkeypatch):
    from whipit.launcher import install_launcher

    monkeypatch.setattr("whipit.launcher.get_applications_dir", lambda: tmp_path)
    monkeypatch.setattr("whipit.launcher.get_icon_path", lambda: tmp_path / "whipit.svg")
    monkeypatch.setattr("whipit.launcher.refresh_desktop_database", lambda: None)

    for bad_char in ['"', "`", "$", "\\", "%", "\n"]:
        with pytest.raises(ValueError, match="unsafe for a .desktop Exec line"):
            install_launcher(custom_exec=f"/usr/bin/whipit{bad_char}")


def test_resolve_executable_path_does_not_prioritize_path_over_venv(tmp_path, monkeypatch):
    import sys
    from whipit.launcher import resolve_executable_path

    fake_venv_bin = tmp_path / "bin" / "whipit"
    fake_venv_bin.parent.mkdir(parents=True)
    fake_venv_bin.touch()

    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/malicious-whipit")

    assert resolve_executable_path() == str(fake_venv_bin.resolve())


def test_install_desktop_launcher_handles_unsafe_characters_gracefully(monkeypatch):
    from whipit.bootstrap import install_desktop_launcher
    monkeypatch.setattr("whipit.bootstrap.install_launcher", lambda: (_ for _ in ()).throw(ValueError("Unsafe characters")))
    assert install_desktop_launcher() is False
