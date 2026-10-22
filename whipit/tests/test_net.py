import urllib.error
import pytest
from whipit.net import _host_ok, _LockedRedirects, fetch_bounded, is_strict_https_url


def test_host_ok_validates_https_and_domains():
    suffixes = ("github.com", "githubusercontent.com")
    assert _host_ok("https://github.com/foo", suffixes) is True
    assert _host_ok("https://api.github.com/foo", suffixes) is True
    assert _host_ok("https://objects.githubusercontent.com/bar", suffixes) is True
    assert _host_ok("http://github.com/foo", suffixes) is False
    assert _host_ok("https://evil-github.com/foo", suffixes) is False
    assert _host_ok("https://github.com.attacker.com/foo", suffixes) is False
    assert _host_ok("file:///etc/passwd", suffixes) is False
    assert _host_ok(None, suffixes) is False


def test_is_strict_https_url_rejects_tricks_and_non_ascii():
    suffixes = ("discogs.com", "musicbrainz.org")
    assert is_strict_https_url("https://www.discogs.com/release/123", suffixes) is True
    assert is_strict_https_url("https://musicbrainz.org/release/abc", suffixes) is True
    assert is_strict_https_url("https://discogs.com:443/release/1", suffixes) is True

    # Userinfo / backslash parser confusion
    assert is_strict_https_url(r"https://evil.example\@discogs.com/x", suffixes) is False
    assert is_strict_https_url("https://user:pass@discogs.com/release/1", suffixes) is False
    assert is_strict_https_url("https://discogs.com:8080/release/1", suffixes) is False
    assert is_strict_https_url("https://discogs.com/test\r\n", suffixes) is False
    assert is_strict_https_url("https://discogs.com/test space", suffixes) is False
    assert is_strict_https_url("https://dіscogs.com/release/1", suffixes) is False
    assert is_strict_https_url(None, suffixes) is False


def test_locked_redirects_blocks_unauthorized_redirect():
    handler = _LockedRedirects(("github.com",))
    with pytest.raises(urllib.error.URLError, match="Blocked redirect"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.com/payload")


def test_fetch_bounded_rejects_unauthorized_host():
    with pytest.raises(ValueError, match="URL host not allowed"):
        fetch_bounded("https://evil.com/test", {}, ("github.com",), 1000)


def test_fetch_bounded_rejects_oversized_payload(monkeypatch):
    class MockResponse:
        def __init__(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, n=-1):
            return b"A" * 2000

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=12.0: MockResponse())
    with pytest.raises(ValueError, match="exceeds size limit"):
        fetch_bounded("https://github.com/test", {}, ("github.com",), max_bytes=1000)
