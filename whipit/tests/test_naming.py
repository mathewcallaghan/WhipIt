from pathlib import Path
import pytest
from whipit.naming import (
    format_album_stem,
    format_track_filename,
    sanitize_filename,
    sanitise_string,
    truncate_to_bytes,
)


def test_truncate_to_bytes_ascii_and_multibyte():
    assert truncate_to_bytes("A" * 300, max_bytes=200) == "A" * 200

    s_kanji = "日本語テスト" * 20
    truncated = truncate_to_bytes(s_kanji, max_bytes=50)
    assert len(truncated.encode("utf-8")) <= 50
    assert truncated.encode("utf-8").decode("utf-8") == truncated

    s_emoji = "🎵" * 70
    assert len(truncate_to_bytes(s_emoji, max_bytes=15).encode("utf-8")) == 12


def test_sanitize_filename_quotes_and_delimiters():
    assert sanitize_filename('"Heroes"') == "'Heroes'"
    assert sanitize_filename('Song "Album Mix"') == "Song 'Album Mix'"
    assert sanitize_filename("Track <Intro>") == "Track (Intro)"
    assert sanitize_filename("Track | Version") == "Track - Version"


def test_sanitize_filename_colons_and_slashes():
    assert sanitize_filename("Delirium: Dance Remix") == "Delirium - Dance Remix"
    assert sanitize_filename("AC/DC") == "AC-DC"
    assert sanitize_filename("François K / Eric Kupper") == "François K - Eric Kupper"
    assert sanitize_filename(r"Path\To\Track") == "Path-To-Track"
    assert sanitize_filename(r"Track \ Alternate") == "Track - Alternate"


def test_sanitize_filename_trailing_dots_and_ellipses():
    assert sanitize_filename("Waiting...") == "Waiting"
    assert sanitize_filename("Album Title.") == "Album Title"
    assert sanitize_filename("- Leading and Trailing -") == "Leading and Trailing"


def test_sanitize_filename_illegal_characters_and_whitespace():
    assert sanitize_filename("Who's Next?") == "Who's Next"
    assert sanitize_filename("Song * (Demo)") == "Song (Demo)"
    assert sanitize_filename("Song\x00\x1fWith\x7fControls") == "SongWithControls"
    assert sanitize_filename("Multiple   Spaces   Here") == "Multiple Spaces Here"
    assert sanitize_filename("") == "Unknown"
    assert sanitize_filename("   ") == "Unknown"


def test_sanitize_filename_windows_reserved_device_names():
    assert sanitize_filename("aux") == "aux_"
    assert sanitize_filename("AUX") == "AUX_"
    assert sanitize_filename("con") == "con_"
    assert sanitize_filename("NUL") == "NUL_"
    assert sanitize_filename("com1") == "com1_"
    assert sanitize_filename("lpt9") == "lpt9_"
    # Legitimate non-device names containing reserved substrings are untouched
    assert sanitize_filename("Aux 88") == "Aux 88"
    assert sanitize_filename("Contact") == "Contact"


def test_format_track_filename():
    assert format_track_filename(1, "AC/DC", "Back In Black?") == "01. AC-DC - Back In Black.flac"
    assert format_track_filename(2, 'Artist "Name"', "Song: Remix", extension="opus") == "02. Artist 'Name' - Song - Remix.opus"
    assert format_track_filename(10, "Artist", "Waiting...") == "10. Artist - Waiting.flac"


def test_format_album_stem():
    assert format_album_stem("Pink Floyd", "The Wall") == "Pink Floyd - The Wall"
    assert format_album_stem("AC/DC", "Back In Black") == "AC-DC - Back In Black"
    assert format_album_stem("Artist", "Delirium: Dance Remix") == "Artist - Delirium - Dance Remix"
    assert format_album_stem("Artist", "Waiting...") == "Artist - Waiting"


def test_backward_compatibility_reexports():
    from whipit.config import sanitise_string as cfg_sanitise
    from whipit.validation.flac import format_track_filename as flac_format

    assert cfg_sanitise("AC/DC") == "AC-DC"
    assert flac_format(1, "Artist", "Title") == "01. Artist - Title.flac"
