"""Regression tests for Phase 4 ReplayGain, Opus, metadata/cover art, and HTTP bounds."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from whipit.audio.metadata import (
    clean_album_title,
    create_embedded_cover_art,
    embed_cover_art_flacs,
    fetch_cover_art_from_caa,
)
from whipit.audio.opus import _encode_single_track
from whipit.audio.replaygain import apply_replaygain
from whipit.net import fetch_bounded


def test_apply_replaygain_handles_missing_binary_and_timeout_and_dot_ext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "01.flac").touch()
    seen_cmds: list[list[str]] = []

    def mock_ok(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", mock_ok)
    assert apply_replaygain(tmp_path, ".flac") is True
    assert len(seen_cmds) == 1

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("loudgain not found")),
    )
    assert apply_replaygain(tmp_path, "flac") is False

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(["loudgain"], 120.0)),
    )
    assert apply_replaygain(tmp_path, "flac") is False


def test_opus_encode_does_not_skip_zero_byte_output_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flac = tmp_path / "track.flac"
    flac.touch()
    opus_dir = tmp_path / "opus"
    opus_dir.mkdir()
    empty_opus = opus_dir / "track.opus"
    empty_opus.write_bytes(b"")

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    fake_opusenc = mock_bin / "opusenc"
    fake_opusenc.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "with open(sys.argv[-1], 'wb') as f:\n"
        "    f.write(b'VALID_OPUS_DATA')\n",
        encoding="utf-8",
    )
    fake_opusenc.chmod(0o755)

    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))

    ok, _msg = _encode_single_track(flac, tmp_path, opus_dir, cover_art=None)
    assert ok is True
    assert empty_opus.read_bytes() == b"VALID_OPUS_DATA"


def test_clean_album_title_preserves_year_range_and_strips_catalog_noise() -> None:
    assert clean_album_title("Greatest Hits (1975-1985)") == "Greatest Hits (1975-1985)"
    assert clean_album_title("Anthology (1990-2005)") == "Anthology (1990-2005)"
    assert clean_album_title("Unplugged (936245024-2 WME)") == "Unplugged"


def test_create_embedded_cover_art_composites_transparent_png_over_white(tmp_path: Path) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage

    src_png = tmp_path / "transparent_cover.png"
    img = QImage(1200, 1200, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    for x in range(400, 800):
        for y in range(400, 800):
            img.setPixelColor(x, y, QColor(200, 30, 30, 255))
    assert img.save(str(src_png), "PNG")

    out_jpg = tmp_path / "embedded.jpg"
    res = create_embedded_cover_art(src_png, out_jpg, max_dimension=1000)
    assert res == out_jpg
    assert out_jpg.exists()

    loaded = QImage(str(out_jpg))
    corner = loaded.pixelColor(5, 5)
    assert corner.red() >= 245 and corner.green() >= 245 and corner.blue() >= 245


def test_embed_cover_art_flacs_rejects_pipe_char_and_handles_missing_metaflac(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flac = tmp_path / "01.flac"
    flac.touch()
    pipe_art = tmp_path / "bad|cover.jpg"
    pipe_art.write_bytes(b"\xff\xd8\xff\xe0")
    assert embed_cover_art_flacs([flac], pipe_art) is False

    normal_art = tmp_path / "cover.jpg"
    normal_art.write_bytes(b"\xff\xd8\xff\xe0")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("metaflac missing")),
    )
    assert embed_cover_art_flacs([flac], normal_art) is False


def test_fetch_cover_art_from_caa_prioritizes_front_image_in_json_index(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mbid = "11111111-2222-3333-4444-555555555555"
    back_jpeg = b"\xff\xd8\xff\xe0" + b"BACK_COVER"
    front_jpeg = b"\xff\xd8\xff\xe0" + b"FRONT_COVER"
    idx_payload = {
        "images": [
            {"front": False, "approved": True, "image": "https://archive.org/back.jpg"},
            {"front": True, "approved": True, "image": "https://archive.org/front.jpg"},
        ]
    }

    def mock_fetch(url: str, *a: object, **kw: object) -> bytes:
        if url.endswith("/release/" + mbid):
            return json.dumps(idx_payload).encode("utf-8")
        if url == "https://archive.org/front.jpg":
            return front_jpeg
        if url == "https://archive.org/back.jpg":
            return back_jpeg
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr("whipit.audio.metadata.fetch_bounded", mock_fetch)
    art = fetch_cover_art_from_caa(mbid)
    assert art is not None
    assert art.read_bytes() == front_jpeg


def test_fetch_bounded_upgrades_uppercase_http_and_checks_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    class FakeResp:
        def __init__(self, url: str) -> None:
            self._url = url

        def __enter__(self) -> FakeResp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def geturl(self) -> str:
            return self._url

        def read(self, n: int) -> bytes:
            return b"OK"

    class FakeOpener:
        def __init__(self, resp_url: str) -> None:
            self._resp_url = resp_url

        def open(self, req: urllib.request.Request, timeout: float = 12.0) -> FakeResp:
            seen_urls.append(req.full_url)
            return FakeResp(self._resp_url)

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *a: FakeOpener("https://musicbrainz.org/ws/2/release/123"),
    )
    data = fetch_bounded(
        "HTTP://musicbrainz.org/ws/2/release/123",
        headers={},
        suffixes=("musicbrainz.org",),
        max_bytes=1024,
    )
    assert data == b"OK"
    assert seen_urls == ["https://musicbrainz.org/ws/2/release/123"]

    monkeypatch.setattr(
        "urllib.request.build_opener",
        lambda *a: FakeOpener("https://evil.example.com/payload"),
    )
    with pytest.raises(ValueError, match="URL host not allowed"):
        fetch_bounded(
            "https://musicbrainz.org/ws/2/release/123",
            headers={},
            suffixes=("musicbrainz.org",),
            max_bytes=1024,
        )


def test_opus_encode_respects_custom_bitrate_preference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that opusenc receives the configured opus_bitrate CLI flag."""
    from whipit.audio.opus import _encode_single_track

    flac = tmp_path / "track.flac"
    flac.touch()
    opus_dir = tmp_path / "opus"
    opus_dir.mkdir()

    captured_cmds: list[list[str]] = []

    def mock_popen(cmd, **kw):
        captured_cmds.append(list(cmd))
        dest = cmd[-1]
        Path(dest).write_bytes(b"MOCK_OPUS_DATA")

        class MockProc:
            returncode = 0
            stdout = None
            stderr = None
            stdin = None
            pid = 12345

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        return MockProc()

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr("subprocess.Popen", mock_popen)

    ok, _msg = _encode_single_track(flac, tmp_path, opus_dir, cover_art=None, bitrate=256)
    assert ok is True
    assert len(captured_cmds) == 1
    assert "--bitrate" in captured_cmds[0]
    bitrate_idx = captured_cmds[0].index("--bitrate")
    assert captured_cmds[0][bitrate_idx + 1] == "256"
