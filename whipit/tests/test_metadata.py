import json
import threading
import urllib.request
import pytest
from whipit.audio.metadata import (
    fetch_cover_art_from_caa,
    fetch_release_details,
    is_valid_discogs_url,
)
from whipit.audio.opus import transcode_flacs_to_opus


def test_musicbrainz_user_agent_format(monkeypatch):
    captured_headers = {}

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b"{}"

    def mock_urlopen(req, timeout=8.0):
        if hasattr(req, "headers"):
            captured_headers.update(req.headers)
        return MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)

    fetch_release_details("a0000000-0000-0000-0000-000000000001")
    ua = captured_headers.get("User-agent") or captured_headers.get("User-Agent")
    assert ua is not None
    assert "https://github.com/mathewcallaghan/WhipIt" in ua


def test_caa_cover_fetch_prefers_full_resolution(temp_env, monkeypatch):
    requested_urls = []

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return b"\xff\xd8\xff\xe0\x00\x10JFIF"

    def mock_urlopen(req, timeout=12.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        requested_urls.append(url)
        return MockResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    test_mbid = "12345678-1234-1234-1234-1234567890ab"
    art_path = fetch_cover_art_from_caa(test_mbid)
    assert art_path is not None
    assert art_path.exists()
    assert art_path.suffix == ".jpg"
    assert "_front" in art_path.name
    assert requested_urls[0] == f"https://coverartarchive.org/release/{test_mbid}/front"


def test_caa_cover_fetch_rejects_invalid_mbid(temp_env):
    assert fetch_cover_art_from_caa("../../etc/passwd") is None
    assert fetch_cover_art_from_caa("not-a-valid-uuid") is None
    assert fetch_cover_art_from_caa("") is None


def test_caa_cover_fetch_rejects_non_image_payload(temp_env, monkeypatch):
    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return b"<html><body>Not an image</body></html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=12.0: MockResponse())
    assert fetch_cover_art_from_caa("12345678-1234-1234-1234-1234567890ab") is None


def test_caa_cover_fetch_rejects_oversized_payload(temp_env, monkeypatch):
    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return b"\xff\xd8\xff" + b"0" * (16 * 1024 * 1024)

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=12.0: MockResponse())
    assert fetch_cover_art_from_caa("12345678-1234-1234-1234-1234567890ab") is None


def test_is_valid_discogs_url_enforces_https_and_domain():
    assert is_valid_discogs_url("https://www.discogs.com/release/123456") is True
    assert is_valid_discogs_url("https://discogs.com/master/789") is True

    assert is_valid_discogs_url("http://www.discogs.com/release/123456") is False
    assert is_valid_discogs_url("smb://attacker/discogs.com") is False
    assert is_valid_discogs_url("file:///etc/passwd") is False
    assert is_valid_discogs_url("https://evil-discogs.com/phish") is False
    assert is_valid_discogs_url("https://discogs.com.attacker.com/") is False
    assert is_valid_discogs_url(r"https://evil.example\@discogs.com/x") is False
    assert is_valid_discogs_url("https://user:pass@discogs.com/release/1") is False
    assert is_valid_discogs_url(None) is False


def test_fetch_release_details_rejects_untrusted_discogs_relation(monkeypatch):
    mock_payload = {
        "media": [{"tracks": []}],
        "relations": [
            {"url": {"resource": "smb://malicious-host/discogs.com"}},
            {"url": {"resource": "http://insecure-discogs.com/123"}},
        ],
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps(mock_payload).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=8.0: MockResponse())
    monkeypatch.setattr("time.sleep", lambda s: None)

    details = fetch_release_details("a0000000-0000-0000-0000-000000000001")
    assert details.discogs_url != "smb://malicious-host/discogs.com"


def test_fetch_release_details_rejects_invalid_mbid():
    details = fetch_release_details("invalid-mbid; injection")
    assert len(details.tracks) == 0
    assert details.discogs_url is None

    details2 = fetch_release_details("../../etc/passwd")
    assert len(details2.tracks) == 0
    assert details2.discogs_url is None


def test_fetch_release_details_handles_malformed_tracks(monkeypatch):
    mock_payload = {
        "media": [{
            "tracks": [
                None,
                "not-a-dict",
                {"position": "bad-pos", "length": "invalid-length", "title": "Track 1"},
                {"position": 2, "length": 185000, "title": "Track 2"},
            ]
        }],
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return json.dumps(mock_payload).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=8.0: MockResponse())
    monkeypatch.setattr("time.sleep", lambda s: None)

    details = fetch_release_details("a0000000-0000-0000-0000-000000000001")
    assert len(details.tracks) == 2
    assert details.tracks[0].duration is None
    assert details.tracks[1].duration == "03:05"


def test_fetch_release_details_handles_corrupt_relations_and_release_group(monkeypatch):
    mock_payload = {
        "media": None,
        "relations": [None, "invalid-relation", {"url": None}, {"url": "not-a-dict"}],
        "release-group": "not-a-dict",
    }

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return json.dumps(mock_payload).encode("utf-8")

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=8.0: MockResponse())
    monkeypatch.setattr("time.sleep", lambda s: None)

    details = fetch_release_details("a0000000-0000-0000-0000-000000000001")
    assert len(details.tracks) == 0
    assert details.discogs_url is None


def test_fetch_release_details_rejects_unvalidated_release_group_id(monkeypatch):
    mock_payload = {
        "media": [],
        "relations": [],
        "release-group": {"id": "../lookup/injection"},
    }

    requested_urls = []

    class MockResponse:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return json.dumps(mock_payload).encode("utf-8")

    def mock_urlopen(req, timeout=8.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        requested_urls.append(url)
        return MockResponse()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)

    details = fetch_release_details("a0000000-0000-0000-0000-000000000001")
    assert not any("injection" in u for u in requested_urls)
    assert details.discogs_url is None


def test_mb_throttle_enforces_minimum_interval(monkeypatch):
    from whipit.audio import metadata

    sleeps = []
    monkeypatch.setattr(metadata.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(metadata, "_mb_last", 100.0)
    monkeypatch.setattr(metadata.time, "monotonic", lambda: 100.2)

    metadata._mb_throttle(1.1)
    assert len(sleeps) == 1
    assert abs(sleeps[0] - 0.9) < 0.05


def test_opus_transcode_catches_future_exceptions(tmp_path, monkeypatch):
    flac = tmp_path / "01.flac"
    flac.touch()

    def mock_encode(*args, **kwargs):
        raise RuntimeError("Simulated unexpected disk error")

    monkeypatch.setattr("whipit.audio.opus._encode_single_track", mock_encode)

    ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", cover_art=None)
    assert ok is False


def test_opus_transcode_immediate_cancellation(tmp_path, monkeypatch):
    flac = tmp_path / "01.flac"
    flac.touch()
    cancel_evt = threading.Event()
    cancel_evt.set()

    ok = transcode_flacs_to_opus([flac], tmp_path, tmp_path / "opus", cover_art=None, cancel_event=cancel_evt)
    assert ok is False
