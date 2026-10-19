from pathlib import Path
from whipit.ripping.reports import parse_cue_expected_tracks, parse_whipper_report
from whipit.transaction import ArchiveTransactionManager
from whipit.ui.base import ConflictChoice, NullInteractionAdapter
from whipit.validation.tracks import validate_track_sequence


def test_cue_track_count_parsing(tmp_path: Path):
    cue = tmp_path / "album.cue"
    cue.write_text("""
TITLE "Sample Album"
PERFORMER "Sample Artist"
FILE "track01.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
FILE "track02.flac" WAVE
  TRACK 02 AUDIO
    INDEX 01 03:00:00
FILE "track03.flac" WAVE
  TRACK 03 AUDIO
    INDEX 01 07:00:00
""")
    assert parse_cue_expected_tracks(cue) == 3


def test_whipper_report_parsing(tmp_path: Path):
    report = tmp_path / "report.log"
    report.write_text("""
Whipper report
Health status: No errors occurred
AccurateRip summary: All tracks accurately ripped
Filename: track01.flac
Status: Copy OK
Filename: track02.flac
Status: Copy OK
""")
    health, ar, ok_tracks, listed = parse_whipper_report(report)
    assert health == "No errors occurred"
    assert ar == "All tracks accurately ripped"
    assert ok_tracks == 2
    assert listed == 2


def test_track_sequence_detects_gap(tmp_path: Path, monkeypatch):
    (tmp_path / "01.flac").touch()
    (tmp_path / "03.flac").touch()

    def mock_run(cmd, *args, **kwargs):
        class Res:
            stdout = "TRACKNUMBER=01\n" if "01.flac" in cmd[-1] else "TRACKNUMBER=03\n"
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)
    valid, errors = validate_track_sequence(tmp_path, 3)
    assert not valid
    assert any("found 2, expected exactly 3" in e for e in errors)


def test_ui_port_conflict_resolution(sample_manifest, temp_env):
    from whipit.state_store import ManifestStore

    class MockGuiAdapter(NullInteractionAdapter):
        def is_interactive(self) -> bool:
            return True

        def prompt_conflict_resolution(self, flac_dir, opus_dir) -> ConflictChoice:
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
