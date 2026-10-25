from pathlib import Path
from whipit.config import get_cache_dir
from whipit.models import DeviceInfo, JobManifest, PathLayout, TransactionJournal
from whipit.state_store import ManifestStore
from whipit.transaction import ArchiveTransactionManager
from whipit.ui.base import ConflictChoice, NullInteractionAdapter


def test_transaction_rollback_restores_replaced_flac(sample_manifest, temp_env):
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    (final_flac / "current.flac").write_bytes(b"NEW_VERSION")

    bak_flac = final_flac.parent / "backup.flac.replaced.2026"
    bak_flac.mkdir(parents=True, exist_ok=True)
    (bak_flac / "original.flac").write_bytes(b"ORIGINAL_VERSION")

    sample_manifest.transaction.flac_backed_up = True
    sample_manifest.transaction.flac_backup_path = bak_flac
    sample_manifest.transaction.flac_published = True

    tx = ArchiveTransactionManager(sample_manifest, store)
    ok = tx.rollback()
    assert ok is True
    assert (final_flac / "original.flac").exists()
    assert not bak_flac.exists()


def test_rollback_cleans_nonempty_destination_targets(sample_manifest, temp_env):
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    (final_flac / "published_track.flac").write_bytes(b"PUBLISHED_AUDIO")

    stage_undo = sample_manifest.paths.candidate_stage / "flac"
    stage_undo.mkdir(parents=True, exist_ok=True)
    (stage_undo / "stale_leftover.flac").write_bytes(b"STALE_STAGE_AUDIO")

    sample_manifest.transaction.flac_published = True
    store.save(sample_manifest)

    tx = ArchiveTransactionManager(sample_manifest, store)
    ok = tx.rollback()
    assert ok is True
    assert not final_flac.exists()
    assert stage_undo.exists()
    assert (stage_undo / "published_track.flac").exists()
    assert not (stage_undo / "stale_leftover.flac").exists()


def test_ui_port_conflict_resolution(sample_manifest, temp_env):
    class MockGuiAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            return ConflictChoice.TIMESTAMP_SUFFIX

    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=False,
        ui=MockGuiAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None


