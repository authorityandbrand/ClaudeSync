"""Tests for the shared retry helper in ``ctxsync.http``."""

from __future__ import annotations

import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from ctxsync.http import urlopen_with_retry


def _http_error(code, headers=None):
    from urllib.error import HTTPError

    return HTTPError(
        "http://example.test/",
        code,
        f"HTTP {code}",
        headers or {},
        MagicMock(read=lambda: b""),
    )


def test_retry_on_429_honors_retry_after_and_succeeds():
    """A 429 with Retry-After: 1 is retried; the second attempt returns."""
    ok = MagicMock()
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, headers={"Retry-After": "1"})
        return ok

    sleeps: list[float] = []
    req = urllib.request.Request("http://example.test/")
    with patch("ctxsync.http.urllib.request.urlopen", side_effect=fake_urlopen):
        result = urlopen_with_retry(req, sleeper=sleeps.append)

    assert result is ok
    assert len(calls) == 2
    assert sleeps == [1.0], "Retry-After: 1 should sleep exactly 1s"


def test_three_failures_raises_last_error():
    """After 3 failing attempts the HTTPError surfaces unchanged."""

    def fake_urlopen(req, timeout=None):
        raise _http_error(503)

    req = urllib.request.Request("http://example.test/")
    sleeps: list[float] = []
    with patch("ctxsync.http.urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(Exception) as exc:
            urlopen_with_retry(req, sleeper=sleeps.append)

    # Should have retried the two intermediate times with exponential backoff.
    assert sleeps == [1.0, 2.0]
    from urllib.error import HTTPError

    assert isinstance(exc.value, HTTPError)
    assert exc.value.code == 503


def test_non_retryable_status_raises_immediately():
    """A 400 or 401 is a real bug — no retry, no sleep."""

    def fake_urlopen(req, timeout=None):
        raise _http_error(400)

    req = urllib.request.Request("http://example.test/")
    sleeps: list[float] = []
    with patch("ctxsync.http.urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(Exception):
            urlopen_with_retry(req, sleeper=sleeps.append)
    assert sleeps == []
