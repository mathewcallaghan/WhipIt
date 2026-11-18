from pathlib import Path

from whipit.audio.metadata import clean_album_title
from whipit.models import AlbumMetadata, TrackInfo, WorkflowResult
from whipit.ripping.identify import inspect_disc
from whipit.ui.gui.session import ActiveSession
from whipit.validation.flac import format_track_filename, write_flac_tags
from whipit.workflow import RipWorkflowCoordinator


def test_album_metadata_custom_fields():
    meta = AlbumMetadata(
        artist="Custom Artist",
        album="Custom Album",
        year="2026",
        genre="Alternative Rock",
        disc_number=2,
        total_discs=3,
        custom_track_titles={1: "Opening Track", 2: "Second Track"},
        custom_track_artists={1: "Guest Artist"},
    )
    assert meta.year == "2026"
    assert meta.genre == "Alternative Rock"
    assert meta.disc_number == 2
    assert meta.total_discs == 3
    assert meta.custom_track_titles[1] == "Opening Track"
    assert meta.custom_track_titles[2] == "Second Track"
    assert meta.custom_track_artists[1] == "Guest Artist"


def test_format_track_filename():
    fname = format_track_filename(1, "Foo Fighters", "The Pretender")
    assert fname == "01. Foo Fighters - The Pretender.flac"

    # Verifies character sanitization and forward slash replacement
    fname_slash = format_track_filename(4, "AC/DC", "Back In Black?")
    assert fname_slash == "04. AC-DC - Back In Black.flac"


def test_write_flac_tags_invocation(tmp_path: Path, monkeypatch):
    test_flac = tmp_path / "track.flac"
    test_flac.touch()

    executed_cmds = []

    def mock_run(cmd, *a, **kw):
        executed_cmds.append(cmd)
        class Res:
            returncode = 0
            stdout = ""
        return Res()

    monkeypatch.setattr("subprocess.run", mock_run)

    ok = write_flac_tags(
        test_flac,
        {"TITLE": "My Track", "ARTIST": "Band Name", "DATE": "2026"},
    )
    assert ok is True
    assert len(executed_cmds) == 1
    args = executed_cmds[0]
    assert "--remove-tag=TITLE" in args
    assert "--set-tag=TITLE=My Track" in args
    assert "--remove-tag=ARTIST" in args
    assert "--set-tag=ARTIST=Band Name" in args
    assert "--remove-tag=DATE" in args
    assert "--set-tag=DATE=2026" in args


def test_active_session_to_album_metadata_carries_custom_titles():
    session = ActiveSession()
    session.artist = "Sonic Youth"
    session.title = "Daydream Nation"
    session.year = "1988"
    session.genre = "Noise Rock"
    session.tracks = [
        TrackInfo(number=1, title="Teen Age Riot", duration="06:57"),
        TrackInfo(number=2, title="Silver Rocket", duration="03:47"),
    ]

    meta = session.to_album_metadata()
    assert meta.artist == "Sonic Youth"
    assert meta.album == "Daydream Nation"
    assert meta.year == "1988"
    assert meta.genre == "Noise Rock"
    assert meta.custom_track_titles == {1: "Teen Age Riot", 2: "Silver Rocket"}


