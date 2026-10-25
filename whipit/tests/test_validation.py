import os
import subprocess
from pathlib import Path
import pytest
from whipit.config import sanitise_string, truncate_to_bytes
from whipit.models import AlbumMetadata
from whipit.ripping.reports import parse_cue_expected_tracks, parse_whipper_report
from whipit.validation.integrity import compare_trees, verify_release_candidate
from whipit.validation.tracks import validate_track_sequence
from whipit.workflow import RipWorkflowCoordinator


def test_truncate_to_bytes_ascii_and_multibyte():
    s_ascii = "A" * 300
    res_ascii = truncate_to_bytes(s_ascii, max_bytes=200)
    assert len(res_ascii.encode("utf-8")) <= 200
    assert len(res_ascii) == 200

    s_kanji = "日本語テスト" * 20
    res_kanji = truncate_to_bytes(s_kanji, max_bytes=50)
    encoded = res_kanji.encode("utf-8")
    assert len(encoded) <= 50
    assert encoded.decode("utf-8") == res_kanji

    s_emoji = "🎵" * 70
    res_emoji = truncate_to_bytes(s_emoji, max_bytes=15)
    assert len(res_emoji.encode("utf-8")) == 12


def test_resolve_paths_long_tags_prevent_errno_36(sample_manifest, temp_env):
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    long_meta = AlbumMetadata(
        artist="Super Long Artist Name In Classical Release " * 8,
        album="The Complete Symphonies Serenades Divertimenti Operas and Concertos " * 6,
    )

    coord.resolve_paths(long_meta, partial=True, timestamp_suffix="2026-10-20_15-30-00")
    flac_dir = coord.manifest.paths.final_flac_dir
    assert flac_dir is not None

    master_component = flac_dir.parent.name
    flac_component = flac_dir.name

    assert len(master_component.encode("utf-8")) <= 255
    assert len(flac_component.encode("utf-8")) <= 255

    flac_dir.mkdir(parents=True, exist_ok=True)
    assert flac_dir.exists()


def test_resolve_paths_partial_naming(sample_manifest, tmp_path):
    # 1. Under shared root (when flac_root == opus_root): format suffix prevents collision
    coord = RipWorkflowCoordinator(device="/dev/sr0")

    coord.resolve_paths(sample_manifest.metadata, partial=True, timestamp_suffix="")
    flac_dir = coord.manifest.paths.final_flac_dir
    assert flac_dir is not None
    assert flac_dir.name == "Pink Floyd - The Wall (Partial) (FLAC)"

    coord.resolve_paths(sample_manifest.metadata, partial=True, timestamp_suffix="2026-10-20")
    flac_dir_ts = coord.manifest.paths.final_flac_dir
    assert flac_dir_ts is not None
    assert flac_dir_ts.name == "Pink Floyd - The Wall (Partial 2026-10-20) (FLAC)"

    coord.resolve_paths(sample_manifest.metadata, partial=False, timestamp_suffix="")
    flac_dir_full = coord.manifest.paths.final_flac_dir
    assert flac_dir_full is not None
    assert flac_dir_full.name == "Pink Floyd - The Wall (FLAC)"

    # 2. Under independent roots: clean single-level folders without format suffix
    coord_ind = RipWorkflowCoordinator(
        device="/dev/sr0",
        flac_root=tmp_path / "FLAC",
        opus_root=tmp_path / "Opus",
    )
    coord_ind.resolve_paths(sample_manifest.metadata, partial=True, timestamp_suffix="")
    assert coord_ind.manifest.paths.final_flac_dir.name == "Pink Floyd - The Wall (Partial)"
    coord_ind.resolve_paths(sample_manifest.metadata, partial=False, timestamp_suffix="")
    assert coord_ind.manifest.paths.final_flac_dir.name == "Pink Floyd - The Wall"


def test_cue_track_count_parsing(tmp_path: Path):
    cue = tmp_path / "album.cue"
    cue.write_text("""
TITLE "Sample Album"
PERFORMER "Sample Artist"
FILE "track01.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
FILE "track02.flac" WAVE
  TRACK 02 AUDIO
    INDEX 01 03:00:00
FILE "track03.flac" WAVE
  TRACK 03 AUDIO
    INDEX 01 07:00:00
""")
    assert parse_cue_expected_tracks(cue) == 3


def test_whipper_report_parsing(tmp_path: Path):
    report = tmp_path / "report.log"
    report.write_text("""
Whipper report
Health status: No errors occurred
AccurateRip summary: All tracks accurately ripped
Filename: track01.flac
Status: Copy OK
Filename: track02.flac
Status: Copy OK
""")
    health, ar, ok_tracks, listed = parse_whipper_report(report)
    assert health == "No errors occurred"
    assert ar == "All tracks accurately ripped"
    assert ok_tracks == 2
    assert listed == 2


def test_whipper_report_parsing_uses_last_verdict_to_prevent_injection(tmp_path: Path):
    report = tmp_path / "report.log"
    report.write_text("""
Whipper report
Injected metadata: Health status: No errors occurred
Injected metadata: AccurateRip summary: All tracks accurately ripped
Actual results at log termination:
Health status: Extraction halted with errors (C2 errors)
AccurateRip summary: Track not present in database
Filename: track01.flac
Status: Copy OK
""")
    health, ar, ok_tracks, listed = parse_whipper_report(report)
    assert health == "Extraction halted with errors (C2 errors)"
    assert ar == "Track not present in database"