def test_conflict_resolution_warns_on_accurate_archive_downgrade(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")

    sample_manifest.verification.is_accurate = False
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    captured_warnings = []

    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            captured_warnings.append(warning)
            return ConflictChoice.TIMESTAMP_SUFFIX

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=False,
        ui=MockAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None
    assert len(captured_warnings) == 1
    assert "AccurateRip verified" in (captured_warnings[0] or "")


def test_conflict_resolution_defaults_to_timestamp_suffix(sample_manifest, temp_env):
    sample_manifest.paths.final_flac_dir.mkdir(parents=True, exist_ok=True)
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    proceed, eff_overwrite, ts = tx.resolve_destination_conflict(overwrite=False, ui=NullInteractionAdapter())
    assert proceed is False


def test_conflict_resolution_overwrite_flag_does_not_bypass_downgrade_warning(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")

    sample_manifest.verification.is_accurate = False
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    captured_warnings = []

    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            captured_warnings.append(warning)
            return ConflictChoice.TIMESTAMP_SUFFIX

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=True,
        ui=MockAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None
    assert len(captured_warnings) == 1
    assert "AccurateRip verified" in (captured_warnings[0] or "")


def test_conflict_resolution_overwrite_flag_bypasses_prompt_when_no_downgrade(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    (final_flac / "01.flac").touch()

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")

    sample_manifest.verification.is_accurate = True
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    class FailIfPromptedAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            raise AssertionError("Should not prompt when overwrite=True and no downgrade")

    proceed, eff_overwrite, ts = tx.resolve_destination_conflict(
        overwrite=True,
        ui=FailIfPromptedAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is True
    assert ts is None


def test_conflict_resolution_different_mbid_triggers_downgrade_warning(sample_manifest, temp_env, monkeypatch):
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    existing_flac = final_flac / "01.flac"
    existing_flac.touch()

    monkeypatch.setattr(
        "whipit.transaction.read_flac_tag",
        lambda f, tag: "mbid-old-release" if "MUSICBRAINZ" in tag else None,
    )

    sample_manifest.verification.is_accurate = True
    sample_manifest.metadata.musicbrainz_id = "mbid-new-remaster"

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")

    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    captured_warnings = []

    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            captured_warnings.append(warning)
            return ConflictChoice.TIMESTAMP_SUFFIX

    proceed, eff_overwrite, ts_suffix = tx.resolve_destination_conflict(
        overwrite=True,
        ui=MockAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is False
    assert ts_suffix is not None
    assert len(captured_warnings) == 1
    assert "DIFFERENT MusicBrainz release" in (captured_warnings[0] or "")


def test_conflict_resolution_warns_and_preserves_backup_on_non_flac_destination(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_opus = sample_manifest.paths.final_opus_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)

    (final_flac / "user_notes.txt").write_text("Important notes")

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"NEW_AUDIO")

    sample_manifest.verification.is_accurate = True
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    captured_warnings = []

    class MockAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir, warning=None) -> ConflictChoice:
            captured_warnings.append(warning)
            return ConflictChoice.OVERWRITE

    proceed, eff_overwrite, ts = tx.resolve_destination_conflict(
        overwrite=True,
        ui=MockAdapter(),
    )
    assert proceed is True
    assert eff_overwrite is True
    assert len(captured_warnings) == 1
    assert "does not look like a WhipIt archive" in (captured_warnings[0] or "")

    tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    backup_dirs = list(final_flac.parent.glob(f"{final_flac.name}.replaced.*"))
    assert len(backup_dirs) == 1
    assert (backup_dirs[0] / "user_notes.txt").exists()


def test_execute_publication_preserves_backup_on_different_mbid(sample_manifest, temp_env, monkeypatch):
    final_flac = sample_manifest.paths.final_flac_dir
    final_opus = sample_manifest.paths.final_opus_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")
    (final_flac / "old_release_track.flac").write_bytes(b"OLD_RELEASE")

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "new_release_track.flac").write_bytes(b"NEW_RELEASE")

    monkeypatch.setattr(
        "whipit.transaction.read_flac_tag",
        lambda f, tag: "mbid-first-pressing" if "old_release" in str(f) else "mbid-second-pressing",
    )

    sample_manifest.verification.is_accurate = True
    sample_manifest.metadata.musicbrainz_id = "mbid-second-pressing"
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    assert (final_flac / "new_release_track.flac").exists()

    backup_dirs = list(final_flac.parent.glob(f"{final_flac.name}.replaced.*"))
    assert len(backup_dirs) == 1
    assert (backup_dirs[0] / "old_release_track.flac").exists()


def test_execute_publication_preserves_backup_on_downgrade(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_opus = sample_manifest.paths.final_opus_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)

    existing_log = final_flac / "existing.log"
    existing_log.write_text("AccurateRip summary: All tracks accurately ripped\n")
    (final_flac / "verified_track.flac").write_bytes(b"VERIFIED_AUDIO")

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "unverified_track.flac").write_bytes(b"NEW_AUDIO")

    sample_manifest.verification.is_accurate = False
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    assert (final_flac / "unverified_track.flac").exists()

    backup_dirs = list(final_flac.parent.glob(f"{final_flac.name}.replaced.*"))
    assert len(backup_dirs) == 1
    assert (backup_dirs[0] / "verified_track.flac").exists()


def test_execute_publication_preserves_backup_on_standard_overwrite(sample_manifest, temp_env):
    final_flac = sample_manifest.paths.final_flac_dir
    final_opus = sample_manifest.paths.final_opus_dir
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)

    (final_flac / "curated_track.flac").write_bytes(b"CURATED_AUDIO")
    (final_flac / "booklet.pdf").write_bytes(b"PDF_BOOKLET")

    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_opus = sample_manifest.paths.candidate_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "new_track.flac").write_bytes(b"NEW_AUDIO")

    sample_manifest.verification.is_accurate = True
    store = ManifestStore()
    tx = ArchiveTransactionManager(sample_manifest, store)

    tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    assert (final_flac / "new_track.flac").exists()
    backup_dirs = list(final_flac.parent.glob(f"{final_flac.name}.replaced.*"))
    assert len(backup_dirs) == 1
    assert (backup_dirs[0] / "curated_track.flac").exists()
    assert (backup_dirs[0] / "booklet.pdf").exists()


