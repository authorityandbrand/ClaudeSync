"""Google Drive persistent-storage wrapper.

Second backend for :mod:`ctxsync.easy` — when a file needs to survive
claude.ai Project turnover, drop it in Drive instead of (or alongside)
the KB. Scope is intentionally narrow: Drive only. No Gmail, no Docs,
no Sheets, no Calendar — if you need those, use the MCP tools directly.

Wire under the gws-worker MCP endpoint so calls run under your
Google-authenticated worker rather than requiring the caller to hold a
Google OAuth refresh token. The worker fronts the Drive REST API.

Auth resolution for the worker's own session-key:

1. ``GWS_WORKER_SESSION_KEY`` env var (direct)
2. ``GWS_WORKER_KV_NAMESPACE`` / ``GWS_WORKER_KV_KEY`` on Cloudflare KV
   for parity with how ``browser-auth-worker`` rotates the claude.ai key
3. Raises :class:`GWSAuthUnavailable` if neither is set. Inside a Claude
   Code session, the ``mcp__GWS_Worker__drive`` tool works without any
   additional setup and is the recommended path there.

Usage::

    from ctxsync.gws import drive
    d = drive()
    d.search("name contains 'brief'")
    d.upload("notes.md", "# hi", folder_id="…")
    d.download("<file-id>")            # returns str content
    d.folder("<parent-id>", "New folder")
    d.delete("<file-id>")
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from .exceptions import ProviderError

GWS_WORKER_URL = "https://gws-worker.authorityandbrand.workers.dev/mcp"


class GWSAuthUnavailable(ProviderError):
    """Raised when no gws-worker session key can be resolved."""


def _resolve_worker_key() -> str:
    env_key = os.environ.get("GWS_WORKER_SESSION_KEY")
    if env_key:
        return env_key

    kv_namespace = os.environ.get("GWS_WORKER_KV_NAMESPACE")
    kv_key = os.environ.get("GWS_WORKER_KV_KEY", "current_gws_worker_key")
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_account = os.environ.get("CF_ACCOUNT_ID")
    if kv_namespace and cf_token and cf_account:
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/{cf_account}"
            f"/storage/kv/namespaces/{kv_namespace}/values/{kv_key}"
        )
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {cf_token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                value = r.read().decode("utf-8").strip()
                if value:
                    return value
        except urllib.error.HTTPError as e:
            raise GWSAuthUnavailable(
                f"KV lookup {kv_namespace}/{kv_key}: HTTP {e.code}"
            ) from e

    raise GWSAuthUnavailable(
        "No GWS worker session key. Set GWS_WORKER_SESSION_KEY, or point "
        "GWS_WORKER_KV_NAMESPACE at a KV entry a rotation worker publishes. "
        "Inside Claude Code, the mcp__GWS_Worker__drive tool works with no "
        "additional setup."
    )


class GoogleDrive:
    """Thin JSON-RPC client that only exposes Drive.

    Methods return the same dict shape the underlying Drive REST API
    returns (worker passes it through). See the ``drive`` action list in
    the gws-worker MCP tool schema for the full parameter set behind
    :meth:`call`; the named helpers below cover the common ops.
    """

    def __init__(self, session_key: Optional[str] = None, url: str = GWS_WORKER_URL):
        self._session_key = session_key or _resolve_worker_key()
        self._url = url
        self._request_id = 0

    # --------------------------------------------------------- transport

    def call(self, **params: Any) -> Any:
        """Invoke the worker's ``drive`` tool with the given params.

        Escape hatch for actions the named helpers don't cover
        (permissions, comments, revisions, labels, activity, etc.).
        Every helper below is a thin wrapper over this.
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": "drive", "arguments": params},
        }
        req = urllib.request.Request(
            self._url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Session-Key", self._session_key)
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"gws-worker {e.code}: {e.read().decode('utf-8', 'replace')[:400]}"
            ) from e

        if "error" in body:
            raise ProviderError(f"gws-worker rpc error: {body['error']}")
        return body.get("result")

    # -------------------------------------------------- browsing / search

    def list_folder(self, folder_id: str, max_results: int = 50):
        """List direct children of a folder (files + subfolders)."""
        return self.call(action="list", folderId=folder_id, maxResults=max_results)

    def search(self, query: str, max_results: int = 50):
        """Drive search — pass a Drive query string.

        Examples:
            ``d.search("name contains 'brief' and trashed = false")``
            ``d.search("mimeType = 'application/pdf'")``
        """
        return self.call(action="search", query=query, maxResults=max_results)

    def get(self, file_id: str):
        """Metadata + inline content for a file."""
        return self.call(action="get", fileId=file_id)

    def metadata(self, file_id: str):
        """Full metadata (permissions, owners, checksums)."""
        return self.call(action="metadata", fileId=file_id)

    # ----------------------------------------------------- upload / write

    def upload(
        self,
        name: str,
        content: str,
        folder_id: Optional[str] = None,
        mime_type: str = "text/plain",
    ):
        """Create a new text file. Use :meth:`upload_binary` for bytes."""
        params: dict = {
            "action": "create",
            "name": name,
            "content": content,
            "mimeType": mime_type,
        }
        if folder_id:
            params["folderId"] = folder_id
        return self.call(**params)

    def upload_binary(
        self,
        name: str,
        data: bytes,
        folder_id: Optional[str] = None,
        mime_type: str = "application/octet-stream",
    ):
        """Upload binary data (PDFs, images, archives) as base64."""
        params: dict = {
            "action": "create",
            "name": name,
            "content": base64.b64encode(data).decode("ascii"),
            "contentEncoding": "base64",
            "mimeType": mime_type,
        }
        if folder_id:
            params["folderId"] = folder_id
        return self.call(**params)

    def folder(self, name: str, parent_id: Optional[str] = None):
        """Create a folder. Returns the new folder's metadata."""
        params: dict = {"action": "folder", "name": name}
        if parent_id:
            params["parentId"] = parent_id
        return self.call(**params)

    # ----------------------------------------------------------- download

    def download(self, file_id: str) -> str:
        """Return the file's text content (for text files / exports)."""
        result = self.call(action="get", fileId=file_id)
        # Worker returns {content: "...", ...} for text files
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return str(result)

    def download_url(self, file_id: str):
        """Return a signed URL / temporary download endpoint."""
        return self.call(action="download", fileId=file_id)

    def export(self, file_id: str, mime_type: str):
        """Export a Google-native file (Doc/Sheet/Slide) as another format."""
        return self.call(action="export", fileId=file_id, mimeType=mime_type)

    # ----------------------------------------------------- move / delete

    def move(self, file_id: str, target_folder_id: str):
        return self.call(
            action="move", fileId=file_id, targetFolderId=target_folder_id
        )

    def rename(self, file_id: str, new_name: str):
        return self.call(action="update", fileId=file_id, name=new_name)

    def delete(self, file_id: str):
        """PERMANENT delete. Trash it via ``update`` if you want reversible."""
        return self.call(action="delete", fileId=file_id)


def drive() -> GoogleDrive:
    """One-line entry point mirroring :func:`ctxsync.easy.claude_projects`.

    >>> from ctxsync.gws import drive
    >>> d = drive()
    >>> d.search("name contains 'brief' and trashed = false")
    """
    return GoogleDrive()


# Backwards-compat alias in case anything's already imported the old name.
GoogleWorkspace = GoogleDrive
gws = drive