def test_cue_escape_strips_newlines_and_escapes_quotes():
    from whipit.workflow import _cue_escape

    injected = 'Malicious Artist\nTRACK 99 AUDIO\nTITLE "Fake Track"'
    escaped = _cue_escape(injected)
    assert "\n" not in escaped
    assert "\r" not in escaped
    assert '\\"Fake Track\\"' in escaped


def test_ascii_int_parser():
    from whipit.workflow import _ascii_int

    assert _ascii_int("1") == 1
    assert _ascii_int("04/10") == 4
    assert _ascii_int(" 12 ") == 12
    assert _ascii_int("²") is None
    assert _ascii_int("12345") is None
    assert _ascii_int(None) is None
    assert _ascii_int("-5") is None


def test_track_sequence_handles_unicode_digit_tag_without_crashing(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = "TRACKNUMBER=²\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 1)
    assert not valid
    assert any("missing valid TRACKNUMBER tag: '²'" in e for e in errors)


def test_flac_audio_sha256_returns_none_on_missing_or_corrupt_file(tmp_path):
    from whipit.validation.flac import flac_audio_sha256

    assert flac_audio_sha256(tmp_path / "nonexistent.flac") is None
    corrupt = tmp_path / "corrupt.flac"
    corrupt.write_bytes(b"NOT_A_FLAC")
    assert flac_audio_sha256(corrupt) is None


def test_track_sequence_detects_gap(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "03.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = "TRACKNUMBER=01\n" if "01.flac" in cmd[-1] else "TRACKNUMBER=03\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 3)
    assert not valid
    assert any("found 2, expected exactly 3" in e for e in errors)


def test_track_sequence_detects_duplicate_and_out_of_bounds(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "02.flac").touch()
    (tmp_path / "03.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = ""
        target = str(cmd[-1])
        if "01.flac" in target:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "02.flac" in target:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "03.flac" in target:
            Res.stdout = "TRACKNUMBER=99\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 3)
    assert not valid
    assert any("Duplicate" in e for e in errors)
    assert any("out of bounds" in e for e in errors)


def test_partial_rip_candidate_verification(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "02.flac").touch()
    (tmp_path / "04.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            returncode = 0
            stdout = ""
        last_arg = str(cmd[-1])
        if "01.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=01\n"
        elif "02.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=02\n"
        elif "04.flac" in last_arg:
            Res.stdout = "TRACKNUMBER=04\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setattr("whipit.validation.integrity.verify_flac_integrity", lambda files: True)

    valid_full, _ = validate_track_sequence(tmp_path, expected_count=4)
    assert not valid_full

    valid_partial, errors_partial = validate_track_sequence(
        tmp_path,
        expected_count=3,
        total_tracks=4,
        allow_partial=True,
    )
    assert valid_partial
    assert len(errors_partial) == 0

    ok, errors = verify_release_candidate(
        tmp_path,
        expected_count=3,
        total_tracks=4,
        allow_partial=True,
    )
    assert ok
    assert len(errors) == 0


def test_compare_trees_detects_byte_difference_with_same_mtime(tmp_path: Path):
    src = tmp_path / "source"
    tgt = tmp_path / "target"
    src.mkdir()
    tgt.mkdir()

    f1 = src / "01.flac"
    f2 = tgt / "01.flac"
    f1.write_bytes(b"ORIGINAL_AUDIO_PAYLOAD_12345678")
    f2.write_bytes(b"CORRUPTED_AUDIO_PAYLOAD_12345678")

    t = 1600000000.0
    os.utime(f1, (t, t))
    os.utime(f2, (t, t))

    assert not compare_trees(src, tgt)

    f2.write_bytes(b"ORIGINAL_AUDIO_PAYLOAD_12345678")
    os.utime(f2, (t, t))
    assert compare_trees(src, tgt)


def test_create_embedded_cover_art_downscales_large_image(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage
    from whipit.audio.metadata import create_embedded_cover_art

    large_scan = tmp_path / "huge_scan.jpg"
    img = QImage(2400, 1800, QImage.Format.Format_RGB32)
    img.fill(QColor("blue"))
    img.save(str(large_scan), "JPEG", 95)

    out_scaled = tmp_path / "embedded.jpg"
    result_path = create_embedded_cover_art(large_scan, out_scaled, max_dimension=1000)
    assert result_path == out_scaled
    assert out_scaled.exists()

    scaled_img = QImage(str(out_scaled))
    assert max(scaled_img.width(), scaled_img.height()) <= 1000


def test_create_embedded_cover_art_preserves_small_image(tmp_path):
    pytest.importorskip("PyQt6")
    from PyQt6.QtGui import QColor, QImage
    from whipit.audio.metadata import create_embedded_cover_art

    small_scan = tmp_path / "small_scan.jpg"
    img = QImage(600, 600, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    img.save(str(small_scan), "JPEG", 85)

    out_scaled = tmp_path / "embedded.jpg"
    result_path = create_embedded_cover_art(small_scan, out_scaled, max_dimension=1000)
    assert result_path == small_scan
    assert not out_scaled.exists()


def test_embed_cover_art_flacs_rejects_oversized_file(tmp_path):
    from whipit.audio.metadata import embed_cover_art_flacs

    fake_huge = tmp_path / "huge.jpg"
    with open(fake_huge, "wb") as f:
        f.truncate(20 * 1024 * 1024)

    ok = embed_cover_art_flacs([tmp_path / "track.flac"], fake_huge)
    assert ok is False
