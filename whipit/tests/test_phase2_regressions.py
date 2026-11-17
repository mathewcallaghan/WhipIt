"""Regression tests for Phase 2 workflow, metadata normalization, CUE escaping, and abort cleanup."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from whipit.models import AlbumMetadata, JobManifest, WorkflowResult, WorkflowState
from whipit.recovery import RecoveryCoordinator
from whipit.services.publication_service import PublicationService
from whipit.services.stage_metadata_service import StageMetadataService
from whipit.state_store import ManifestStore
from whipit.transaction import _build_pressing_suffix
from whipit.ui.base import NullInteractionAdapter
from whipit.workflow import RipWorkflowCoordinator


def test_compilation_cd_preserves_per_track_artists_when_year_and_titles_present(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_dir = sample_manifest.paths.whipper_stage / "disc"
    content_dir.mkdir(parents=True, exist_ok=True)
    t1 = content_dir / "01.flac"
    t2 = content_dir / "02.flac"
    t1.write_bytes(b"FLAC1")
    t2.write_bytes(b"FLAC2")

    track_tags = {
        "01.flac": {"TRACKNUMBER": "1", "ARTIST": "Daft Punk", "TITLE": "One More Time"},
        "02.flac": {"TRACKNUMBER": "2", "ARTIST": "Justice", "TITLE": "Genesis"},
    }
    written: list[tuple[str, dict[str, str]]] = []

    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.flac_audio_sha256", lambda p: "hash")
    monkeypatch.setattr(
        "whipit.workflow.read_flac_tag",
        lambda f, tag: track_tags.get(f.name, {}).get(tag.upper()),
    )
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: (written.append((f.name, dict(tags))), True)[1],
    )

    meta = AlbumMetadata(
        artist="Various Artists",
        album="Club Compilation",
        year="2007",
        custom_track_titles={1: "One More Time", 2: "Genesis"},
    )
    res = StageMetadataService().stage_candidate(
        content_dir=content_dir,
        candidate_stage=sample_manifest.paths.candidate_stage,
        candidate_stage_opus=None,
        generate_opus=False,
        expected_tracks=2,
        flac_files=[t1, t2],
        is_partial=False,
        metadata=meta,
        raw_tag_artist="Various Artists",
        raw_tag_album="Club Compilation",
        user_art=None,
        ui=NullInteractionAdapter(),
    )

    assert res.success is True
    names = sorted(p.name for p in res.flac_files)
    assert names == ["01. Daft Punk - One More Time.flac", "02. Justice - Genesis.flac"]
    assert all("ARTIST" not in tags for _, tags in written)
    assert all(tags.get("ALBUMARTIST") == "Various Artists" for _, tags in written)


def test_normalize_candidate_tracks_collision_never_deletes_existing_track(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_flac = tmp_path / "flac"
    stage_flac.mkdir()
    f1 = stage_flac / "raw_a.flac"
    f2 = stage_flac / "raw_b.flac"
    f1.write_bytes(b"TRACK_ONE_AUDIO")
    f2.write_bytes(b"TRACK_TWO_AUDIO")

    monkeypatch.setattr(
        "whipit.workflow.read_flac_tag",
        lambda f, tag: "1" if tag == "TRACKNUMBER" else "Same Title",
    )
    monkeypatch.setattr("whipit.workflow.write_flac_tags", lambda f, tags: True)

    meta = AlbumMetadata(artist="Artist", album="Album")
    StageMetadataService().normalize_candidate_tracks(stage_flac, meta, apply_tags=False)

    remaining = sorted(stage_flac.glob("*.flac"))
    assert len(remaining) == 2
    payloads = sorted(p.read_bytes() for p in remaining)
    assert payloads == [b"TRACK_ONE_AUDIO", b"TRACK_TWO_AUDIO"]


def test_cleanup_aborted_session_preserves_manifest_on_rollback_failed(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
) -> None:
    store = ManifestStore()
    sample_manifest.state = WorkflowState.ROLLBACK_FAILED
    sample_manifest.transaction.flac_published = True
    sample_manifest.transaction.flac_backup_path = temp_env["output"] / "Pink Floyd - The Wall (FLAC).replaced.1"
    store.save(sample_manifest)

    RecoveryCoordinator.cleanup_aborted_session(
        manifest=sample_manifest,
        store=store,
        whipper_stage=sample_manifest.paths.whipper_stage,
        candidate_stage=sample_manifest.paths.candidate_stage,
        candidate_stage_opus=sample_manifest.paths.candidate_stage_opus,
        ui=NullInteractionAdapter(),
        job_id=sample_manifest.job_id,
    )

    assert store.manifest_path(sample_manifest.job_id).exists()


def test_pressing_suffix_sanitizes_slashes_and_colons_in_year_and_country(
    sample_manifest: JobManifest,
    temp_env: dict[str, Path],
) -> None:
    meta = AlbumMetadata(artist="Artist", album="Album", year="1998/05/12", country="U/K")
    suffix = _build_pressing_suffix(meta)
    assert "/" not in suffix

    PublicationService.resolve_paths(
        manifest=sample_manifest,
        metadata=meta,
        flac_root=temp_env["output"],
        opus_root=temp_env["output"],
        generate_opus=True,
        timestamp_suffix="1998/05:12",
    )
    assert sample_manifest.paths.final_flac_dir is not None
    assert sample_manifest.paths.final_flac_dir.parent == temp_env["output"].resolve()
    sample_manifest.validate_paths()


def test_update_cue_sheet_metadata_handles_quotes_and_backslashes_safely(
    tmp_path: Path,
) -> None:
    stage_flac = tmp_path / "flac"
    stage_flac.mkdir()
    track_path = stage_flac / "01. Artist 'Live' - Song \\1.flac"
    track_path.write_bytes(b"FLAC")
    cue = stage_flac / "album.cue"
    cue.write_text(
        'PERFORMER "Old \\"Artist\\""\n'
        'TITLE "Old \\"Album\\""\n'
        'FILE "01. Old.flac" WAVE\n'
        '  TRACK 01 AUDIO\n'
        '    INDEX 01 00:00:00\n',
        encoding="utf-8",
    )

    meta = AlbumMetadata(artist='Artist "Live"', album='Album \\1 "Special"')
    StageMetadataService().update_cue_sheet_metadata(
        stage_flac=stage_flac,
        meta=meta,
        old_to_new_names={"01. Old.flac": track_path.name},
        tracks=[(1, "Song", track_path)],
    )

    updated = cue.read_text(encoding="utf-8")
    assert 'PERFORMER "Artist \\"Live\\""' in updated
    assert 'TITLE "Album \\\\1 \\"Special\\""' in updated
    assert 'FILE "01. Artist \'Live\' - Song \\\\1.flac" WAVE' in updated


def test_run_workflow_handles_secondary_enospc_in_abort_save_and_cleans_embed_temp(
    temp_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    monkeypatch.setattr("whipit.workflow.notify_rip_failed", lambda **kw: None)

    orig_save = coord.store.save

    def failing_abort_save(manifest: JobManifest, force: bool = False) -> None:
        if manifest.state == WorkflowState.ABORTED:
            raise OSError(errno.ENOSPC, "No space left on device")
        orig_save(manifest, force=force)

    monkeypatch.setattr(coord.store, "save", failing_abort_save)
    monkeypatch.setattr(
        coord,
        "_run_workflow_impl",
        lambda **kw: (_ for _ in ()).throw(OSError(errno.ENOSPC, "Disk full")),
    )

    res = coord.run_workflow()
    assert res == WorkflowResult.ABORTED

    # Also ensure embed_art_temp is cleaned up if transcode raises
    coord2 = RipWorkflowCoordinator(device="/dev/sr0")
    coord2.whipper_stage.mkdir(parents=True, exist_ok=True)
    (coord2.whipper_stage / "01.flac").touch()
    (coord2.whipper_stage / "test.cue").touch()
    (coord2.whipper_stage / "test.log").touch()
    temp_art = coord2.cache_dir / "embed_cover_testleak.jpg"
    temp_art.write_bytes(b"JPG")

    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)
    monkeypatch.setattr(
        "whipit.workflow.RippingEngine",
        lambda **kw: type(
            "E",
            (),
            {
                "run_extraction": lambda self, **k: type(
                    "R",
                    (),
                    {
                        "success": True,
                        "partial": False,
                        "cancelled": False,
                        "device_path": "/dev/sr0",
                        "effective_offset": 6,
                        "speed": None,
                    },
                )()
            },
        )(),
    )
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr("whipit.workflow.parse_whipper_report", lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1))
    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.read_flac_tag", lambda f, tag: "1" if tag == "TRACKNUMBER" else "Test")
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr(
        coord2.transcode_service,
        "prepare_embed_art",
        lambda *a, **kw: (temp_art, temp_art),
    )
    monkeypatch.setattr(
        coord2.transcode_service,
        "transcode_opus",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("Simulated encoder crash")),
    )

    res2 = coord2.run_workflow()
    assert res2 == WorkflowResult.ABORTED
    assert not temp_art.exists()
