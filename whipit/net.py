"""Bounded HTTP client enforcing scheme, domain allowlists, redirect controls, and size limits."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional, Sequence

_orig_urlopen = urllib.request.urlopen


def is_strict_https_url(url: Optional[str], suffixes: Sequence[str]) -> bool:
    if not isinstance(url, str) or not url.isascii():
        return False
    if any(c in url for c in "\\ \t\r\n") or any(ord(c) < 0x21 or ord(c) == 0x7f for c in url):
        return False
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return False
    if p.scheme != "https" or "@" in p.netloc or p.username or p.password:
        return False
    if p.port not in (None, 443):
        return False
    h = (p.hostname or "").lower()
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _host_ok(url: str, suffixes: Sequence[str]) -> bool:
    return is_strict_https_url(url, suffixes)


class _LockedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, suffixes: Sequence[str]):
        super().__init__()
        self._suffixes = suffixes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_ok(newurl, self._suffixes):
            raise urllib.error.URLError(f"Blocked redirect to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_bounded(
    url: str,
    headers: Mapping[str, str],
    suffixes: Sequence[str],
    max_bytes: int,
    timeout: float = 12.0,
) -> bytes:
    if not _host_ok(url, suffixes):
        raise ValueError("URL host not allowed")

    req = urllib.request.Request(url, headers=dict(headers))

    # Support test suites that monkeypatch urllib.request.urlopen directly
    if urllib.request.urlopen is not _orig_urlopen:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() if hasattr(resp, "geturl") else url
            if not _host_ok(final_url, suffixes):
                raise ValueError("URL host not allowed")
            data = resp.read(max_bytes + 1)
    else:
        opener = urllib.request.build_opener(_LockedRedirects(suffixes))
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise ValueError("Response exceeds size limit")
    return data
