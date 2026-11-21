"""Tests for copy-protection heuristic detection and safe extraction preset."""

from __future__ import annotations

import json

import pytest

from whipit.config import CDS_AUDIO, CDS_DISC_OK, CDS_MIXED, UserPreferences
from whipit.metadata.protection import is_copy_protected_disc
from whipit.models import ReleaseOption, TrackInfo
from whipit.ripping.identify import DiscInspectionResult, inspect_disc


def test_is_copy_protected_disc_keywords() -> None:
    assert is_copy_protected_disc(release_title="Artist — Album (Copy Control)") is True
    assert is_copy_protected_disc(disambiguation="CDS200, Copy Control") is True
    assert is_copy_protected_disc(disambiguation="Cactus Data Shield") is True
    assert is_copy_protected_disc(disambiguation="Key2Audio") is True
    assert is_copy_protected_disc(disambiguation="MediaMax CD-3") is True
    assert is_copy_protected_disc(release_title="Pink Floyd — The Wall", disambiguation="1994 Remaster") is False


def test_is_copy_protected_disc_barcode_and_mixed_mode() -> None:
    assert is_copy_protected_disc(barcode="724359123421") is True
    assert is_copy_protected_disc(barcode="0724359123421") is True
    assert is_copy_protected_disc(barcode="007567891234") is False
    assert is_copy_protected_disc(is_mixed_mode=True) is True
    assert is_copy_protected_disc(is_mixed_mode=False) is False


def test_is_copy_protected_disc_q_subchannel_crc_errors() -> None:
    zero_crc = "Track 1 finished, found 0 Q sub-channels with CRC errors\n"
    assert is_copy_protected_disc(whipper_info_stdout=zero_crc) is False

    low_crc = "Track 1 finished, found 3 Q sub-channels with CRC errors\n"
    assert is_copy_protected_disc(whipper_info_stdout=low_crc) is True

    singular_crc = "Track 1: 1 Q sub-channel with CRC errors\n"
    assert is_copy_protected_disc(whipper_info_stdout=singular_crc) is True


def test_inspect_disc_propagates_disambiguation(monkeypatch: pytest.MonkeyPatch) -> None:
    sample_stdout = (
        "MusicBrainz disc id DISCID_PROTECTED\n"
        "Disc duration: 00:42:00.000, 10 audio tracks\n"
        "release not found: NotFoundException\n"
    )

    class MockProc:
        def __init__(self, *a: object, **kw: object) -> None:
            import io
            self.stdout = io.StringIO(sample_stdout)
            self.returncode = 0
            self.pid = 2222

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    mb_api = {
        "releases": [
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "title": "Protected Album",
                "artist-credit": [{"name": "Test Artist", "joinphrase": ""}],
                "disambiguation": "Copy Control / CDS200",
            }
        ]
    }
    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("subprocess.Popen", MockProc)
    monkeypatch.setattr("whipit.net.fetch_bounded", lambda *a, **kw: json.dumps(mb_api).encode("utf-8"))
    monkeypatch.setattr(
        "whipit.audio.metadata.fetch_release_details",
        lambda *a, **kw: type(
            "D",
            (),
            {"tracks": [], "discogs_url": None, "disc_number": None, "total_discs": None, "artist": None, "title": None},
        )(),
    )

    res = inspect_disc("/dev/sr0")
    assert res.success is True
    assert res.disambiguation == "Copy Control / CDS200"
    assert res.matching_releases[0].disambiguation == "Copy Control / CDS200"


def test_drive_panel_copy_protection_badge_and_safe_preset(
    monkeypatch: pytest.MonkeyPatch,
    temp_env,
) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from whipit.ui.gui.drive_panel import DrivePanel

    _ = QApplication.instance() or QApplication([])
    panel = DrivePanel(device="/dev/sr0", prefs=UserPreferences(default_speed=None))
    assert panel.protected_badge.isHidden() is True

    defeats_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "whipit.ui.gui.drive_panel.store_whipper_defeats_cache",
        lambda dev, val: defeats_calls.append((dev, val)),
    )

    # 1. Mixed-mode disc detected on drive poll triggers badge immediately without duplicate log spam
    monkeypatch.setattr(panel, "identify_disc", lambda silent=True: None)
    panel.handle_drive_status(CDS_DISC_OK, CDS_MIXED)
    assert panel.protected_badge.isHidden() is False
    assert panel.keep_going_chk.isChecked() is True
    assert panel._safe_speed_override == 4
    calls_after_first = len(defeats_calls)
    panel.handle_drive_status(CDS_DISC_OK, CDS_MIXED)
    assert len(defeats_calls) == calls_after_first

    # 2. Switching to standard Audio CD clears mixed-mode badge
    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert panel.protected_badge.isHidden() is True
    assert panel._safe_speed_override is None

    # 3. EMI 7243 barcode detected via inspection / details triggers badge and survives audio polls
    barcode_res = DiscInspectionResult(
        success=True,
        artist="EMI Artist",
        title="Standard Title",
        mbid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        mb_url="https://musicbrainz.org/release/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        barcode="724359123421",
        matching_releases=[
            ReleaseOption(
                mbid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                artist="EMI Artist",
                title="Standard Title",
                barcode="724359123421",
            )
        ],
    )
    panel._on_identify_finished(barcode_res, silent=True)
    assert panel.protected_badge.isHidden() is False
    assert panel._safe_speed_override == 4

    panel.handle_drive_status(CDS_DISC_OK, CDS_AUDIO)
    assert panel.protected_badge.isHidden() is False

    spawned: list[dict[str, object]] = []

    class DummyRipWorker:
        def __init__(self, **kwargs: object) -> None:
            spawned.append(kwargs)
            self.workflow_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()

        def start(self) -> None:
            pass

        def isRunning(self) -> bool:
            return False

    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", DummyRipWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)
    panel.start_rip()
    assert len(spawned) == 1
    assert spawned[0]["speed"] == 4
    assert spawned[0]["keep_going"] is True

    # 4. Normal disc clears badge and _on_details_resolved re-evaluates protection cleanly
    clean_res = DiscInspectionResult(
        success=True,
        artist="Normal Artist",
        title="Normal Album",
        mbid="11111111-2222-3333-4444-555555555555",
        mb_url="https://musicbrainz.org/release/11111111-2222-3333-4444-555555555555",
        matching_releases=[
            ReleaseOption(
                mbid="11111111-2222-3333-4444-555555555555",
                artist="Normal Artist",
                title="Normal Album",
                disambiguation="Copy Control",
            )
        ],
    )
    clean_res.matching_releases[0].disambiguation = None
    panel._on_identify_finished(clean_res, silent=True)
    assert panel.protected_badge.isHidden() is True
    assert panel._safe_speed_override is None

    panel.session.matching_releases[0].disambiguation = "CDS200"
    panel._on_details_resolved(
        "11111111-2222-3333-4444-555555555555",
        "",
        [TrackInfo(number=1, title="Track 1")],
    )
    assert panel.protected_badge.isHidden() is False