def test_apply_custom_metadata_to_stage_renames_files_and_updates_cue(sample_manifest, temp_env, monkeypatch):
    stage_flac = sample_manifest.paths.candidate_stage / "flac"
    stage_flac.mkdir(parents=True, exist_ok=True)

    t1 = stage_flac / "01. Unknown Artist - Track 01.flac"
    t2 = stage_flac / "02. Unknown Artist - Track 02.flac"
    t1.touch()
    t2.touch()

    cue = stage_flac / "test.cue"
    cue.write_text(
        'PERFORMER "Unknown Artist"\n'
        'TITLE "Unknown Album"\n'
        'FILE "01. Unknown Artist - Track 01.flac" WAVE\n'
        '  TRACK 01 AUDIO\n'
        '    INDEX 01 00:00:00\n'
        'FILE "02. Unknown Artist - Track 02.flac" WAVE\n'
        '  TRACK 02 AUDIO\n'
        '    INDEX 01 03:00:00\n',
        encoding="utf-8",
    )

    custom_meta = AlbumMetadata(
        artist="Nirvana",
        album="Nevermind",
        year="1991",
        genre="Grunge",
        custom_track_titles={1: "Smells Like Teen Spirit", 2: "In Bloom"},
    )

    written_tags = []
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: written_tags.append((f.name, tags)),
    )
    monkeypatch.setattr(
        "whipit.workflow.read_flac_tag",
        lambda f, tag: ("1" if "01." in str(f) else "2") if tag == "TRACKNUMBER" else None,
    )

    coord = RipWorkflowCoordinator(device="/dev/sr0", initial_metadata=custom_meta)
    coord._apply_custom_metadata_to_stage(stage_flac, custom_meta)

    # Asserts FLAC tags were written
    assert len(written_tags) == 2
    assert written_tags[0][1]["TITLE"] == "Smells Like Teen Spirit"
    assert written_tags[0][1]["ARTIST"] == "Nirvana"
    assert written_tags[0][1]["DATE"] == "1991"
    assert written_tags[1][1]["TITLE"] == "In Bloom"

    # Asserts files were renamed
    renamed_files = [f.name for f in stage_flac.glob("*.flac")]
    assert "01. Nirvana - Smells Like Teen Spirit.flac" in renamed_files
    assert "02. Nirvana - In Bloom.flac" in renamed_files

    # Asserts CUE sheet was updated with new filenames
    cue_content = cue.read_text(encoding="utf-8")
    assert 'PERFORMER "Nirvana"' in cue_content
    assert 'TITLE "Nevermind"' in cue_content
    assert 'FILE "01. Nirvana - Smells Like Teen Spirit.flac" WAVE' in cue_content
    assert 'FILE "02. Nirvana - In Bloom.flac" WAVE' in cue_content


def test_stage_metadata_writes_disc_numbers_and_per_track_artists(tmp_path: Path, monkeypatch):
    from whipit.services.stage_metadata_service import StageMetadataService

    stage_flac = tmp_path / "flac_disc_test"
    stage_flac.mkdir()
    t1 = stage_flac / "01. Unknown.flac"
    t2 = stage_flac / "02. Unknown.flac"
    t1.write_bytes(b"FLAC1")
    t2.write_bytes(b"FLAC2")

    cue = stage_flac / "disc.cue"
    cue.write_text(
        'FILE "01. Unknown.flac" WAVE\n'
        '  TRACK 01 AUDIO\n'
        '    INDEX 01 00:00:00\n'
        'FILE "02. Unknown.flac" WAVE\n'
        '  TRACK 02 AUDIO\n'
        '    INDEX 01 04:00:00\n',
        encoding="utf-8",
    )

    written: dict[str, dict[str, str]] = {}
    monkeypatch.setattr(
        "whipit.workflow.write_flac_tags",
        lambda f, tags: (written.setdefault(f.name, {}).update(tags), True)[1],
    )
    monkeypatch.setattr(
        "whipit.workflow.read_flac_tag",
        lambda f, tag: ("1" if "01" in f.name else "2") if tag == "TRACKNUMBER" else None,
    )

    meta = AlbumMetadata(
        artist="Various Artists",
        album="Anthology Vol 1",
        disc_number=2,
        total_discs=3,
        custom_track_titles={1: "Intro Song", 2: "Second Song"},
        custom_track_artists={1: "Artist One", 2: "Artist Two"},
    )

    StageMetadataService().normalize_candidate_tracks(stage_flac, meta, apply_tags=True)

    renamed = sorted(f.name for f in stage_flac.glob("*.flac"))
    assert renamed == ["01. Artist One - Intro Song.flac", "02. Artist Two - Second Song.flac"]

    t1_tags = written.get("01. Unknown.flac", {})
    assert t1_tags.get("DISCNUMBER") == "2"
    assert t1_tags.get("DISCTOTAL") == "3"
    assert t1_tags.get("ARTIST") == "Artist One"
    assert t1_tags.get("ALBUMARTIST") == "Various Artists"
    assert t1_tags.get("TITLE") == "Intro Song"

    t2_tags = written.get("02. Unknown.flac", {})
    assert t2_tags.get("DISCNUMBER") == "2"
    assert t2_tags.get("DISCTOTAL") == "3"
    assert t2_tags.get("ARTIST") == "Artist Two"

    updated_cue = cue.read_text(encoding="utf-8")
    assert "REM DISCNUMBER 2" in updated_cue
    assert "REM TOTALDISCS 3" in updated_cue
    assert 'PERFORMER "Artist One"' in updated_cue
    assert 'PERFORMER "Artist Two"' in updated_cue


