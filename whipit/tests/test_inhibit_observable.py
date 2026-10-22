import json
import logging
import os
import subprocess
from pathlib import Path
import pytest
from whipit.inhibit import MediaDialogInhibitor, restore_setting
from whipit.ui.base import LogLevel, NullInteractionAdapter


def test_restore_setting_checks_returncode_and_timeout():
    assert restore_setting(["echo", "hello"], "echo test") is True
    assert restore_setting(["false"], "false test") is False
    assert restore_setting(["sleep", "1"], "sleep test", timeout=0.05) is False
    assert restore_setting(["/nonexistent/tool"], "missing test") is False


def test_inhibit_restore_unavailable_desktop_utilities_retains_state(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))

    mock_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {"org.gnome.desktop.media-handling": {"automount": "true"}},
    }
    state_file.write_text(json.dumps(mock_payload))

    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: None)

    did_restore = MediaDialogInhibitor.restore_persisted_state()
    assert did_restore is False
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert saved["xfconf"]["/automount-media/enabled"] == "true"
    assert saved["gsettings"]["org.gnome.desktop.media-handling"]["automount"] == "true"


def test_inhibit_stop_timeout_retains_failed_setting(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    inhibitor = MediaDialogInhibitor()
    inhibitor._orig_gsettings = {"org.gnome.desktop.media-handling": {"automount": "true"}}
    inhibitor._has_gsettings = True

    def timing_out_run(cmd, *a, **kw):
        raise subprocess.TimeoutExpired(cmd, 5.0)

    monkeypatch.setattr("subprocess.run", timing_out_run)

    ok = inhibitor.stop()
    assert ok is False
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert saved["gsettings"]["org.gnome.desktop.media-handling"]["automount"] == "true"


def test_inhibit_partial_restoration_retains_only_failed_settings(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    inhibitor = MediaDialogInhibitor()
    inhibitor._orig_xfconf = {"/automount-media/enabled": "true"}
    inhibitor._has_xfconf = True
    inhibitor._orig_gsettings = {"org.gnome.desktop.media-handling": {"automount": "true"}}
    inhibitor._has_gsettings = True

    def partial_run(cmd, *a, **kw):
        if "xfconf-query" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Error connecting to d-bus\n")

    monkeypatch.setattr("subprocess.run", partial_run)

    ok = inhibitor.stop()
    assert ok is False
    assert state_file.exists()
    saved = json.loads(state_file.read_text())
    assert "/automount-media/enabled" not in saved.get("xfconf", {})
    assert saved["gsettings"]["org.gnome.desktop.media-handling"]["automount"] == "true"

    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    restored = MediaDialogInhibitor.restore_persisted_state()
    assert restored is True
    assert not state_file.exists()


def test_inhibit_start_refuses_to_modify_settings_if_persist_fails(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    applied_changes = []

    def mock_run(cmd, *a, **kw):
        if "-s" in cmd or "set" in cmd:
            applied_changes.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n")

    monkeypatch.setattr("subprocess.run", mock_run)

    inhibitor = MediaDialogInhibitor()
    monkeypatch.setattr(inhibitor, "_persist_state", lambda: False)

    with pytest.raises(RuntimeError, match="Cannot safely suppress desktop automount"):
        inhibitor.start()

    assert len(applied_changes) == 0


def test_inhibit_start_persists_before_modifying_settings(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    state_existed_before_first_change = None

    def mock_run(cmd, *a, **kw):
        nonlocal state_existed_before_first_change
        if "-s" in cmd or "set" in cmd:
            if state_existed_before_first_change is None:
                state_existed_before_first_change = state_file.exists()
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n")

    monkeypatch.setattr("subprocess.run", mock_run)

    inhibitor = MediaDialogInhibitor()
    inhibitor.start()

    assert state_existed_before_first_change is True
    assert state_file.exists()


def test_failed_recovery_setting_preserved_and_merged_on_start(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    # 1. Seed state file with setting 1 (xfconf) whose original value was "true"
    seeded_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {},
    }
    state_file.write_text(json.dumps(seeded_payload))

    # 2. Make setting 1 fail on restore, but make setting 2 (gsettings) capturable
    def mock_run(cmd, *a, **kw):
        if "xfconf-query" in cmd[0]:
            if "-s" in cmd:
                # Restoration of setting 1 fails
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="xfconf d-bus failure\n")
            # Querying xfconf returns "false" on system (since previous restore failed)
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n")
        elif "gsettings" in cmd[0]:
            if "list-keys" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="automount\n")
            if "get" in cmd:
                # Setting 2 is capturable: original value is "true"
                return subprocess.CompletedProcess(cmd, 0, stdout="'true'\n")
            if "set" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", mock_run)

    # 3. Call start()
    inhibitor = MediaDialogInhibitor()
    inhibitor.start()

    # 4. Verify setting 1's original value remains recoverable and was merged with setting 2
    assert state_file.exists()
    persisted = json.loads(state_file.read_text())
    assert persisted["xfconf"]["/automount-media/enabled"] == "true"
    assert persisted["gsettings"]["org.gnome.desktop.media-handling"]["automount"] == "true"


def test_partial_restoration_performs_durable_sync(tmp_path, monkeypatch):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    mock_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {"org.gnome.desktop.media-handling": {"automount": "true"}},
    }
    state_file.write_text(json.dumps(mock_payload))

    sync_calls = []
    orig_fsync = os.fsync

    def spy_fsync(fd):
        sync_calls.append(fd)
        orig_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    # XFCE restore succeeds, GNOME fails -> triggers partial state file rewrite
    def mock_run(cmd, *a, **kw):
        if "xfconf-query" in cmd[0]:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 1, stderr="D-Bus error")

    monkeypatch.setattr("subprocess.run", mock_run)

    did_restore = MediaDialogInhibitor.restore_persisted_state()
    assert did_restore is True
    assert len(sync_calls) >= 2  # Temp file fsync + directory fsync
    assert state_file.exists()

    # Now allow all to succeed -> deletes state file and syncs directory
    sync_calls.clear()
    monkeypatch.setattr("subprocess.run", lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0))
    final_restore = MediaDialogInhibitor.restore_persisted_state()
    assert final_restore is True
    assert not state_file.exists()
    assert len(sync_calls) >= 1  # Directory fsync on unlink


