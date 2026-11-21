"""Tests for Stage 3 hardware LED pill badges and dynamic media spec strip."""

from __future__ import annotations

import pytest

from whipit.config import (
    CDS_AUDIO,
    CDS_DATA_1,
    CDS_DISC_OK,
    CDS_MIXED,
    CDS_NO_DISC,
    CDS_NO_INFO,
    UserPreferences,
)


def test_drive_panel_hardware_led_pills_reflect_hardware_state(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])

    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_drive_hardware_id", lambda dev: "ASUS_BW-16D1HT")
    monkeypatch.setattr("whipit.ui.gui.drive_panel.is_usb_drive", lambda dev: True)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_drive_max_speed", lambda dev: 24)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_whipper_defeats_cache", lambda dev: False)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_stored_drive_offset", lambda dev: 6)

    panel = DrivePanel(device="/dev/sr0")
    assert "USB" in panel.interface_badge.text()
    assert "Max 24x" in panel.speed_badge.text()
    assert "Cache: Bypass" in panel.cache_badge.text()
    assert "+6" in panel.offset_badge.text()

    # Switching to SATA + defeats_cache=True + None max speed updates pills cleanly
    monkeypatch.setattr("whipit.ui.gui.drive_panel.is_usb_drive", lambda dev: False)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_drive_max_speed", lambda dev: None)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_whipper_defeats_cache", lambda dev: True)
    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_stored_drive_offset", lambda dev: None)

    panel.set_device("/dev/sr1")
    assert "SATA" in panel.interface_badge.text()
    assert "Max --" in panel.speed_badge.text()
    assert "Cache: Defeated" in panel.cache_badge.text()
    assert "Not Configured" in panel.offset_badge.text()

    # None device resets hardware pills safely
    panel.set_device(None)
    assert "--" in panel.interface_badge.text()
    assert "--" in panel.speed_badge.text()
    assert "--" in panel.cache_badge.text()


def test_hardware_cache_pill_not_queried_on_drive_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])

    cache_calls = 0

    def spy_defeats(dev: str) -> bool:
        nonlocal cache_calls
        cache_calls += 1
        return False

    monkeypatch.setattr("whipit.ui.gui.drive_panel.get_whipper_defeats_cache", spy_defeats)
    panel = DrivePanel(device="/dev/sr0")
    initial_calls = cache_calls

    panel.handle_drive_status(CDS_NO_DISC, CDS_NO_INFO)
    assert cache_calls == initial_calls


def test_media_spec_strip_syncs_disc_type_and_opus_preference(
    monkeypatch: pytest.MonkeyPatch,
    temp_env,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])

    panel = DrivePanel(device="/dev/sr0", prefs=UserPreferences(generate_opus=True))
    monkeypatch.setattr(panel, "identify_disc", lambda silent=True: None)

    assert "Standby" in panel.media_spec_lbl.text()
    assert "FLAC + Opus 192k" in panel.media_spec_lbl.text()

    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert "Red Book CDDA" in panel.media_spec_lbl.text()
    assert "16-bit / 44.1 kHz" in panel.media_spec_lbl.text()
    assert "FLAC + Opus 192k" in panel.media_spec_lbl.text()

    panel.apply_preferences(UserPreferences(generate_opus=False))
    assert "Red Book CDDA" in panel.media_spec_lbl.text()
    assert "FLAC Lossless" in panel.media_spec_lbl.text()

    panel.handle_drive_status(CDS_DISC_OK, CDS_MIXED)
    assert "Enhanced CD" in panel.media_spec_lbl.text()
    assert "FLAC Lossless" in panel.media_spec_lbl.text()

    panel.handle_drive_status(CDS_DISC_OK, CDS_DATA_1)
    assert "Data / Non-Audio Disc" in panel.media_spec_lbl.text()


def test_uncataloged_badge_visibility_and_custom_metadata_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.models import TrackInfo
    from whipit.ripping.identify import DiscInspectionResult
    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0")
    assert panel.uncataloged_badge.isHidden() is True

    uncat = DiscInspectionResult(
        success=False,
        tracks_count=4,
        mb_lookup_url="https://musicbrainz.org/cdtoc/attach?toc=1+4+100000",
        error_message="Disc detected, but not catalogued in MusicBrainz database.",
    )
    panel._on_identify_finished(uncat, silent=True)
    assert panel.uncataloged_badge.isHidden() is False
    assert "Uncataloged Disc" in panel.uncataloged_badge.text()
    assert panel.unknown_chk.isChecked() is True

    monkeypatch.setattr(
        "whipit.ui.gui.main_window.prompt_edit_metadata_dialog",
        lambda parent, artist, album, year, genre, disc_number, total_discs, tracks: (
            "Local Band",
            "Demo Tape",
            "2026",
            "Rock",
            None,
            None,
            [TrackInfo(number=1, title="Intro")],
        ),
    )
    panel._open_edit_metadata_dialog()
    assert panel.uncataloged_badge.isHidden() is False
    assert "Custom Metadata" in panel.uncataloged_badge.text()

    cat = DiscInspectionResult(
        success=True,
        artist="Radiohead",
        title="OK Computer",
        mbid="12345678-1234-1234-1234-123456789abc",
        mb_url="https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc",
    )
    panel._on_identify_finished(cat, silent=True)
    assert panel.uncataloged_badge.isHidden() is True

    panel._on_identify_finished(uncat, silent=True)
    assert panel.uncataloged_badge.isHidden() is False
    panel.eject_drive()
    assert panel.uncataloged_badge.isHidden() is True