def test_parse_tracklist_wikipedia_format():
    from whipit.ui.gui.dialogs import parse_tracklist_clipboard_text

    raw = """
    1. "Smells Like Teen Spirit" – 5:01
    2. "In Bloom" – 4:14
    3. "Come as You Are" – 3:39
    4. "Breed" – 3:03
    """
    titles = parse_tracklist_clipboard_text(raw)
    assert titles == [
        "Smells Like Teen Spirit",
        "In Bloom",
        "Come as You Are",
        "Breed",
    ]


def test_parse_tracklist_bandcamp_and_discogs_format():
    from whipit.ui.gui.dialogs import parse_tracklist_clipboard_text

    raw_bandcamp = """
    01. Teen Age Riot 06:57
    02. Silver Rocket 03:47
    03. The Sprawl 07:42
    """
    assert parse_tracklist_clipboard_text(raw_bandcamp) == [
        "Teen Age Riot",
        "Silver Rocket",
        "The Sprawl",
    ]

    raw_discogs = """
    A1  The Pretender  4:31
    A2  Come Alive (demo)  5:31
    B1  If Ever  4:13
    """
    assert parse_tracklist_clipboard_text(raw_discogs) == [
        "The Pretender",
        "Come Alive (demo)",
        "If Ever",
    ]


def test_parse_tracklist_plain_text():
    from whipit.ui.gui.dialogs import parse_tracklist_clipboard_text

    raw = "Intro\nInterlude\nOutro\n"
    assert parse_tracklist_clipboard_text(raw) == ["Intro", "Interlude", "Outro"]


def test_open_edit_metadata_dialog_updates_session_and_ui(win, monkeypatch):
    win.session.artist = "Unknown Artist"
    win.session.title = "Unknown Album"
    win.session.mbid = None
    win.session.tracks = [
        TrackInfo(number=1, title="Track 01", duration="03:00"),
        TrackInfo(number=2, title="Track 02", duration="04:00"),
    ]

    monkeypatch.setattr(
        "whipit.ui.gui.main_window.prompt_edit_metadata_dialog",
        lambda parent, artist, album, year, genre, tracks: (
            "Queens of the Stone Age",
            "Songs for the Deaf",
            "2002",
            "Hard Rock",
            [
                TrackInfo(number=1, title="You Think I Ain't Worth a Dollar", duration="03:00"),
                TrackInfo(number=2, title="No One Knows", duration="04:00"),
            ],
        ),
    )

    win._open_edit_metadata_dialog()

    assert win.session.artist == "Queens of the Stone Age"
    assert win.session.title == "Songs for the Deaf"
    assert win.session.year == "2002"
    assert win.session.genre == "Hard Rock"
    assert win.session.tracks[0].title == "You Think I Ain't Worth a Dollar"
    assert win.session.tracks[1].title == "No One Knows"
    assert win.track_table.item(0, 1).text() == "You Think I Ain't Worth a Dollar"
    assert win.track_table.item(1, 1).text() == "No One Knows"
    # Auto-checks -U because disc has no MusicBrainz ID
    assert win.unknown_chk.isChecked() is True
    assert "Queens of the Stone Age" in win.album_title_lbl.text()


def test_open_edit_metadata_dialog_multidisc_and_per_track_artists(win, monkeypatch):
    win.session.artist = "Various Artists"
    win.session.title = "Global Underground"
    win.session.disc_number = 1
    win.session.total_discs = 1
    win.session.tracks = [
        TrackInfo(number=1, title="Track 01", duration="05:00"),
        TrackInfo(number=2, title="Track 02", duration="06:00"),
    ]

    monkeypatch.setattr(
        "whipit.ui.gui.main_window.prompt_edit_metadata_dialog",
        lambda parent, artist, album, year, genre, disc_number, total_discs, tracks: (
            "Various Artists",
            "Global Underground 019",
            "2001",
            "Progressive House",
            2,
            3,
            [
                TrackInfo(number=1, title="Deepest Star", artist="Salt Tank", duration="05:00"),
                TrackInfo(number=2, title="Flesh", artist="Jan Johnston", duration="06:00"),
            ],
        ),
    )

    win._open_edit_metadata_dialog()

    assert win.session.artist == "Various Artists"
    assert win.session.title == "Global Underground 019"
    assert win.session.disc_number == 2
    assert win.session.total_discs == 3
    assert win.session.is_multi_disc is True
    assert "(Disc 2)" in win.session.display_title
    assert "(Disc 2)" in win.album_title_lbl.text()
    assert win.session.tracks[0].artist == "Salt Tank"
    assert win.session.tracks[1].artist == "Jan Johnston"
    assert win.track_table.columnCount() == 4
    assert win.track_table.item(0, 1).text() == "Salt Tank"
    assert win.track_table.item(0, 2).text() == "Deepest Star"

    meta = win.session.to_album_metadata()
    assert meta.disc_number == 2
    assert meta.total_discs == 3
    assert meta.custom_track_artists[1] == "Salt Tank"
    assert meta.custom_track_artists[2] == "Jan Johnston"


