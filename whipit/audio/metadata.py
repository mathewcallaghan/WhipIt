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
from typing import List, Optional, Sequence

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


_RE_MANUFACTURING_OR_DISAM = re.compile(
    r"(?:"
    r"\bIFPI\b|"
    r"\b(?:mould|master)\s+SID\b|"
    r"\bSID\s+codes?\b|"
    r"\bmatrix(?:\s+no|\s+num|\s+number|\s+code)?\b|"
    r"\b(?:glass\s+)?mastered\s+by\b|"
    r"\bpressed\s+by\b|"
    r"\b(?:repress|reissue|mispress|misprint)\b|"
    r"\b(?:club\s+edition|columbia\s+house|bmg\s+direct)\b|"
    r"\b(?:digipak|jewel\s+case|cardboard\s+sleeve)\b|"
    r"\b(?:made|manufactured|printed)\s+in\b|"
    r"\b(?:WME|DADC|SRC|PDO|Nimbus|Sonopress|Technicolor|Cinram|Disctronics|Allied|Specialty)\b|"
    r"\b(?:france|germany|uk|usa|japan|europe)\s+(?:we|ew|ca|pm|ba)\s*\d{2,4}\b|"
    r"\b(?:we|ew|ca|pm|ba)\s*\d{3,4}\b|"
    r"\b\d{4,}[-\s]\d{3,}[-\s]?[a-z0-9]*\b|"
    r"\b\d{10,14}\b"
    r")",
    re.IGNORECASE,
)

_RE_LEGITIMATE_EDITION = re.compile(
    r"(?:"
    r"\b(?:deluxe|special|expanded|extended|limited|collector'?s?|anniversary|tour|international)\s+(?:edition|version|release|set|package)\b|"
    r"\b(?:\d{1,2}(?:st|nd|rd|th)\s+anniversary(?:\s+edition|\s+remaster)?)\b|"
    r"\b(?:remaster(?:ed)?|remix(?:ed)?)\b|"
    r"\b(?:bonus\s+(?:tracks?|disc|cd|version))\b|"
    r"\b(?:explicit|clean)\b|"
    r"\b(?:live(?:\s+at|\s+in|\s+\d{4})?)\b|"
    r"\b(?:mono|stereo)\b|"
    r"\b(?:disc|cd)\s*\d+\b|"
    r"\b(?:vol(?:ume|\.)?\s*\d+)\b"
    r")",
    re.IGNORECASE,
)


def clean_album_title(title: str, disambiguation: Optional[str] = None) -> str:
    """
    Sanitizes album titles by removing MusicBrainz disambiguation text and manufacturing/pressing
    noise (e.g. matrix numbers, plant codes, IFPI codes, 'Made in...' tags), while strictly
    preserving legitimate album subtitles, remasters, and edition descriptions.
    """
    if not title:
        return ""

    raw_original = title.strip()
    clean = raw_original

    # 1. If explicit disambiguation is provided, attempt targeted removal if it contains manufacturing noise
    if disambiguation:
        disam = disambiguation.strip().strip("()[]")
        if disam:
            if _RE_MANUFACTURING_OR_DISAM.search(disam) or not _RE_LEGITIMATE_EDITION.search(disam):
                for pattern in (
                    rf"\s*\({re.escape(disam)}\)\s*$",
                    rf"\s*\[{re.escape(disam)}\]\s*$",
                    rf"\s*[-–—/:]\s*{re.escape(disam)}\s*$",
                ):
                    candidate = re.sub(pattern, "", clean, flags=re.IGNORECASE).strip()
                    if candidate != clean and candidate:
                        clean = candidate
                        break

    # 2. Iteratively inspect and clean trailing parenthetical or bracketed groups
    re_trailing_group = re.compile(r"\s*([\(\[])([^\(\)\[\]]+)([\)\]])\s*$")

    while True:
        match = re_trailing_group.search(clean)
        if not match:
            break

        open_bracket, inner_content, close_bracket = match.groups()
        inner_stripped = inner_content.strip()

        if not _RE_MANUFACTURING_OR_DISAM.search(inner_stripped):
            break

        clauses = [c.strip() for c in re.split(r"[,;]", inner_stripped) if c.strip()]
        kept_clauses: List[str] = []

        for clause in clauses:
            is_mfg = bool(_RE_MANUFACTURING_OR_DISAM.search(clause))
            is_legit = bool(_RE_LEGITIMATE_EDITION.search(clause))

            if is_legit and not is_mfg:
                kept_clauses.append(clause)
            elif is_mfg:
                continue
            else:
                continue

        prefix = clean[: match.start()].rstrip()

        if kept_clauses:
            rebuilt_group = f" {open_bracket}{', '.join(kept_clauses)}{close_bracket}"
            candidate = f"{prefix}{rebuilt_group}".strip()
            if candidate:
                clean = candidate
            break
        else:
            if prefix:
                clean = prefix
            else:
                break

    # 3. Clean trailing separated reissue/repress notes (e.g. "Album - Repress")
    clean = re.sub(r"\s*[-–—/:]\s*(?:repress|reissue)\s*$", "", clean, flags=re.IGNORECASE)

    # 4. Normalize spacing and remove orphan trailing separators
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"[\s\-–—/:]+$", "", clean).strip()

    return clean if clean else raw_original


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


