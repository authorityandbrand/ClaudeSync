"""Google Workspace persistent-storage wrapper (Drive, Docs, Sheets).

Two-mode design that matches how this ecosystem is deployed:

**Mode A — REST (portable)**
    Set ``GWS_WORKER_SESSION_KEY`` in the environment. The wrapper POSTs
    JSON-RPC calls to ``https://gws-worker.authorityandbrand.workers.dev/mcp``
    with ``X-Session-Key: <your key>``. Works from any Python process. The
    session key is a per-account secret held by the worker (not the
    claude.ai session cookie — different auth domain). Rotate it the same
    way the ``browser-auth-worker`` rotates the claude.ai cookie: a small
    companion worker that refreshes and writes to a Cloudflare KV entry
    you'd read here.

**Mode B — MCP passthrough (Claude Code only)**
    If ``GWS_WORKER_SESSION_KEY`` isn't set and you're inside a Claude Code
    session with the ``mcp__GWS_Worker__*`` tools loaded, you can call the
    Google Workspace ecosystem through those directly — the wrapper isn't
    involved. Prefer Mode A when the same code needs to run from cron/CI.

**Why not fetch the key from Cloudflare KV like claude.ai?**
    The gws-worker's ``SESSION_KEY`` secret is a Cloudflare Worker
    ``secret_text`` binding — opaque to the CF API, so no read path from
    outside the worker. If you set up a rotation worker that publishes the
    current value to a KV entry you control, point
    ``GWS_WORKER_KV_NAMESPACE`` / ``GWS_WORKER_KV_KEY`` at it and this
    module will pull from there. Not the default — most callers just set
    ``GWS_WORKER_SESSION_KEY`` directly.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from .exceptions import ProviderError

GWS_WORKER_URL = "https://gws-worker.authorityandbrand.workers.dev/mcp"


class GWSAuthUnavailable(ProviderError):
    """Raised when no GWS worker session key can be resolved."""


def _resolve_worker_key() -> str:
    """Return a valid worker session key or raise :class:`GWSAuthUnavailable`."""
    env_key = os.environ.get("GWS_WORKER_SESSION_KEY")
    if env_key:
        return env_key

    # Optional KV fallback — off by default. If a rotation worker publishes
    # the current key to a KV entry, point these env vars at it.
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
                f"GWS_WORKER_KV_NAMESPACE lookup {kv_namespace}/{kv_key}: "
                f"HTTP {e.code}"
            ) from e

    raise GWSAuthUnavailable(
        "No GWS worker session key. Set GWS_WORKER_SESSION_KEY, or point "
        "GWS_WORKER_KV_NAMESPACE at a KV entry a rotation worker publishes. "
        "Inside a Claude Code session, the mcp__GWS_Worker__* tools work "
        "without any additional setup."
    )


class GoogleWorkspace:
    """JSON-RPC client for the Anthropic-adjacent gws-worker MCP endpoint.

    The worker exposes ~155 tools grouped as: drive, gmail, docs, sheets,
    slides, calendar, tasks, contacts, chat, forms, apps_script,
    gemini_ai, vision, web_search, nlm. Call any of them via
    :meth:`call` with the tool name and its ``action`` + params::

        gws = GoogleWorkspace()
        gws.call("drive", action="search", query="name contains 'brief'")
        gws.call("docs", action="get", documentId="1abc…")

    Convenience helpers (:meth:`drive_upload`, :meth:`drive_list`,
    :meth:`docs_get`) wrap the most common calls with typed args.
    """

    def __init__(self, session_key: Optional[str] = None, url: str = GWS_WORKER_URL):
        self._session_key = session_key or _resolve_worker_key()
        self._url = url
        self._request_id = 0

    def call(self, tool: str, **params: Any) -> Any:
        """Call a gws-worker tool via JSON-RPC ``tools/call``.

        ``tool`` is one of the grouped tool names ("drive", "gmail", …).
        Remaining kwargs become the tool's ``arguments`` body.
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": params},
        }
        req = urllib.request.Request(
            self._url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Session-Key", self._session_key)
        req.add_header("User-Agent", "ctxsync-gws/0.1")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise ProviderError(f"gws-worker {e.code}: {body[:400]}") from e

        if "error" in body:
            raise ProviderError(f"gws-worker rpc error: {body['error']}")
        return body.get("result")

    # --------------------------------------------------- convenience: Drive

    def drive_list(self, folder_id: Optional[str] = None, max_results: int = 50):
        params = {"action": "list", "maxResults": max_results}
        if folder_id:
            params["folderId"] = folder_id
        return self.call("drive", **params)

    def drive_search(self, query: str, max_results: int = 50):
        return self.call(
            "drive", action="search", query=query, maxResults=max_results
        )

    def drive_upload(
        self,
        name: str,
        content: str,
        folder_id: Optional[str] = None,
        mime_type: str = "text/plain",
    ):
        params = {
            "action": "create",
            "name": name,
            "content": content,
            "mimeType": mime_type,
        }
        if folder_id:
            params["folderId"] = folder_id
        return self.call("drive", **params)

    def drive_get(self, file_id: str):
        return self.call("drive", action="get", fileId=file_id)

    # ---------------------------------------------------- convenience: Docs

    def docs_get(self, document_id: str):
        return self.call("docs", action="get", documentId=document_id)

    def docs_create(self, title: str):
        return self.call("docs", action="create", title=title)

    # --------------------------------------------------- convenience: Sheets

    def sheet_read(self, spreadsheet_id: str, range_: str):
        return self.call(
            "sheets",
            action="read",
            spreadsheetId=spreadsheet_id,
            range=range_,
        )


def gws() -> GoogleWorkspace:
    """One-line entry point mirroring :func:`ctxsync.easy.claude_projects`.

    >>> from ctxsync.gws import gws
    >>> g = gws()
    >>> g.drive_search("name contains 'brief'")
    """
    return GoogleWorkspace()
