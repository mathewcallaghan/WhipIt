"""Deterministic property-based invariants and parser fuzzing suites."""

from __future__ import annotations

import random
from pathlib import Path
from typing import List

from whipit.config import parse_duration_seconds
from whipit.naming import sanitize_filename, truncate_to_bytes
from whipit.ripping.reports import parse_cue_expected_tracks, parse_whipper_report
from whipit.services.extraction_service import _ascii_int


def _mutate_string(rng: random.Random, seeds: List[str], alphabet: str) -> str:
    base = rng.choice(seeds)
    chars = list(base)
    for _ in range(rng.randint(1, 12)):
        op = rng.choice(("insert", "replace", "delete", "append"))
        if op == "insert":
            idx = rng.randint(0, len(chars))
            chars.insert(idx, rng.choice(alphabet))
        elif op == "replace" and chars:
            idx = rng.randint(0, len(chars) - 1)
            chars[idx] = rng.choice(alphabet)
        elif op == "delete" and chars:
            idx = rng.randint(0, len(chars) - 1)
            chars.pop(idx)
        else:
            chars.append(rng.choice(alphabet))
    return "".join(chars)


def test_fuzz_parse_cue_expected_tracks(tmp_path: Path) -> None:
    rng = random.Random(42)
    seeds = [
        'TITLE "Album"\nFILE "01.flac" WAVE\n  TRACK 01 AUDIO\n    INDEX 01 00:00:00\n',
        'PERFORMER "Artist"\nFILE "02.flac" WAVE\n  TRACK 02 DATA\n',
        'TRACK 99 AUDIO\nTRACK 00 AUDIO\nTRACK abc AUDIO\n',
        "",
    ]
    alphabet = '0123456789ABCDEFabcdefTRACK AUDIO DATA"\'\\\x00\x07\x1f\n\r\t []{}日本語🎵'
    cue_file = tmp_path / "fuzz.cue"

    for _ in range(600):
        payload = _mutate_string(rng, seeds, alphabet)
        cue_file.write_bytes(payload.encode("utf-8", errors="surrogatepass"))
        count = parse_cue_expected_tracks(cue_file)
        assert isinstance(count, int)
        assert count >= 0


def test_fuzz_parse_whipper_report(tmp_path: Path) -> None:
    rng = random.Random(42)
    seeds = [
        "Health status: No errors occurred\nAccurateRip summary: All tracks accurately ripped\nFilename: 01.flac\nStatus: Copy OK\n",
        "Health status: Extraction halted\nAccurateRip summary: Track not present\n",
        "Filename: track.flac\nStatus: Error\nHealth status:\nAccurateRip summary:\n",
        "",
    ]
    alphabet = "Health status:AccurateRip summary:Copy OKFilename\n\r\t\x00\x0b\x1b0123456789xyz!@# "
    report_file = tmp_path / "fuzz_report.log"

    for _ in range(600):
        payload = _mutate_string(rng, seeds, alphabet)
        report_file.write_bytes(payload.encode("utf-8", errors="replace"))
        health, accuraterip, ok_tracks, listed_tracks = parse_whipper_report(report_file)
        assert isinstance(health, str)
        assert isinstance(accuraterip, str)
        assert isinstance(ok_tracks, int) and ok_tracks >= 0
        assert isinstance(listed_tracks, int) and listed_tracks >= 0


def test_fuzz_parse_duration_seconds() -> None:
    rng = random.Random(42)
    seeds = [
        "00:45:53.826",
        "04:15",
        "01:02:03",
        "35s",
        "120",
        "invalid",
        "",
    ]
    alphabet = "0123456789:.-+eEsSmMhHnaninfNANINF \t\n\x00/"

    assert parse_duration_seconds(None) is None
    for _ in range(1000):
        fuzzed = _mutate_string(rng, seeds, alphabet)
        result = parse_duration_seconds(fuzzed)
        assert result is None or isinstance(result, float)


def test_fuzz_filename_sanitizer_and_truncation() -> None:
    rng = random.Random(42)
    seeds = [
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "LPT9",
        "日本語テスト🎵",
        'AC/DC: "Back In Black?"...',
        "   ...---___   ",
        "Normal Track Title",
        "",
    ]
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/:\\|*?<>"._- \x00\x01\x1f\x7f日本語🎵é'

    for _ in range(1000):
        fuzzed = _mutate_string(rng, seeds, alphabet)
        max_bytes = rng.choice((16, 32, 64, 120, 255))

        sanitized = sanitize_filename(fuzzed, max_bytes=max_bytes)
        encoded_s = sanitized.encode("utf-8")
        assert len(encoded_s) <= max_bytes
        assert encoded_s.decode("utf-8") == sanitized
        assert "/" not in sanitized
        assert "\x00" not in sanitized

        truncated = truncate_to_bytes(fuzzed, max_bytes=max_bytes)
        encoded_t = truncated.encode("utf-8")
        assert len(encoded_t) <= max_bytes
        assert encoded_t.decode("utf-8") == truncated


def test_fuzz_ascii_int_parser() -> None:
    rng = random.Random(42)
    seeds = ["1", "04/10", " 12 ", "²", "½", "-5", "12345", ""]
    alphabet = "0123456789/-+ \t\n²³½¼abc\x00"

    assert _ascii_int(None) is None
    for _ in range(1000):
        fuzzed = _mutate_string(rng, seeds, alphabet)
        result = _ascii_int(fuzzed)
        assert result is None or isinstance(result, int)
