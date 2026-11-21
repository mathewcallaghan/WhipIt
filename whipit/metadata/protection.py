"""Copy-protection heuristic detector for CDS200, Key2Audio, and MediaMax discs."""

from __future__ import annotations

import re
from typing import Optional

PROTECTION_KEYWORDS = (
    "copy control",
    "cactus data shield",
    "cds200",
    "cds 200",
    "key2audio",
    "mediamax",
    "copy-protected",
    "copy protected",
)

_RE_Q_SUBCHANNEL_CRC = re.compile(
    r"(\d+)\s+Q\s+sub-?channels?\s+with\s+CRC\s+errors",
    re.IGNORECASE,
)


def is_copy_protected_disc(
    whipper_info_stdout: str = "",
    release_title: str = "",
    disambiguation: str = "",
    barcode: Optional[str] = None,
    is_mixed_mode: bool = False,
) -> bool:
    """Heuristic check to determine if an Audio CD utilizes non-standard copy-control structures."""
    combined_text = f"{release_title} {disambiguation}".lower()

    # 1. Direct MusicBrainz Title / Disambiguation Keyword Match
    if any(keyword in combined_text for keyword in PROTECTION_KEYWORDS):
        return True

    # 2. Enhanced CD (CDS_MIXED dual-session format)
    if is_mixed_mode:
        return True

    # 3. Copy-Control Barcode Prefixes (e.g. EMI / Virgin 7243... series)
    clean_barcode = barcode.strip() if barcode else ""
    if clean_barcode.startswith(("7243", "07243")):
        return True

    # 4. Check for Q sub-channel CRC errors reported during TOC scanning
    crc_matches = _RE_Q_SUBCHANNEL_CRC.findall(whipper_info_stdout or "")
    if crc_matches and sum(int(c) for c in crc_matches) > 0:
        return True

    return False
