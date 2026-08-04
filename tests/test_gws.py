"""Tests for the native Google Drive wrapper."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from ctxsync.exceptions import ProviderError
from ctxsync.gws import (
    DEFAULT_TOKEN_URL,
    GWSAuthUnavailable,
    GeminiGWSDrive,
    GoogleDrive,
    _TokenSource,
)


def _fake_urlopen(response_body, content_type="application/json"):
    def fake(req, timeout=None):
        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        if isinstance(response_body, (bytes, bytearray)):
            response.read.return_value = bytes(response_body)
        elif isinstance(response_body, str):
            response.read.return_value = response_body.encode("utf-8")
        else:
            response.read.return_value = json.dumps(response_body).encode()
        response.headers = {"Content-Type": content_type}
        return response

    return fake


# ---------------------------------------------------------------- token source


def test_token_source_caches_within_window():
    src = _TokenSource(DEFAULT_TOKEN_URL)
    fake = MagicMock(side_effect=_fake_urlopen({"access_token": "t1", "expires_in": 3600}))
    with patch("urllib.request.urlopen", fake):
        assert src.get() == "t1"
        # A second call inside the safety window returns the same value
        assert src.get() == "t1"
        assert fake.call_count == 1


def test_token_source_refetches_after_expiry():
    src = _TokenSource(DEFAULT_TOKEN_URL)
    # First fetch: expires almost immediately (well inside safety window)
    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"access_token": "t1", "expires_in": 60}),
    ):
        assert src.get() == "t1"

    # Simulate the safety window elapsing
    src._expires_at = time.time() - 1

    with patch(
        "urllib.request.urlopen",
        _fake_urlopen({"access_token": "t2", "expires_in": 3600}),
    ):
        assert src.get() == "t2"


def test_token_source_raises_when_response_missing_token():
    src = _TokenSource(DEFAULT_TOKEN_URL)
    with patch(
        "urllib.request.urlopen", _fake_urlopen({"unrelated": "value"})
    ):
        with pytest.raises(GWSAuthUnavailable):
            src.get()


# ------------------------------------------------------------------ drive core


def test_request_uses_explicit_token_when_provided():
    drive = GoogleDrive(access_token="explicit-token")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps({"files": []}).encode()
        response.headers = {"Content-Type": "application/json"}
        return response

    with patch("urllib.request.urlopen", fake_urlopen):
        drive.request("GET", "/files", params={"pageSize": 5})

    assert captured["headers"]["Authorization"] == "Bearer explicit-token"
    assert "pageSize=5" in captured["url"]
    assert captured["url"].startswith("https://www.googleapis.com/drive/v3/files")


def test_request_raises_provider_error_on_http_error():
    drive = GoogleDrive(access_token="explicit-token")
    from urllib.error import HTTPError

    def fake(req, timeout=None):
        raise HTTPError(req.full_url, 401, "Unauthorized", {}, MagicMock(read=lambda: b'{"error":"unauth"}'))

    with patch("urllib.request.urlopen", fake):
        with pytest.raises(ProviderError, match="401"):
            drive.request("GET", "/files")


def test_search_builds_query_params():
    drive = GoogleDrive(access_token="t")
    drive.request = MagicMock(return_value={"files": []})
    drive.search("name contains 'x'", page_size=25)
    drive.request.assert_called_once()
    _, kwargs = drive.request.call_args
    assert kwargs["params"]["q"] == "name contains 'x'"
    assert kwargs["params"]["pageSize"] == 25


def test_list_folder_wraps_search_with_parents_filter():
    drive = GoogleDrive(access_token="t")
    drive.search = MagicMock(return_value={"files": []})
    drive.list_folder("folder-abc", page_size=10)
    query, kwargs = drive.search.call_args[0], drive.search.call_args[1]
    assert "'folder-abc' in parents" in query[0]
    assert "trashed = false" in query[0]
    assert kwargs["page_size"] == 10


def test_folder_creates_with_folder_mime_type():
    drive = GoogleDrive(access_token="t")
    drive.request = MagicMock(return_value={"id": "new-folder"})
    drive.folder("New folder", parent_id="parent-1")
    _, kwargs = drive.request.call_args
    assert kwargs["json_body"]["mimeType"] == "application/vnd.google-apps.folder"
    assert kwargs["json_body"]["parents"] == ["parent-1"]


def test_upload_binary_posts_multipart():
    drive = GoogleDrive(access_token="t")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        response = MagicMock()
        response.__enter__ = lambda s: response
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps({"id": "new-file"}).encode()
        return response

    with patch("urllib.request.urlopen", fake_urlopen):
        drive.upload_binary(
            "logo.png", b"\x89PNG", folder_id="f-1", mime_type="image/png"
        )

    assert captured["method"] == "POST"
    assert "uploadType=multipart" in captured["url"]
    assert "multipart/related" in captured["headers"]["Content-type"]
    assert b'"name": "logo.png"' in captured["body"]
    assert b"\x89PNG" in captured["body"]


def test_move_removes_current_parents():
    drive = GoogleDrive(access_token="t")
    responses = [
        {"parents": ["old-parent-1", "old-parent-2"]},  # from .get
        {"id": "f-1", "parents": ["new-parent"]},         # from .request PATCH
    ]
    drive.request = MagicMock(side_effect=responses)
    drive.move("f-1", "new-parent")
    # Second call is the PATCH — grab its params and verify removeParents lists
    # both old parents joined by comma.
    patch_call = drive.request.call_args_list[1]
    assert patch_call.kwargs["params"]["removeParents"] == "old-parent-1,old-parent-2"
    assert patch_call.kwargs["params"]["addParents"] == "new-parent"


def test_delete_calls_delete_verb():
    drive = GoogleDrive(access_token="t")
    drive.request = MagicMock(return_value=None)
    drive.delete("file-1")
    drive.request.assert_called_once_with("DELETE", "/files/file-1")


def test_trash_patches_trashed_true():
    drive = GoogleDrive(access_token="t")
    drive.request = MagicMock(return_value={"id": "f-1", "trashed": True})
    drive.trash("f-1")
    _, kwargs = drive.request.call_args
    assert kwargs["json_body"] == {"trashed": True}


def test_json_body_and_raw_body_mutually_exclusive():
    drive = GoogleDrive(access_token="t")
    with pytest.raises(ValueError):
        drive.request("POST", "/x", json_body={"a": 1}, raw_body=b"raw")


# ------------------------------------------------------------ Gemini MCP trash


def test_gemini_trash_sends_trashed_true():
    """Regression for the placeholder ``starred=False`` bug at gws.py:242."""
    drive = GeminiGWSDrive(key="mock-key")
    drive._call = MagicMock(return_value={"id": "f-1", "trashed": True})
    drive.trash("f-1")
    # Should be called as ("update", fileId="f-1", trashed=True) — not starred.
    args, kwargs = drive._call.call_args
    assert args == ("update",)
    assert kwargs == {"fileId": "f-1", "trashed": True}
