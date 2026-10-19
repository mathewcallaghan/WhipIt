"""Cover art extraction, embedding, replication, and database URL resolution."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from whipit import __version__
from whipit.models import TrackInfo

ART_NAMES = [
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
]


@dataclass
class ReleaseDetails:
    discogs_url: Optional[str] = None
    tracks: List[TrackInfo] = field(default_factory=list)


def find_cover_art(directory: Path) -> Optional[Path]:
    for name in ART_NAMES:
        for p in directory.rglob(name):
            if p.is_file():
                return p
    return None


def embed_cover_art_flacs(flac_files: List[Path], art_path: Path) -> None:
    if not art_path.exists():
        return
    for f in flac_files:
        subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", str(f)], check=False, capture_output=True)
        subprocess.run(["metaflac", f"--import-picture-from={art_path}", str(f)], check=False, capture_output=True)


def replicate_art_to_opus(source_dir: Path, target_dir: Path) -> None:
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for img in source_dir.rglob(ext):
            rel = img.relative_to(source_dir)
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(img, dest)


def fetch_release_details(
    mbid: str,
    barcode: Optional[str] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    expected_tracks: Optional[int] = None,
    timeout: float = 8.0,
) -> ReleaseDetails:
    """Queries MusicBrainz API for official track titles, lengths, and Discogs link in one request."""
    time.sleep(1.1)

    url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=recordings+url-rels+release-groups&fmt=json"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"WhipIt/{__version__} (https://github.com/whipit)",
            "Accept": "application/json",
        },
    )

    data = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
        except urllib.error.HTTPError as err:
            if err.code == 503 and attempt == 0:
                time.sleep(1.5)
                continue
            break
        except Exception:
            break

    tracks: List[TrackInfo] = []
    discogs_url: Optional[str] = None

    if data:
        # 1. Parse track list from media entry
        media_list = data.get("media", [])
        target_medium = None

        if len(media_list) == 1:
            target_medium = media_list[0]
        elif expected_tracks:
            for m in media_list:
                if m.get("track-count") == expected_tracks or len(m.get("tracks", [])) == expected_tracks:
                    target_medium = m
                    break
        if not target_medium and media_list:
            target_medium = media_list[0]

        if target_medium:
            for tr in target_medium.get("tracks", []):
                t_num_raw = tr.get("position", tr.get("number", len(tracks) + 1))
                try:
                    t_num = int(t_num_raw)
                except Exception:
                    t_num = len(tracks) + 1

                t_title = tr.get("title") or tr.get("recording", {}).get("title") or f"Track {t_num}"
                t_len_ms = tr.get("length") or tr.get("recording", {}).get("length")
                t_dur = None
                if t_len_ms:
                    secs = int(t_len_ms) // 1000
                    m, s = divmod(secs, 60)
                    h, m = divmod(m, 60)
                    t_dur = f"{m:02d}:{s:02d}" if h == 0 else f"{h}:{m:02d}:{s:02d}"
                tracks.append(TrackInfo(number=t_num, title=t_title, duration=t_dur))

        # 2. Parse Discogs link from release relations
        for rel in data.get("relations", []):
            resource = rel.get("url", {}).get("resource", "")
            if "discogs.com" in resource:
                discogs_url = resource
                break

        # Check release group if not found on release level
        if not discogs_url:
            rg = data.get("release-group")
            if rg and "id" in rg:
                rg_id = rg["id"]
                time.sleep(1.1)
                rg_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id}?inc=url-rels&fmt=json"
                rg_req = urllib.request.Request(
                    rg_url,
                    headers={
                        "User-Agent": f"WhipIt/{__version__} (https://github.com/whipit)",
                        "Accept": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(rg_req, timeout=timeout) as resp:
                        if resp.status == 200:
                            rg_data = json.loads(resp.read().decode("utf-8"))
                            for rel in rg_data.get("relations", []):
                                resource = rel.get("url", {}).get("resource", "")
                                if "discogs.com" in resource:
                                    discogs_url = resource
                                    break
                except Exception:
                    pass

    # Fallback to Discogs search by barcode or artist/title
    if not discogs_url:
        if barcode and re.match(r"^\d+$", barcode):
            discogs_url = f"https://www.discogs.com/search/?q={urllib.parse.quote_plus(barcode)}&type=release"
        elif artist and title and artist != "Unknown Artist":
            query = f"{artist} {title}"
            discogs_url = f"https://www.discogs.com/search/?q={urllib.parse.quote_plus(query)}&type=release"

    return ReleaseDetails(discogs_url=discogs_url, tracks=tracks)


def fetch_discogs_url_from_mbid(
    mbid: str,
    barcode: Optional[str] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    timeout: float = 8.0,
) -> Optional[str]:
    """Compatibility wrapper returning just the Discogs URL."""
    details = fetch_release_details(mbid, barcode=barcode, artist=artist, title=title, timeout=timeout)
    return details.discogs_url