def test_edit_metadata_dialog_direct_multidisc_and_paste_roundtrip(win):
    from whipit.ui.gui.dialogs import EditMetadataDialog

    tracks = [
        TrackInfo(number=1, title="Intro", duration="02:00"),
        TrackInfo(number=2, title="Outro", duration="03:00"),
    ]
    dlg = EditMetadataDialog(
        parent=None,
        artist="Original Artist",
        album="Box Set",
        year="2020",
        genre="Ambient",
        disc_number=3,
        total_discs=4,
        tracks=tracks,
    )
    assert dlg.multidisc_chk.isChecked() is True
    assert dlg.disc_spin.value() == 3
    assert dlg.total_discs_spin.value() == 4
    assert dlg.compilation_chk.isChecked() is False
    assert dlg.table.isColumnHidden(2) is True

    # Enable Various Artists mode and paste "Artist - Title" strings
    dlg.compilation_chk.setChecked(True)
    assert dlg.table.isColumnHidden(2) is False

    from PyQt6.QtWidgets import QApplication
    clipboard = QApplication.clipboard()
    if clipboard:
        clipboard.setText("01. Artist A - First Song 02:00\n02. Artist B - Second Song 03:00")
        dlg._on_paste_clipboard()

        art, alb, yr, gnr, d_num, t_discs, res_tracks = dlg.get_metadata()
        assert d_num == 3
        assert t_discs == 4
        assert res_tracks[0].artist == "Artist A"
        assert res_tracks[0].title == "First Song"
        assert res_tracks[1].artist == "Artist B"
        assert res_tracks[1].title == "Second Song"


def test_edit_metadata_dialog_shift_titles_up_and_down(win):
    from whipit.ui.gui.dialogs import EditMetadataDialog

    tracks = [
        TrackInfo(number=1, title="Track One", artist="Artist 1", duration="03:00"),
        TrackInfo(number=2, title="Track Two", artist="Artist 2", duration="04:00"),
        TrackInfo(number=3, title="Track Three", artist="Artist 3", duration="05:00"),
    ]
    dlg = EditMetadataDialog(parent=None, artist="Main Artist", album="Album", tracks=tracks)
    dlg.compilation_chk.setChecked(True)

    # Shift down (+1): Track 1 becomes generic, Track 2 becomes Track One, Track 3 becomes Track Two
    dlg._on_shift_down()
    _, _, _, _, _, _, res_down = dlg.get_metadata()
    assert res_down[0].title == "Track 01"
    assert res_down[0].artist is None
    assert res_down[1].title == "Track One"
    assert res_down[1].artist == "Artist 1"
    assert res_down[2].title == "Track Two"
    assert res_down[2].artist == "Artist 2"

    # Shift up (-1): Track 1 becomes Track One, Track 2 becomes Track Two, Track 3 becomes generic
    dlg._on_shift_up()
    _, _, _, _, _, _, res_up = dlg.get_metadata()
    assert res_up[0].title == "Track One"
    assert res_up[0].artist == "Artist 1"
    assert res_up[1].title == "Track Two"
    assert res_up[1].artist == "Artist 2"
    assert res_up[2].title == "Track 03"
    assert res_up[2].artist is None