def fetch_cover_art_from_caa(
    mbid: str,
    alternate_mbids: Optional[Sequence[str]] = None,
    timeout: float = 12.0,
) -> Optional[Path]:
    """
    Downloads original high-resolution front cover art from Cover Art Archive into local cache.
    Tries the requested release first (/front, /front-1200, /front-500, /front-250), then alternate
    matching releases from the same disc, then the release-group endpoints and release-group JSON index.
    """
    if not mbid or not _MBID_RE.fullmatch(mbid.strip()):
        return None
    mbid = mbid.strip().lower()

    cache_covers_dir = get_cache_dir() / "covers"
    cache_covers_dir.mkdir(parents=True, exist_ok=True)

    # 1. Check local cache for primary mbid
    for ext in (".jpg", ".jpeg", ".png"):
        cached = cache_covers_dir / f"{mbid}_front{ext}"
        if cached.exists() and cached.stat().st_size > 0:
            return cached

    req_headers = {
        "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
        "Accept": "image/*",
    }
    json_headers = {
        "User-Agent": f"WhipIt/{__version__} (https://github.com/mathewcallaghan/WhipIt)",
        "Accept": "application/json",
    }
    caa_suffixes = ("coverartarchive.org", "archive.org")

    def _download_image(candidate_urls: List[str]) -> Optional[bytes]:
        for url in candidate_urls:
            try:
                data = fetch_bounded(
                    url,
                    headers=req_headers,
                    suffixes=caa_suffixes,
                    max_bytes=_MAX_ART_BYTES,
                    timeout=timeout,
                )
                if not data or len(data) > _MAX_ART_BYTES:
                    continue
                ext = _image_ext(data)
                if ext is not None:
                    return data
            except urllib.error.HTTPError as err:
                if err.code in (404, 400):
                    continue
            except Exception:
                continue
        return None

    def _fetch_release_art(rel_id: str) -> Optional[bytes]:
        urls = [
            f"https://coverartarchive.org/release/{rel_id}/front",
            f"https://coverartarchive.org/release/{rel_id}/front-1200",
            f"https://coverartarchive.org/release/{rel_id}/front-500",
            f"https://coverartarchive.org/release/{rel_id}/front-250",
        ]
        img_bytes = _download_image(urls)
        if img_bytes:
            return img_bytes

        # Query /release/{rel_id} JSON index in case image is present without 'Front' tag
        try:
            raw_index = fetch_bounded(
                f"https://coverartarchive.org/release/{rel_id}",
                headers=json_headers,
                suffixes=caa_suffixes,
                max_bytes=500_000,
                timeout=timeout,
            )
            idx_json = json.loads(raw_index.decode("utf-8"))
            images = idx_json.get("images", []) if isinstance(idx_json, dict) else []
            for img_obj in images:
                if not isinstance(img_obj, dict):
                    continue
                candidate_urls = []
                thumbs = img_obj.get("thumbnails") or {}
                for sz in ("1200", "500", "large", "250", "small"):
                    if thumbs.get(sz):
                        candidate_urls.append(thumbs[sz])
                if img_obj.get("image"):
                    candidate_urls.append(img_obj["image"])
                found = _download_image(candidate_urls)
                if found:
                    return found
        except Exception:
            pass
        return None

    def _save_to_cache(data: bytes, key_id: str) -> Optional[Path]:
        ext = _image_ext(data) or ".jpg"
        dest = cache_covers_dir / f"{key_id}_front{ext}"
        tmp = dest.with_suffix(f".tmp.{os.getpid()}_{threading.get_ident()}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return dest
        except Exception:
            return None
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    # 2. Try primary release
    img_data = _fetch_release_art(mbid)
    if img_data:
        saved = _save_to_cache(img_data, mbid)
        if saved:
            return saved

    # 3. Try alternate matching releases for the same disc (e.g. other pressings)
    if alternate_mbids:
        for alt_id in alternate_mbids:
            if not alt_id or alt_id.lower() == mbid or not _MBID_RE.fullmatch(alt_id.strip()):
                continue
            alt_clean = alt_id.strip().lower()
            for ext in (".jpg", ".jpeg", ".png"):
                alt_cached = cache_covers_dir / f"{alt_clean}_front{ext}"
                if alt_cached.exists() and alt_cached.stat().st_size > 0:
                    dest = cache_covers_dir / f"{mbid}_front{ext}"
                    try:
                        shutil.copy2(alt_cached, dest)
                        return dest
                    except Exception:
                        return alt_cached

            alt_data = _fetch_release_art(alt_clean)
            if alt_data:
                saved = _save_to_cache(alt_data, mbid)
                _save_to_cache(alt_data, alt_clean)
                if saved:
                    return saved

    # 4. Fallback: Query release group
    try:
        _mb_throttle(1.1)
        mb_url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=release-groups&fmt=json"
        raw_mb = fetch_bounded(
            mb_url,
            headers=json_headers,
            suffixes=("musicbrainz.org",),
            max_bytes=500_000,
            timeout=timeout,
        )
        mb_data = json.loads(raw_mb.decode("utf-8"))
        rg = mb_data.get("release-group") if isinstance(mb_data, dict) else None
        rg_id = rg.get("id") if isinstance(rg, dict) else None
        if isinstance(rg_id, str) and _MBID_RE.fullmatch(rg_id.strip()):
            rg_id = rg_id.strip().lower()

            # 4a. Try direct release-group endpoints
            rg_urls = [
                f"https://coverartarchive.org/release-group/{rg_id}/front",
                f"https://coverartarchive.org/release-group/{rg_id}/front-1200",
                f"https://coverartarchive.org/release-group/{rg_id}/front-500",
                f"https://coverartarchive.org/release-group/{rg_id}/front-250",
            ]
            rg_data = _download_image(rg_urls)
            if rg_data:
                saved = _save_to_cache(rg_data, mbid)
                if saved:
                    return saved

            # 4b. Try release group JSON index
            try:
                raw_rg_index = fetch_bounded(
                    f"https://coverartarchive.org/release-group/{rg_id}",
                    headers=json_headers,
                    suffixes=caa_suffixes,
                    max_bytes=500_000,
                    timeout=timeout,
                )
                rg_idx = json.loads(raw_rg_index.decode("utf-8"))
                rg_images = rg_idx.get("images", []) if isinstance(rg_idx, dict) else []
                for img_obj in rg_images:
                    if not isinstance(img_obj, dict):
                        continue
                    candidate_urls = []
                    thumbs = img_obj.get("thumbnails") or {}
                    for sz in ("1200", "500", "large", "250", "small"):
                        if thumbs.get(sz):
                            candidate_urls.append(thumbs[sz])
                    if img_obj.get("image"):
                        candidate_urls.append(img_obj["image"])
                    found = _download_image(candidate_urls)
                    if found:
                        saved = _save_to_cache(found, mbid)
                        if saved:
                            return saved
            except Exception:
                pass

            # 4c. Query MusicBrainz for other releases in that release group
            try:
                _mb_throttle(1.1)
                releases_url = f"https://musicbrainz.org/ws/2/release?release-group={rg_id}&fmt=json&limit=15"
                raw_rels = fetch_bounded(
                    releases_url,
                    headers=json_headers,
                    suffixes=("musicbrainz.org",),
                    max_bytes=1_000_000,
                    timeout=timeout,
                )
                rels_data = json.loads(raw_rels.decode("utf-8"))
                rels_list = rels_data.get("releases", []) if isinstance(rels_data, dict) else []
                rels_with_art = [
                    r.get("id") for r in rels_list
                    if isinstance(r, dict) and r.get("id") and (
                        (r.get("cover-art-archive") or {}).get("artwork") or
                        (r.get("cover-art-archive") or {}).get("front")
                    )
                ]
                other_rels = [
                    r.get("id") for r in rels_list
                    if isinstance(r, dict) and r.get("id") and r.get("id") not in rels_with_art
                ]
                for candidate_rel in (rels_with_art + other_rels):
                    if candidate_rel and candidate_rel.lower() != mbid:
                        cand_clean = candidate_rel.strip().lower()
                        cand_data = _fetch_release_art(cand_clean)
                        if cand_data:
                            saved = _save_to_cache(cand_data, mbid)
                            _save_to_cache(cand_data, cand_clean)
                            if saved:
                                return saved
            except Exception:
                pass
    except Exception:
        pass

    return None


def fetch_release_details(
    mbid: str,
    barcode: Optional[str] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    expected_tracks: Optional[int] = None,
    timeout: float = 12.0,
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
            if err.code in (429, 503) and attempt == 0:
                _mb_throttle(2.0)
                continue
            break
        except Exception:
            if attempt == 0:
                _mb_throttle(1.5)
                continue
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
                raw_tracks = target_medium.get("tracks") or target_medium.get("track")
                if not raw_tracks and len(media_list) > 1:
                    for alt_m in media_list:
                        if isinstance(alt_m, dict) and (alt_m.get("tracks") or alt_m.get("track")):
                            target_medium = alt_m
                            raw_tracks = alt_m.get("tracks") or alt_m.get("track")
                            break
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
