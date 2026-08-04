"""Tests for the GoogleDrive wrapper."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from ctxsync.exceptions import ProviderError
from ctxsync.gws import (
    GWSAuthUnavailable,
    GoogleDrive,
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


def _fake_urlopen(response_body):
    def fake(req, timeout=None):
        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps(response_body).encode()
        return response

    return fake


def test_call_posts_json_rpc_with_x_session_key(monkeypatch):
    """The call builds a proper JSON-RPC envelope with the right auth header."""
    drive = GoogleDrive(session_key="test-key")

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
        result = drive.call(action="list", folderId="folder-1")

    assert result == {"files": [{"id": "f-1"}]}
    assert "gws-worker" in captured["url"]
    assert captured["method"] == "POST"
    # urllib title-cases custom headers
    assert captured["headers"]["X-session-key"] == "test-key"
    body = json.loads(captured["body"])
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "drive"
    assert body["params"]["arguments"] == {"action": "list", "folderId": "folder-1"}


def test_call_raises_on_rpc_error(monkeypatch):
    drive = GoogleDrive(session_key="test-key")

    with patch(
        "urllib.request.urlopen",
        _fake_urlopen(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "Tool not found"},
            }
        ),
    ):
        with pytest.raises(ProviderError, match="Tool not found"):
            drive.call(action="bogus")


def test_upload_helper_maps_to_create():
    drive = GoogleDrive(session_key="test-key")
    drive.call = MagicMock(return_value={"id": "new-file"})
    drive.upload(
        "note.md", "hi", folder_id="folder-1", mime_type="text/markdown"
    )
    drive.call.assert_called_once_with(
        action="create",
        name="note.md",
        content="hi",
        mimeType="text/markdown",
        folderId="folder-1",
    )


def test_upload_binary_base64_encodes():
    drive = GoogleDrive(session_key="test-key")
    drive.call = MagicMock(return_value={"id": "bin-file"})
    payload = b"\x89PNG\r\n\x1a\n"  # PNG header bytes
    drive.upload_binary("logo.png", payload, folder_id="f", mime_type="image/png")
    drive.call.assert_called_once_with(
        action="create",
        name="logo.png",
        content=base64.b64encode(payload).decode("ascii"),
        contentEncoding="base64",
        mimeType="image/png",
        folderId="f",
    )


def test_folder_helper():
    drive = GoogleDrive(session_key="test-key")
    drive.call = MagicMock(return_value={"id": "new-folder"})
    drive.folder("New folder", parent_id="parent-1")
    drive.call.assert_called_once_with(
        action="folder", name="New folder", parentId="parent-1"
    )


def test_search_and_list_helpers():
    drive = GoogleDrive(session_key="test-key")
    drive.call = MagicMock(return_value={"files": []})
    drive.search("name contains 'x'", max_results=25)
    drive.call.assert_called_with(
        action="search", query="name contains 'x'", maxResults=25
    )
    drive.list_folder("f-1", max_results=10)
    drive.call.assert_called_with(action="list", folderId="f-1", maxResults=10)


def test_download_extracts_content_field():
    drive = GoogleDrive(session_key="test-key")
    drive.call = MagicMock(return_value={"content": "file body", "id": "f-1"})
    assert drive.download("f-1") == "file body"