def test_edit_metadata_dialog_paste_with_offset_and_padding(win):
    from whipit.ui.gui.dialogs import EditMetadataDialog

    tracks = [
        TrackInfo(number=1, title="Old 1", duration="03:00"),
        TrackInfo(number=2, title="Old 2", duration="04:00"),
    ]
    dlg = EditMetadataDialog(parent=None, artist="Artist", album="Album", tracks=tracks)

    from PyQt6.QtWidgets import QApplication
    clipboard = QApplication.clipboard()
    if clipboard:
        # Clipboard contains 4 songs, but disc only has 2 tracks: start at index 3 (offset 3)
        clipboard.setText("01. First 03:00\n02. Second 03:00\n03. Third 03:00\n04. Fourth 03:00")
        dlg._on_paste_clipboard(start_index=3)
        _, _, _, _, _, _, res_offset = dlg.get_metadata()
        assert res_offset[0].title == "Third"
        assert res_offset[1].title == "Fourth"

        # Clipboard contains only 1 song for 2 tracks: second track padded with generic
        clipboard.setText("01. Solo Track 03:00")
        dlg._on_paste_clipboard()
        _, _, _, _, _, _, res_padded = dlg.get_metadata()
        assert res_padded[0].title == "Solo Track"
        assert res_padded[1].title == "Track 02"


def test_start_rip_carries_custom_metadata_to_worker(win, monkeypatch):
    win.session.artist = "Radiohead"
    win.session.title = "In Rainbows"
    win.session.year = "2007"
    win.session.genre = "Art Rock"
    win.session.tracks = [
        TrackInfo(number=1, title="15 Step", duration="03:57"),
    ]

    spawned = []
    class MockWorker:
        def __init__(self, **kwargs):
            spawned.append(kwargs)
            self.workflow_finished = type("Sig", (), {"connect": lambda *a: None})()
            self.finished = type("Sig", (), {"connect": lambda *a: None})()
        def start(self):
            pass
        def isRunning(self):
            return False

    monkeypatch.setattr("whipit.ui.gui.main_window.RipWorker", MockWorker)
    monkeypatch.setattr("whipit.ui.gui.main_window._track_detached_worker", lambda w: w)

    win.start_rip()

    assert len(spawned) == 1
    init_meta = spawned[0]["initial_metadata"]
    assert init_meta is not None
    assert init_meta.artist == "Radiohead"
    assert init_meta.album == "In Rainbows"
    assert init_meta.year == "2007"
    assert init_meta.genre == "Art Rock"
    assert init_meta.custom_track_titles[1] == "15 Step"


def test_clean_album_title_strips_matrix_and_pressing_noise():
    noisy = "Unplugged (FRANCE WE 833, Made in Germany, 936245024-2 WME, has IFPI mould SID)"
    assert clean_album_title(noisy) == "Unplugged"

    noisy_dadc = "Album Title (DADC Austria matrix 12345)"
    assert clean_album_title(noisy_dadc) == "Album Title"

    noisy_repress = "Album Title - Repress"
    assert clean_album_title(noisy_repress) == "Album Title"


def test_clean_album_title_preserves_legitimate_editions():
    deluxe = "Nevermind (Deluxe Edition)"
    assert clean_album_title(deluxe) == "Nevermind (Deluxe Edition)"

    anniv = "The Dark Side of the Moon (50th Anniversary Remaster)"
    assert clean_album_title(anniv) == "The Dark Side of the Moon (50th Anniversary Remaster)"

    remaster = "Daydream Nation (2014 Remaster)"
    assert clean_album_title(remaster) == "Daydream Nation (2014 Remaster)"

    bonus = "OK Computer (Collector's Edition) [Bonus Tracks]"
    assert clean_album_title(bonus) == "OK Computer (Collector's Edition) [Bonus Tracks]"


def test_clean_album_title_handles_mixed_clauses():
    mixed = "Killers (1998 Remaster, Made in UK)"
    assert clean_album_title(mixed) == "Killers (1998 Remaster)"

    mixed_mould = "In Rainbows (Special Edition, has IFPI mould SID)"
    assert clean_album_title(mixed_mould) == "In Rainbows (Special Edition)"


def test_clean_album_title_with_explicit_disambiguation():
    disam = "FRANCE WE 833, Made in Germany"
    title = f"Unplugged ({disam})"
    assert clean_album_title(title, disambiguation=disam) == "Unplugged"

    title_sep = f"Unplugged - {disam}"
    assert clean_album_title(title_sep, disambiguation=disam) == "Unplugged"


def test_clean_album_title_preserves_leading_parentheses_and_fallbacks():
    assert clean_album_title("(What's the Story) Morning Glory?") == "(What's the Story) Morning Glory?"
    assert clean_album_title("(White Album)") == "(White Album)"
    assert clean_album_title("") == ""
    assert clean_album_title("   ") == ""


