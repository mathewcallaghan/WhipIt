"""Cover art extraction, embedding, replication, and database URL resolution."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from whipit import __version__
from whipit.config import get_cache_dir
from whipit.models import TrackInfo
from whipit.net import fetch_bounded, is_strict_https_url

ART_NAMES = [
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
]

_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_MAX_ART_BYTES = 15 * 1024 * 1024
_mb_lock = threading.Lock()
_mb_last = 0.0


def _mb_throttle(interval: float = 1.1) -> None:
    global _mb_last
    with _mb_lock:
        now = time.monotonic()
        wait = _mb_last + interval - now
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.monotonic()


def _image_ext(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    return None


def is_valid_discogs_url(url: Optional[str]) -> bool:
    """Validates that a URL is a secure HTTPS link to Discogs."""
    return is_strict_https_url(url, ("discogs.com",))


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


def create_embedded_cover_art(
    art_path: Path,
    output_path: Path,
    max_dimension: int = 1000,
    max_bytes: int = 1_500_000,
) -> Path:
    """
    Creates a downscaled JPEG copy (max dimension ~1000px, quality 88) suitable for embedding
    into FLAC and Opus tags. Keeps the original full-size scan intact on disk while avoiding
    audio file bloat and FLAC 16 MiB picture block overflow.
    """
    if not art_path.exists():
        return art_path

    # Prefer PIL (Pillow) if available
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = 50_000_000

        with Image.open(art_path) as img:
            w, h = img.size
            if w * h > 50_000_000:
                return art_path
            if w <= max_dimension and h <= max_dimension and art_path.stat().st_size <= max_bytes:
                return art_path

            img = img.convert("RGB")
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(output_path, "JPEG", quality=88, optimize=True)
            return output_path
    except ImportError:
        pass
    except Exception:
        pass

    # Fall back to PyQt6 (QImage)
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QImage, QImageReader

        if hasattr(QImageReader, "setAllocationLimit"):
            QImageReader.setAllocationLimit(128)

        reader = QImageReader(str(art_path))
        sz = reader.size()
        if not sz.isValid() or sz.width() * sz.height() > 50_000_000:
            return art_path

        qimg = reader.read()
        if not qimg.isNull():
            w, h = qimg.width(), qimg.height()
            if w <= max_dimension and h <= max_dimension and art_path.stat().st_size <= max_bytes:
                return art_path

            if w > max_dimension or h > max_dimension:
                qimg = qimg.scaled(
                    max_dimension,
                    max_dimension,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if qimg.save(str(output_path), "JPEG", 88):
                return output_path
    except Exception:
        pass

    return art_path


def embed_cover_art_flacs(flac_files: List[Path], art_path: Path) -> bool:
    if not art_path.exists() or not flac_files:
        return False
    if art_path.stat().st_size >= (16 * 1024 * 1024 - 1024):
        return False
    all_ok = True
    for f in flac_files:
        subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", str(f)], check=False, capture_output=True)
        res = subprocess.run(["metaflac", f"--import-picture-from={art_path}", str(f)], check=False, capture_output=True)
        if res.returncode != 0:
            all_ok = False
    return all_ok


def replicate_art_to_opus(source_dir: Path, target_dir: Path) -> None:
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for img in source_dir.rglob(ext):
            rel = img.relative_to(source_dir)
            dest = target_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(img, dest)


def fetch_cover_art_from_caa(mbid: str, timeout: float = 12.0) -> Optional[Path]:
    """
    Downloads original high-resolution front cover art from Cover Art Archive into local cache.
    Tries the original master scan (/front) first, falling back to /front-1200 and /front-500 if needed.
    """
    if not mbid or not _MBID_RE.fullmatch(mbid.strip()):
        return None
    mbid = mbid.strip().lower()

    cache_covers_dir = get_cache_dir() / "covers"
    cache_covers_dir.mkdir(parents=True, exist_ok=True)

    for ext in (".jpg", ".jpeg", ".png"):
        cached = cache_covers_dir / f"{mbid}_front{ext}"
        if cached.exists() and cached.stat().st_size > 0:
            return cached

    urls = [
        f"https://coverartarchive.org/release/{mbid}/front",
        f"https://coverartarchive.org/release/{mbid}/front-1200",
        f"https://coverartarchive.org/release/{mbid}/front-500",
    ]
    req_headers = {
        "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
        "Accept": "image/*",
    }

    for url in urls:
        try:
            data = fetch_bounded(
                url,
                headers=req_headers,
                suffixes=("coverartarchive.org", "archive.org"),
                max_bytes=_MAX_ART_BYTES,
                timeout=timeout,
            )
            if not data or len(data) > _MAX_ART_BYTES:
                continue
            ext = _image_ext(data)
            if ext is None:
                continue
            dest = cache_covers_dir / f"{mbid}_front{ext}"
            tmp = dest.with_suffix(f".tmp.{os.getpid()}")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                return dest
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        except urllib.error.HTTPError as err:
            if err.code in (404, 400):
                continue
        except Exception:
            continue

    return None


def fetch_release_details(
    mbid: str,
    barcode: Optional[str] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    expected_tracks: Optional[int] = None,
    timeout: float = 8.0,
) -> ReleaseDetails:
    """Queries MusicBrainz API for official track titles, lengths, and Discogs link with proper contact User-Agent."""
    if not mbid or not _MBID_RE.fullmatch(mbid.strip()):
        return ReleaseDetails()
    mbid = mbid.strip().lower()

    _mb_throttle(1.1)

    url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=recordings+url-rels+release-groups&fmt=json"
    req_headers = {
        "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
        "Accept": "application/json",
    }

    data = None
    for attempt in range(2):
        try:
            raw_json = fetch_bounded(
                url,
                headers=req_headers,
                suffixes=("musicbrainz.org",),
                max_bytes=4_000_000,
                timeout=timeout,
            )
            data = json.loads(raw_json.decode("utf-8"))
            break
        except urllib.error.HTTPError as err:
            if err.code == 503 and attempt == 0:
                _mb_throttle(1.5)
                continue
            break
        except Exception:
            break

    tracks: List[TrackInfo] = []
    discogs_url: Optional[str] = None

    if data and isinstance(data, dict):
        try:
            media_list = data.get("media")
            if not isinstance(media_list, list):
                media_list = []
            target_medium = None

            if len(media_list) == 1 and isinstance(media_list[0], dict):
                target_medium = media_list[0]
            elif expected_tracks:
                for m in media_list:
                    if isinstance(m, dict) and (
                        m.get("track-count") == expected_tracks or len(m.get("tracks") or []) == expected_tracks
                    ):
                        target_medium = m
                        break
            if not target_medium and media_list and isinstance(media_list[0], dict):
                target_medium = media_list[0]

            if target_medium and isinstance(target_medium, dict):
                raw_tracks = target_medium.get("tracks")
                if isinstance(raw_tracks, list):
                    for tr in raw_tracks:
                        if not isinstance(tr, dict):
                            continue
                        t_num_raw = tr.get("position", tr.get("number", len(tracks) + 1))
                        try:
                            t_num = int(t_num_raw)
                        except Exception:
                            t_num = len(tracks) + 1

                        rec = tr.get("recording")
                        rec_title = rec.get("title") if isinstance(rec, dict) else None
                        t_title = tr.get("title") or rec_title or f"Track {t_num}"
                        rec_len = rec.get("length") if isinstance(rec, dict) else None
                        t_len_ms = tr.get("length") or rec_len
                        t_dur = None
                        if t_len_ms:
                            try:
                                secs = int(t_len_ms) // 1000
                                m, s = divmod(secs, 60)
                                h, m = divmod(m, 60)
                                t_dur = f"{m:02d}:{s:02d}" if h == 0 else f"{h}:{m:02d}:{s:02d}"
                            except (TypeError, ValueError):
                                t_dur = None
                        tracks.append(TrackInfo(number=t_num, title=t_title, duration=t_dur))

            for rel in (data.get("relations") or []):
                if not isinstance(rel, dict):
                    continue
                url_obj = rel.get("url") if isinstance(rel, dict) else None
                resource = url_obj.get("resource", "") if isinstance(url_obj, dict) else ""
                if is_valid_discogs_url(resource):
                    discogs_url = resource
                    break

            if not discogs_url:
                rg = data.get("release-group")
                rg_id = rg.get("id") if isinstance(rg, dict) else None
                if isinstance(rg_id, str) and _MBID_RE.fullmatch(rg_id.strip()):
                    _mb_throttle(1.1)
                    rg_url = f"https://musicbrainz.org/ws/2/release-group/{rg_id.strip().lower()}?inc=url-rels&fmt=json"
                    rg_req_headers = {
                        "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
                        "Accept": "application/json",
                    }
                    try:
                        raw_rg = fetch_bounded(
                            rg_url,
                            headers=rg_req_headers,
                            suffixes=("musicbrainz.org",),
                            max_bytes=2_000_000,
                            timeout=timeout,
                        )
                        rg_data = json.loads(raw_rg.decode("utf-8"))
                        if isinstance(rg_data, dict):
                            for rel in (rg_data.get("relations") or []):
                                if not isinstance(rel, dict):
                                    continue
                                url_obj = rel.get("url") if isinstance(rel, dict) else None
                                resource = url_obj.get("resource", "") if isinstance(url_obj, dict) else ""
                                if is_valid_discogs_url(resource):
                                    discogs_url = resource
                                    break
                    except Exception:
                        pass
        except Exception:
            pass

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
