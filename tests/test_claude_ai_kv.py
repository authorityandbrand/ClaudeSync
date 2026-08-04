"""Tests for the claude.ai-kv provider's reliability features."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ctxsync.configmanager import InMemoryConfigManager
from ctxsync.exceptions import ProviderError
from ctxsync.providers.claude_ai_kv import ClaudeAIKVProvider


def _http_error(code, body=b'{"error":"bad"}'):
    from urllib.error import HTTPError

    return HTTPError(
        "https://claude.ai/api/test",
        code,
        f"HTTP {code}",
        {},
        MagicMock(read=lambda: body),
    )


def _ok_response(payload):
    """Fake response context manager returning ``payload`` as JSON."""
    response = MagicMock()
    response.__enter__ = lambda s: response
    response.__exit__ = lambda *a: None
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.headers = {}
    return response


def test_401_re_resolves_session_key_and_retries_once():
    """A 401 should clear the KV cache, re-resolve, and retry — exactly once."""
    provider = ClaudeAIKVProvider(config=InMemoryConfigManager(), session_key="stale")
    # Pre-populate the cache so the first request sends "stale".
    provider._resolved_session_key = "stale"

    resolutions: list[str] = []
    seen_bearers: list[str] = []
    call_count = {"n": 0}

    def fake_resolve(explicit):
        # The provider only calls resolve_session_key() when its cache is
        # empty. The first attempt uses the pre-populated "stale" cache; the
        # 401 handler clears it, so this fake fires exactly once — on retry.
        resolutions.append(explicit or "")
        return "fresh"

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        # Capture what Authorization header this attempt used.
        seen_bearers.append(dict(req.header_items()).get("Authorization", ""))
        if call_count["n"] == 1:
            raise _http_error(401)
        return _ok_response([{"uuid": "org-1", "name": "X", "capabilities": []}])

    with patch(
        "ctxsync.providers.claude_ai_kv.resolve_session_key",
        side_effect=fake_resolve,
    ):
        with patch(
            "ctxsync.http.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            result = provider._make_request("GET", "/test")

    assert call_count["n"] == 2, "expected exactly one retry after 401"
    assert seen_bearers[0] == "Bearer stale"
    assert seen_bearers[1] == "Bearer fresh"
    assert len(resolutions) == 1, "cache should force exactly one re-resolve"
    assert result == [{"uuid": "org-1", "name": "X", "capabilities": []}]


def test_401_after_re_resolve_still_raises():
    """If the retry also 401s, don't recurse forever — raise."""
    provider = ClaudeAIKVProvider(config=InMemoryConfigManager(), session_key="stale")
    provider._resolved_session_key = "stale"

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        raise _http_error(401)

    with patch(
        "ctxsync.providers.claude_ai_kv.resolve_session_key",
        return_value="also-stale",
    ):
        with patch(
            "ctxsync.http.urllib.request.urlopen", side_effect=fake_urlopen
        ):
            with pytest.raises(ProviderError, match="401"):
                provider._make_request("GET", "/test")

    assert call_count["n"] == 2, "should retry exactly once, no infinite loop"