def test_start_with_pending_restoration_warns_and_retries_on_stop(tmp_path, monkeypatch, caplog):
    state_file = tmp_path / "inhibit_state.json"
    monkeypatch.setattr(MediaDialogInhibitor, "state_file", staticmethod(lambda: state_file))
    monkeypatch.setattr("shutil.which", lambda cmd, *a, **kw: f"/usr/bin/{cmd}")

    seeded_payload = {
        "xfconf": {"/automount-media/enabled": "true"},
        "gsettings": {},
    }
    state_file.write_text(json.dumps(seeded_payload))

    xfconf_should_succeed = False
    ran_cmds = []

    def mock_run(cmd, *a, **kw):
        ran_cmds.append(cmd)
        if "xfconf-query" in cmd[0]:
            if "-s" in cmd:
                if xfconf_should_succeed:
                    return subprocess.CompletedProcess(cmd, 0)
                return subprocess.CompletedProcess(cmd, 1, stderr="D-Bus temporarily unavailable")
            return subprocess.CompletedProcess(cmd, 0, stdout="false\n")
        elif "gsettings" in cmd[0]:
            if "list-keys" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="automount\n")
            if "get" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="'true'\n")
            if "set" in cmd:
                return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", mock_run)

    logged_warnings = []
    class MockUI(NullInteractionAdapter):
        def on_log(self, level, msg):
            if level == LogLevel.WARN:
                logged_warnings.append(msg)

    inhibitor = MediaDialogInhibitor()
    with caplog.at_level(logging.WARNING):
        inhibitor.start(ui=MockUI())

    assert inhibitor.pending_restoration is True
    assert "/automount-media/enabled" in inhibitor.pending_restoration_details.get("xfconf", {})
    assert len(logged_warnings) >= 1
    assert "could not be fully restored" in logged_warnings[0]

    xfconf_should_succeed = True
    ok = inhibitor.stop()
    assert ok is True
    assert inhibitor.pending_restoration is False
    assert not state_file.exists()
    assert any("xfconf-query" in c and "-s" in c and "true" in c for c in ran_cmds)