def test_inspect_disc_sanitizes_album_title_from_whipper_stdout(monkeypatch):
    sample_out = """
CDDB disc id: 800ac10a
MusicBrainz lookup URL https://musicbrainz.org/cdtoc/attach?toc=1+10+206687
Disc duration: 00:45:53.826, 10 audio tracks

Matching releases:

Artist  : Eric Clapton
Title   : Unplugged (FRANCE WE 833, Made in Germany, 936245024-2 WME, has IFPI mould SID)
URL     : https://musicbrainz.org/release/12345678-1234-1234-1234-123456789abc
Release : 12345678-1234-1234-1234-123456789abc
"""
    class MockInspectProc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdout = io.StringIO(sample_out.strip())
            self.returncode = 0
            self.pid = 9999
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("whipit.ripping.identify.drive_has_disc", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.ripping.identify.unmount_cdda_device", lambda *a, **kw: None)
    monkeypatch.setattr("subprocess.Popen", MockInspectProc)
    monkeypatch.setattr("whipit.audio.metadata.fetch_release_details", lambda *a, **kw: type("Details", (), {"tracks": [], "discogs_url": None})())

    res = inspect_disc("/dev/sr0")
    assert res.success is True
    assert res.title == "Unplugged"
    assert res.matching_releases[0].title == "Unplugged"


def test_workflow_sanitizes_album_title_from_whipper_tags(sample_manifest, temp_env, monkeypatch):
    coord = RipWorkflowCoordinator(device="/dev/sr0")
    coord.whipper_stage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("whipit.workflow.unmount_cdda_device", lambda dev: None)

    class MockRipResult:
        success = True
        cancelled = False
        partial = False
        device_path = "/dev/sr0"
        effective_offset = 6
        speed = None

    class MockEngine:
        def run_extraction(self, **kw):
            return MockRipResult()

    monkeypatch.setattr("whipit.workflow.RippingEngine", lambda **kw: MockEngine())
    monkeypatch.setattr("whipit.workflow.find_primary_cue", lambda d: d / "test.cue")
    monkeypatch.setattr("whipit.workflow.find_whipper_log", lambda d: d / "test.log")
    (coord.whipper_stage / "test.cue").touch()
    (coord.whipper_stage / "test.log").touch()

    monkeypatch.setattr("whipit.workflow.parse_cue_expected_tracks", lambda p: 1)
    monkeypatch.setattr(
        "whipit.workflow.parse_whipper_report",
        lambda p: ("No errors occurred", "All tracks accurately ripped", 1, 1),
    )

    track_file = coord.whipper_stage / "01.flac"
    track_file.touch()

    monkeypatch.setattr("whipit.workflow.validate_track_sequence", lambda d, cnt, **kw: (True, []))

    noisy_album_tag = "Unplugged (FRANCE WE 833, Made in Germany, 936245024-2 WME, has IFPI mould SID)"
    def mock_read_tag(f, tag):
        if tag == "ALBUM":
            return noisy_album_tag
        if tag in ("ARTIST", "ALBUMARTIST"):
            return "Eric Clapton"
        if tag == "TRACKNUMBER":
            return "1"
        if tag == "TITLE":
            return "Signe"
        return None

    monkeypatch.setattr("whipit.workflow.read_flac_tag", mock_read_tag)
    monkeypatch.setattr("whipit.workflow.verify_release_candidate", lambda *a, **kw: (True, []))
    monkeypatch.setattr("whipit.workflow.compare_trees", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.transcode_flacs_to_opus", lambda *a, **kw: True)
    monkeypatch.setattr("whipit.workflow.apply_replaygain", lambda d, ext: True)
    monkeypatch.setattr("whipit.workflow.verify_flac_integrity", lambda files: True)

    written_tags = []
    monkeypatch.setattr("whipit.workflow.write_flac_tags", lambda f, t: (written_tags.append(t), True)[1])

    res = coord.run_workflow()
    assert res == WorkflowResult.SUCCESS
    assert coord.manifest.metadata.album == "Unplugged"
    assert "Eric Clapton - Unplugged" in str(coord.manifest.paths.final_flac_dir)
    assert any(t.get("ALBUM") == "Unplugged" for t in written_tags)
