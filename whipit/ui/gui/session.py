"""Authoritative disc session state model for the WhipIt desktop GUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from whipit.models import AlbumMetadata, ReleaseOption, TrackInfo
from whipit.ripping.identify import DiscInspectionResult


@dataclass
class ActiveSession:
    # Physical drive
    device: Optional[str] = None
    hardware_id: Optional[str] = None
    is_disc_loaded: bool = False

    # Identification & Release metadata
    mbid: Optional[str] = None
    artist: str = "Unknown Artist"
    title: str = "Unknown Album"
    disc_number: Optional[int] = None
    total_discs: Optional[int] = None
    duration: Optional[str] = None
    barcode: Optional[str] = None
    cat_no: Optional[str] = None

    # Track listing
    tracks: List[TrackInfo] = field(default_factory=list)
    matching_releases: List[ReleaseOption] = field(default_factory=list)

    # External database links
    mb_url: Optional[str] = None
    discogs_url: Optional[str] = None
    mb_lookup_url: Optional[str] = None

    # Cover artwork
    art_path: Optional[Path] = None
    art_is_user_selected: bool = False
    downloaded_art_path: Optional[Path] = None

    @property
    def is_multi_disc(self) -> bool:
        return (self.disc_number is not None and self.disc_number > 1) or (
            self.total_discs is not None and self.total_discs > 1
        )

    @property
    def display_title(self) -> str:
        if self.artist == "Unknown Artist" and self.title == "Unknown Album":
            return "Ready to rip Audio CD"
        disc_suffix = f" (Disc {self.disc_number})" if self.is_multi_disc and self.disc_number else ""
        return f"{self.artist} — {self.title}{disc_suffix}"

    def set_from_inspection(self, result: DiscInspectionResult) -> None:
        if result.success and (result.artist != "Unknown Artist" or result.mb_url):
            self.artist = result.artist
            self.title = result.title
            self.mbid = result.mbid
            self.mb_url = result.mb_url
            self.mb_lookup_url = result.mb_lookup_url
            self.discogs_url = result.discogs_url
            self.duration = result.duration
            self.barcode = result.barcode
            self.cat_no = result.cat_no
            self.tracks = list(result.tracks)
            self.matching_releases = list(result.matching_releases)
        else:
            self.artist = "Unknown Artist"
            self.title = f"Uncataloged Audio CD ({result.tracks_count or '?'} Tracks)"
            self.mbid = None
            self.mb_url = None
            self.discogs_url = None
            self.mb_lookup_url = result.mb_lookup_url
            self.tracks.clear()
            self.matching_releases.clear()

    def select_release(self, release: ReleaseOption) -> None:
        self.artist = release.artist
        self.title = release.title
        self.mbid = release.mbid
        self.mb_url = f"https://musicbrainz.org/release/{release.mbid}"
        self.barcode = release.barcode
        self.cat_no = release.cat_no
        self.duration = release.duration or self.duration
        if release.tracks:
            self.tracks = list(release.tracks)
        else:
            self.tracks.clear()

    def set_user_art(self, path: Path) -> None:
        self.art_path = path
        self.art_is_user_selected = True

    def set_downloaded_art(self, path: Path) -> None:
        self.downloaded_art_path = path
        if not self.art_is_user_selected:
            self.art_path = path

    def reset_art_to_original(self) -> None:
        self.art_is_user_selected = False
        self.art_path = self.downloaded_art_path

    def clear_art(self) -> None:
        if not self.art_is_user_selected:
            self.art_path = None
            self.downloaded_art_path = None

    def reset(self, keep_user_art: bool = False) -> None:
        self.mbid = None
        self.artist = "Unknown Artist"
        self.title = "Unknown Album"
        self.disc_number = None
        self.total_discs = None
        self.duration = None
        self.barcode = None
        self.cat_no = None
        self.tracks.clear()
        self.matching_releases.clear()
        self.mb_url = None
        self.discogs_url = None
        self.mb_lookup_url = None
        if not keep_user_art:
            self.art_path = None
            self.art_is_user_selected = False
            self.downloaded_art_path = None

    def to_album_metadata(self) -> AlbumMetadata:
        from whipit.config import sanitise_string
        return AlbumMetadata(
            artist=sanitise_string(self.artist),
            album=sanitise_string(self.title),
            disc_number=self.disc_number,
            total_discs=self.total_discs,
            album_stem=f"{sanitise_string(self.artist)} - {sanitise_string(self.title)}",
            musicbrainz_id=self.mbid,
            musicbrainz_url=self.mb_url,
            musicbrainz_lookup_url=self.mb_lookup_url,
            discogs_url=self.discogs_url,
        )
