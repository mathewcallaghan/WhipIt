from pathlib import Path
import pytest
from whipit.models import AlbumMetadata, TrackInfo
from whipit.ui.gui.session import ActiveSession
from whipit.validation.flac import format_track_filename, write_flac_tags
from whipit.workflow import RipWorkflowCoordinator


def test_album_metadata_custom_fields():
    meta = AlbumMetadata(
        artist="Custom Artist",
        album="Custom Album",
        year="2026",
        genre="Alternative Rock",
        custom_track_titles={1: "Opening Track", 2: "Second Track"},
    )
    assert meta.year == "2026"
    assert meta.genre == "Alternative Rock"
    assert meta.custom_track_titles[1] == "Opening Track"
    assert meta.custom_track_titles[2] == "Second Track"


def test_format_track_filename():
    fname = format_track_filename(1, "Foo Fighters", "The Pretender")
    assert fname == "01. Foo Fighters - The Pretender.flac"

    # Verifies character sanitization and forward slash replacement
    fname_slash = format_track_filename(4, "AC/DC", "Back In Black?")
    assert fname_slash == "04. AC-DC - Back In Black_.flac"


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
