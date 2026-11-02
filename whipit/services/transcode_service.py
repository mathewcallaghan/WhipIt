"""Service encapsulating cover art embedding, parallel Opus transcoding, ReplayGain, and bitstream verification."""

from __future__ import annotations

import logging
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple

from whipit.audio.opus import transcode_flacs_to_opus
from whipit.audio.replaygain import apply_replaygain
from whipit.ui.base import LogLevel, UserInteractionPort
from whipit.validation.flac import verify_flac_integrity

logger = logging.getLogger(__name__)


def _wf() -> Any:
    return sys.modules.get("whipit.workflow", sys.modules[__name__])


class TranscodeService:
    """Handles audio post-processing: artwork embedding, Opus 192k transcoding, ReplayGain tagging, and bitstream validation."""

    def prepare_embed_art(
        self,
        stage_flac: Path,
        stage_opus: Optional[Path],
        flac_files: List[Path],
        cover_art: Optional[Path],
        cache_dir: Path,
        ui: UserInteractionPort,
    ) -> Tuple[Optional[Path], Optional[Path]]:
        """
        Embeds downscaled cover artwork into candidate FLAC files and copies artwork into Opus tree.
        Returns: (embed_art_temp, embed_art)
        """
        if not cover_art or not cover_art.exists():
            return None, None

        from whipit.audio.metadata import (
            create_embedded_cover_art,
            embed_cover_art_flacs,
            replicate_art_to_opus,
        )

        if stage_opus:
            replicate_art_to_opus(stage_flac, stage_opus)

        embed_art_temp = cache_dir / f"embed_cover_{uuid.uuid4().hex[:8]}.jpg"
        embed_art = create_embedded_cover_art(cover_art, embed_art_temp, max_dimension=1000)
        if not embed_cover_art_flacs(flac_files, embed_art):
            ui.on_log(LogLevel.WARN, "Could not embed cover art into all FLAC tracks.")

        return embed_art_temp, embed_art

    def transcode_opus(
        self,
        flac_files: List[Path],
        stage_flac: Path,
        stage_opus: Optional[Path],
        cover_art: Optional[Path],
        ui: UserInteractionPort,
        cancel_event: threading.Event,
    ) -> Tuple[bool, Optional[str]]:
        """Transcodes candidate FLAC files to Opus 192k."""
        if not stage_opus:
            return True, None

        ui.on_log(LogLevel.INFO, f"Transcoding {len(flac_files)} track(s) to Opus 192k...")
        wf = _wf()
        fn_enc = getattr(wf, "transcode_flacs_to_opus", transcode_flacs_to_opus)
        enc_ok = fn_enc(
            flac_files,
            stage_flac,
            stage_opus,
            cover_art,
            progress_callback=ui.on_transcode_progress,
            cancel_event=cancel_event,
        )

        if not enc_ok:
            if cancel_event.is_set():
                ui.on_log(LogLevel.WARN, "Opus transcoding cancelled by user.")
                return False, "Opus transcoding cancelled by user."
            return False, "Opus transcoding failed."

        return True, None

    def apply_replaygain(
        self,
        stage_flac: Path,
        stage_opus: Optional[Path],
        generate_opus: bool,
        ui: UserInteractionPort,
        cancel_event: threading.Event,
    ) -> Tuple[bool, Optional[str]]:
        """Calculates and writes EBU R128 ReplayGain tags across FLAC and Opus trees."""
        ui.on_log(LogLevel.HEADER, "Applying ReplayGain (EBU R128)...")
        wf = _wf()
        fn_rg = getattr(wf, "apply_replaygain", apply_replaygain)

        rg_flac = fn_rg(stage_flac, "flac")
        rg_opus = fn_rg(stage_opus, "opus") if (generate_opus and stage_opus) else True

        if not (rg_flac and rg_opus):
            if cancel_event.is_set():
                ui.on_log(LogLevel.WARN, "ReplayGain cancelled by user.")
                return False, "ReplayGain cancelled by user."
            ui.on_log(LogLevel.WARN, "ReplayGain tagging could not be applied; continuing publication.")

        return True, None

    def verify_flac_integrity(
        self,
        flac_files: List[Path],
        ui: UserInteractionPort,
    ) -> bool:
        """Runs bitstream verification (flac -t -s) across all candidate FLAC files."""
        ui.on_log(LogLevel.INFO, "Verifying candidate FLAC audio stream integrity...")
        wf = _wf()
        fn_v = getattr(wf, "verify_flac_integrity", verify_flac_integrity)
        return bool(fn_v(flac_files))