def test_rollback_refuses_symlink_paths(sample_manifest, temp_env, tmp_path):
    store = ManifestStore()
    final_flac = sample_manifest.paths.final_flac_dir
    final_flac.parent.mkdir(parents=True, exist_ok=True)

    outside_victim = tmp_path / "victim_music"
    outside_victim.mkdir()
    (outside_victim / "original.flac").write_bytes(b"VICTIM")

    # Create symlink pointing outside approved root
    final_flac.symlink_to(outside_victim)

    sample_manifest.transaction.flac_published = True
    store.save(sample_manifest)

    tx = ArchiveTransactionManager(sample_manifest, store)
    ok = tx.rollback()
    assert ok is False
    assert outside_victim.exists()
    assert (outside_victim / "original.flac").exists()


def test_execute_publication_and_rollback_with_independent_roots(temp_env):
    flac_root = temp_env["root"] / "FLAC_ROOT"
    opus_root = temp_env["root"] / "OPUS_ROOT"
    flac_root.mkdir(parents=True, exist_ok=True)
    opus_root.mkdir(parents=True, exist_ok=True)

    cache = get_cache_dir()
    job_id = "test-tx-independent-roots"
    flac_stage = flac_root / f".whipit-stage.{job_id}"
    opus_stage = opus_root / f".whipit-stage.{job_id}"

    stage_flac = flac_stage / "flac"
    stage_opus = opus_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_AUDIO_SEPARATE_ROOT")
    (stage_opus / "01.opus").write_bytes(b"OPUS_AUDIO_SEPARATE_ROOT")

    final_flac = flac_root / "Artist - Album"
    final_opus = opus_root / "Artist - Album"

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=opus_root,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=flac_stage,
            candidate_stage_opus=opus_stage,
            final_flac_dir=final_flac,
            final_opus_dir=final_opus,
        ),
        transaction=TransactionJournal(),
    )

    store = ManifestStore()
    store.save(manifest)
    tx = ArchiveTransactionManager(manifest, store)

    tx.execute_publication(stage_flac, stage_opus, overwrite=False)

    assert (final_flac / "01.flac").exists()
    assert (final_opus / "01.opus").exists()
    assert manifest.transaction.committed is True
    assert manifest.transaction.flac_published is True
    assert manifest.transaction.opus_published is True

    # Test rollback to independent staging roots
    ok = tx.rollback()
    assert ok is True
    assert not final_flac.exists()
    assert not final_opus.exists()
    assert (stage_flac / "01.flac").exists()
    assert (stage_opus / "01.opus").exists()
    assert manifest.transaction.flac_published is False
    assert manifest.transaction.opus_published is False


def test_execute_publication_flac_only_when_opus_disabled(temp_env):
    flac_root = temp_env["root"] / "FLAC_ONLY_ROOT"
    flac_root.mkdir(parents=True, exist_ok=True)

    cache = get_cache_dir()
    job_id = "test-tx-flac-only"
    flac_stage = flac_root / f".whipit-stage.{job_id}"
    stage_flac = flac_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01.flac").write_bytes(b"FLAC_ONLY_AUDIO")

    final_flac = flac_root / "Artist - Album" / "Artist - Album (FLAC)"

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            generate_opus=False,
            whipper_stage=cache / f"stage_{job_id}",
            candidate_stage=flac_stage,
            final_flac_dir=final_flac,
            final_opus_dir=None,
        ),
        transaction=TransactionJournal(),
    )

    store = ManifestStore()
    store.save(manifest)
    tx = ArchiveTransactionManager(manifest, store)

    tx.execute_publication(stage_flac, stage_opus=None, overwrite=False)

    assert (final_flac / "01.flac").exists()
    assert manifest.transaction.committed is True
    assert manifest.transaction.flac_published is True
    assert manifest.transaction.opus_published is False

    ok = tx.rollback()
    assert ok is True
    assert not final_flac.exists()
    assert (stage_flac / "01.flac").exists()
    assert manifest.transaction.flac_published is False


