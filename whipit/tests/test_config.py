import json
import os
import stat
from pathlib import Path
import pytest
from pydantic import ValidationError

from whipit.config import (
    UserPreferences,
    default_flac_root,
    default_opus_root,
    get_settings_file,
    load_preferences,
    save_preferences,
)


def test_default_preferences_with_env_override(temp_env):
    prefs = UserPreferences()
    expected_out = temp_env["output"].resolve()
    assert prefs.flac_root == expected_out
    assert prefs.opus_root == expected_out
    assert prefs.generate_opus is True
    assert prefs.default_speed is None
    assert prefs.auto_eject is True


def test_default_preferences_standard(monkeypatch):
    monkeypatch.delenv("WHIPIT_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("XDG_MUSIC_DIR", raising=False)

    flac_def = default_flac_root()
    opus_def = default_opus_root()

    assert flac_def == (Path.home() / "Music" / "FLAC" / "CD").resolve()
    assert opus_def == (Path.home() / "Music" / "Opus" / "CD").resolve()

    prefs = UserPreferences()
    assert prefs.flac_root == flac_def
    assert prefs.opus_root == opus_def


def test_user_preferences_speed_validation():
    assert UserPreferences(default_speed=4).default_speed == 4
    assert UserPreferences(default_speed=None).default_speed is None

    with pytest.raises(ValidationError, match="default_speed must be between 1 and 100"):
        UserPreferences(default_speed=0)

    with pytest.raises(ValidationError, match="default_speed must be between 1 and 100"):
        UserPreferences(default_speed=101)


def test_user_preferences_offset_validation():
    assert UserPreferences(offset_override=6).offset_override == 6
    assert UserPreferences(offset_override=-12).offset_override == -12
    assert UserPreferences(offset_override=0).offset_override == 0
    assert UserPreferences(offset_override=None).offset_override is None

    with pytest.raises(ValidationError, match="offset_override must be between -10000 and 10000"):
        UserPreferences(offset_override=-10001)

    with pytest.raises(ValidationError, match="offset_override must be between -10000 and 10000"):
        UserPreferences(offset_override=10001)


def test_user_preferences_path_traversal_rejected():
    with pytest.raises(ValidationError, match="Path traversal detected"):
        UserPreferences(flac_root=Path("/tmp/../etc/passwd"))


def test_user_preferences_expands_tilde():
    prefs = UserPreferences(flac_root="~/ArchivalMusic/FLAC")
    assert prefs.flac_root == (Path.home() / "ArchivalMusic" / "FLAC").resolve()


def test_get_settings_file_location(temp_env):
    sf = get_settings_file()
    assert sf == temp_env["config"] / "whipit" / "settings.json"


def test_load_preferences_missing_file_returns_defaults(temp_env):
    sf = get_settings_file()
    assert not sf.exists()

    prefs = load_preferences()
    assert isinstance(prefs, UserPreferences)
    assert prefs.flac_root == temp_env["output"].resolve()
    assert prefs.auto_eject is True


def test_save_and_load_preferences_roundtrip(temp_env):
    custom_flac = (temp_env["root"] / "CustomFLAC").resolve()
    custom_opus = (temp_env["root"] / "CustomOpus").resolve()

    prefs = UserPreferences(
        flac_root=custom_flac,
        opus_root=custom_opus,
        generate_opus=False,
        default_speed=8,
        auto_eject=False,
    )

    ok = save_preferences(prefs)
    assert ok is True

    sf = get_settings_file()
    assert sf.exists()
    assert stat.S_IMODE(sf.stat().st_mode) == 0o600

    loaded = load_preferences()
    assert loaded.flac_root == custom_flac
    assert loaded.opus_root == custom_opus
    assert loaded.generate_opus is False
    assert loaded.default_speed == 8
    assert loaded.auto_eject is False


def test_load_preferences_partial_json(temp_env):
    sf = get_settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"generate_opus": False, "auto_eject": False}), encoding="utf-8")

    loaded = load_preferences()
    assert loaded.generate_opus is False
    assert loaded.auto_eject is False
    # Missing fields take default values
    assert loaded.flac_root == temp_env["output"].resolve()
    assert loaded.default_speed is None


def test_load_preferences_corrupt_json_creates_backup_and_returns_defaults(temp_env):
    sf = get_settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text("{ corrupt json payload", encoding="utf-8")

    loaded = load_preferences()
    assert isinstance(loaded, UserPreferences)
    assert loaded.flac_root == temp_env["output"].resolve()

    bad_file = sf.with_suffix(".json.bad")
    assert bad_file.exists()
    assert bad_file.read_text(encoding="utf-8") == "{ corrupt json payload"


def test_save_preferences_io_error_handled(temp_env, monkeypatch):
    def failing_open(*args, **kwargs):
        raise OSError("Permission denied / disk failure")

    monkeypatch.setattr("os.open", failing_open)
    prefs = UserPreferences()
    ok = save_preferences(prefs)
    assert ok is False
