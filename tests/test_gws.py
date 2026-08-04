"""Tests for the GoogleWorkspace wrapper."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ctxsync.exceptions import ProviderError
from ctxsync.gws import (
    GWSAuthUnavailable,
    GoogleWorkspace,
    _resolve_worker_key,
)


def _clear_gws_env(monkeypatch):
    for var in (
        "GWS_WORKER_SESSION_KEY",
        "GWS_WORKER_KV_NAMESPACE",
        "GWS_WORKER_KV_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CF_ACCOUNT_ID",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_uses_env_key_when_set(monkeypatch):
    _clear_gws_env(monkeypatch)
    monkeypatch.setenv("GWS_WORKER_SESSION_KEY", "direct-key")
    assert _resolve_worker_key() == "direct-key"


def test_resolve_raises_when_nothing_available(monkeypatch):
    _clear_gws_env(monkeypatch)
    with pytest.raises(GWSAuthUnavailable):
        _resolve_worker_key()


def test_resolve_kv_fallback_requires_all_three_vars(monkeypatch):
    _clear_gws_env(monkeypatch)
    monkeypatch.setenv("GWS_WORKER_KV_NAMESPACE", "ns-id")
    # Missing CLOUDFLARE_API_TOKEN and CF_ACCOUNT_ID — should raise
    with pytest.raises(GWSAuthUnavailable):
        _resolve_worker_key()


def test_call_posts_json_rpc_with_x_session_key(monkeypatch):
    """The call builds a proper JSON-RPC envelope with the right auth header."""
    gws = GoogleWorkspace(session_key="test-key")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data.decode("utf-8") if req.data else None

        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"files": [{"id": "f-1"}]}}
        ).encode()
        return response

    with patch("urllib.request.urlopen", fake_urlopen):
        result = gws.call("drive", action="list")

    assert result == {"files": [{"id": "f-1"}]}
    assert "gws-worker" in captured["url"]
    assert captured["method"] == "POST"
    # Headers get title-cased by urllib
    assert captured["headers"]["X-session-key"] == "test-key"
    body = json.loads(captured["body"])
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "drive"
    assert body["params"]["arguments"] == {"action": "list"}


def test_call_raises_on_rpc_error(monkeypatch):
    gws = GoogleWorkspace(session_key="test-key")

    def fake_urlopen(req, timeout=None):
        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "Tool not found"},
            }
        ).encode()
        return response

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(ProviderError, match="Tool not found"):
            gws.call("bogus", action="nope")


def test_drive_upload_helper_maps_to_create(monkeypatch):
    gws = GoogleWorkspace(session_key="test-key")
    gws.call = MagicMock(return_value={"id": "new-file"})
    gws.drive_upload("note.md", "hi", folder_id="folder-1", mime_type="text/markdown")
    gws.call.assert_called_once_with(
        "drive",
        action="create",
        name="note.md",
        content="hi",
        mimeType="text/markdown",
        folderId="folder-1",
    )