def test_transaction_rollback_and_backup_with_clean_single_level_folders(temp_env):
    flac_root = temp_env["root"] / "CLEAN_FLAC_ROOT"
    opus_root = temp_env["root"] / "CLEAN_OPUS_ROOT"
    flac_root.mkdir(parents=True, exist_ok=True)
    opus_root.mkdir(parents=True, exist_ok=True)

    final_flac = flac_root / "Pink Floyd - Animals"
    final_opus = opus_root / "Pink Floyd - Animals"
    final_flac.mkdir(parents=True, exist_ok=True)
    final_opus.mkdir(parents=True, exist_ok=True)
    (final_flac / "01. Dogs.flac").write_bytes(b"ORIGINAL_FLAC_DOGS")
    (final_opus / "01. Dogs.opus").write_bytes(b"ORIGINAL_OPUS_DOGS")

    job_id = "test-clean-single-level-tx"
    flac_stage = flac_root / f".whipit-stage.{job_id}"
    opus_stage = opus_root / f".whipit-stage.{job_id}"
    stage_flac = flac_stage / "flac"
    stage_opus = opus_stage / "opus"
    stage_flac.mkdir(parents=True, exist_ok=True)
    stage_opus.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01. Dogs.flac").write_bytes(b"NEW_REMASTER_FLAC")
    (stage_opus / "01. Dogs.opus").write_bytes(b"NEW_REMASTER_OPUS")

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            opus_root=opus_root,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=flac_stage,
            candidate_stage_opus=opus_stage,
            final_flac_dir=final_flac,
            final_opus_dir=final_opus,
        ),
        transaction=TransactionJournal(),
    )

    store = ManifestStore()
    store.save(manifest)
    tx = ArchiveTransactionManager(manifest, store)

    # 1. Execute overwrite publication
    tx.execute_publication(stage_flac, stage_opus, overwrite=True)

    assert (final_flac / "01. Dogs.flac").read_bytes() == b"NEW_REMASTER_FLAC"
    assert (final_opus / "01. Dogs.opus").read_bytes() == b"NEW_REMASTER_OPUS"
    assert manifest.transaction.committed is True
    assert manifest.transaction.flac_backed_up is True
    backup_path = manifest.transaction.flac_backup_path
    assert backup_path is not None
    assert backup_path.parent == flac_root
    assert (backup_path / "01. Dogs.flac").read_bytes() == b"ORIGINAL_FLAC_DOGS"

    # 2. Rollback the overwrite transaction
    ok = tx.rollback()
    assert ok is True
    assert (final_flac / "01. Dogs.flac").read_bytes() == b"ORIGINAL_FLAC_DOGS"
    assert (final_opus / "01. Dogs.opus").read_bytes() == b"ORIGINAL_OPUS_DOGS"
    assert manifest.transaction.flac_backup_path is None
    assert not backup_path.exists()
    assert (stage_flac / "01. Dogs.flac").read_bytes() == b"NEW_REMASTER_FLAC"


def test_transaction_fresh_publication_rollback_cleans_empty_parent_multidisc(temp_env):
    flac_root = temp_env["root"] / "CLEAN_MULTI_ROOT"
    flac_root.mkdir(parents=True, exist_ok=True)

    album_dir = flac_root / "The Wall"
    final_flac = album_dir / "Disc 1"

    job_id = "test-clean-multi-rollback"
    flac_stage = flac_root / f".whipit-stage.{job_id}"
    stage_flac = flac_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)
    (stage_flac / "01. In the Flesh.flac").write_bytes(b"FLAC_DISC_1")

    manifest = JobManifest(
        job_id=job_id,
        device=DeviceInfo(path="/dev/sr0", hardware_id="TEST_DRIVE"),
        paths=PathLayout(
            flac_root=flac_root,
            generate_opus=False,
            whipper_stage=get_cache_dir() / f"stage_{job_id}",
            candidate_stage=flac_stage,
            final_flac_dir=final_flac,
        ),
        transaction=TransactionJournal(),
    )

    store = ManifestStore()
    store.save(manifest)
    tx = ArchiveTransactionManager(manifest, store)

    tx.execute_publication(stage_flac, stage_opus=None, overwrite=False)
    assert final_flac.exists()

    ok = tx.rollback()
    assert ok is True
    assert not final_flac.exists()
    assert not album_dir.exists()
