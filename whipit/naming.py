"""Portable filesystem-safe string normalization and filename formatting."""

from __future__ import annotations

import re

# Windows reserved device names (cannot be used as file or directory stems on FAT32/exFAT/NTFS)
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)


def truncate_to_bytes(val: str, max_bytes: int = 255) -> str:
    """Safely truncates a string to at most max_bytes in UTF-8 without splitting multi-byte characters."""
    encoded = val.encode("utf-8")
    if len(encoded) <= max_bytes:
        return val
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" ._-")
    return truncated if truncated else "Unknown"


def sanitize_filename(val: str, max_bytes: int = 120) -> str:
    """
    Sanitizes strings for safe, portable cross-platform filesystems (Android MTP, FAT32, exFAT, NTFS, ext4).
    Converts quotes, slashes, and colons cleanly, cleans delimiters, strips invalid chars and trailing dots,
    and protects Windows reserved device names.
    """
    if not val:
        return "Unknown"

    s = val.strip()

    # 1. Convert double quotes to single quotes
    s = s.replace('"', "'")

    # 2. Angle brackets to parentheses: <tag> -> (tag)
    s = s.replace("<", "(").replace(">", ")")

    # 3. Smart separator replacement:
    # Multiple slashes/pipes with surrounding whitespace become a spaced hyphen: " / " -> " - "
    s = re.sub(r"\s+[/\\|]+\s*", " - ", s)
    # Colons become spaced hyphens: "Title: Subtitle" -> "Title - Subtitle"
    s = re.sub(r"\s*:\s*", " - ", s)
    # Standalone slashes/pipes without leading space become a direct hyphen: "AC/DC" -> "AC-DC"
    s = re.sub(r"[/\\|]+", "-", s)

    # 4. Remove characters illegal across Windows / FAT32 / MTP: * ? and control characters
    s = re.sub(r"[*?]", "", s)
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)

    # 5. Normalize whitespace and hyphens
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*-\s*", " - ", s)

    # 6. Strip leading and trailing whitespace, dots, hyphens, and underscores
    # (Trailing dots and spaces are illegal on Windows/FAT32 and crash Android MTP transfers)
    s = re.sub(r"^[\s.\-_]+", "", s)
    s = re.sub(r"[\s.\-_]+$", "", s)

    # 7. Protect standalone Windows reserved device names (e.g. "aux" -> "aux_")
    if _WINDOWS_RESERVED.match(s):
        s = f"{s}_"

    if not s:
        return "Unknown"

    return truncate_to_bytes(s, max_bytes=max_bytes)


# Backward-compatibility alias
sanitise_string = sanitize_filename


def format_track_filename(number: int, artist: str, title: str, extension: str = "flac") -> str:
    """Produces standard, bounded track filenames (e.g. '01. Artist - Title.flac')."""
    s_art = sanitize_filename(artist, max_bytes=90)
    s_title = sanitize_filename(title, max_bytes=120)
    raw_stem = f"{number:02d}. {s_art} - {s_title}"
    ext = extension.lstrip(".")
    max_stem_bytes = 255 - len(ext.encode("utf-8")) - 1  # 255 minus dot and extension
    safe_stem = truncate_to_bytes(raw_stem, max_bytes=max_stem_bytes)
    return f"{safe_stem}.{ext}"


def format_album_stem(artist: str, album: str, max_bytes: int = 180) -> str:
    """Produces clean album folder stems: 'Artist - Album'."""
    s_art = sanitize_filename(artist, max_bytes=90)
    s_alb = sanitize_filename(album, max_bytes=120)
    raw_stem = f"{s_art} - {s_alb}"
    safe_stem = truncate_to_bytes(raw_stem, max_bytes=max_bytes)
    if _WINDOWS_RESERVED.match(safe_stem):
        safe_stem = f"{safe_stem}_"
    return safe_stem
