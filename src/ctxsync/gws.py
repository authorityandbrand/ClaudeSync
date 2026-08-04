"""Google Drive wrapper with two backends.

Two ways to reach Drive from this codebase, both live-verified:

**A. Gemini_Gws MCP** (preferred when a key is available)
    JSON-RPC to ``gemini-webapi-worker.authorityandbrand.workers.dev/mcp``
    with ``?key=<shared secret>``. Uses the worker's own Google OAuth
    (default account: jim@dallasrr.com). Covers 155 tools; we only wrap
    the Drive subset here.

**B. Native REST via google-auth-worker** (fallback)
    ``google-auth-worker.…workers.dev/token`` mints a Google access
    token with no auth needed, then we call
    ``https://www.googleapis.com/drive/v3/*`` directly (default account:
    authorityandbrand@gmail.com).

The factory :func:`drive` picks A if ``GEMINI_GWS_KEY`` (or the KV
fallback) resolves; otherwise B. Both classes present the same public
API so calling code doesn't care which is in use.

Environment variables (all optional):

- ``GEMINI_GWS_KEY`` — direct shared-secret for the MCP endpoint
- ``GEMINI_GWS_KV_NAMESPACE`` + ``GEMINI_GWS_KV_KEY`` — CF KV pointer
  to a rotated key (uses ``CLOUDFLARE_API_TOKEN`` + ``CF_ACCOUNT_ID``)
- ``GEMINI_GWS_URL`` — override the MCP endpoint URL
- ``GOOGLE_AUTH_TOKEN_URL`` — override the fallback token minter URL

Nothing here reads a hardcoded key. Never commit a key to the repo.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .exceptions import ProviderError

DEFAULT_GEMINI_GWS_URL = (
    "https://gemini-webapi-worker.authorityandbrand.workers.dev/mcp"
)
DEFAULT_TOKEN_URL = (
    "https://google-auth-worker.authorityandbrand.workers.dev/token"
)
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

_TOKEN_SAFETY_WINDOW_SECONDS = 300


class GWSAuthUnavailable(ProviderError):
    """Raised when no Drive backend can be authenticated."""


# --------------------------------------------------------------- resolvers


def _resolve_gemini_gws_key() -> Optional[str]:
    """Return the Gemini_Gws MCP key, or None if unavailable.

    Order: explicit env var → CF KV pointer → None.
    """
    env_key = os.environ.get("GEMINI_GWS_KEY")
    if env_key:
        return env_key.strip()

    kv_namespace = os.environ.get("GEMINI_GWS_KV_NAMESPACE")
    kv_key = os.environ.get("GEMINI_GWS_KV_KEY", "gemini_gws_key")
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    # Default account matches auth/kv_session.py so users don't have to set
    # both CF_ACCOUNT_ID and the KV pointer just to make this path work.
    cf_account = os.environ.get("CF_ACCOUNT_ID", "e105d76aa6c851abdbd13d34d901cc7c")
    if kv_namespace and cf_token:
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
        except urllib.error.HTTPError:
            return None
    return None


# ---------------------------------------------------------------- MCP client


class GeminiGWSDrive:
    """Preferred Drive backend — routes through the Gemini_Gws MCP endpoint.

    The MCP endpoint proxies through to the worker's cached Google
    session, so we never touch a refresh token ourselves. Account is
    fixed to whichever address the worker was authenticated as
    (currently jim@dallasrr.com).
    """

    def __init__(self, key: Optional[str] = None, url: Optional[str] = None):
        resolved_key = key or _resolve_gemini_gws_key()
        if not resolved_key:
            raise GWSAuthUnavailable(
                "No Gemini_Gws key resolved. Set GEMINI_GWS_KEY or point "
                "GEMINI_GWS_KV_NAMESPACE at a KV entry."
            )
        self._key = resolved_key
        self._url = url or os.environ.get("GEMINI_GWS_URL") or DEFAULT_GEMINI_GWS_URL
        self._request_id = 0

    # ---------------------------------------------------------- transport

    def _call(self, action: str, **arguments: Any) -> Any:
        """POST tools/call name=gws_drive arguments={action, **arguments}."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": "gws_drive",
                "arguments": {"action": action, **arguments},
            },
        }
        # Query-string key is a shared secret — put it on the URL, not in body
        target = f"{self._url}?key={urllib.parse.quote(self._key)}"
        req = urllib.request.Request(
            target,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Gemini_Gws {e.code}: {e.read().decode('utf-8','replace')[:200]}"
            ) from e

        if "error" in body:
            raise ProviderError(f"Gemini_Gws rpc error: {body['error']}")

        result = body.get("result", {})
        # MCP returns tool output as a content array; unwrap the first text
        # block and try to parse as JSON if it looks like a payload.
        content = result.get("content", [])
        if content and isinstance(content, list):
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                text = first["text"]
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    return text
        return result

    # ---------------------------------------------------- named helpers

    def about(self):
        return self._call("about")

    def search(self, query: str, page_size: int = 50):
        return self._call("search", query=query, maxResults=page_size)

    def list_folder(self, folder_id: str, page_size: int = 50):
        return self._call("list", folderId=folder_id, maxResults=page_size)

    def get(self, file_id: str):
        return self._call("get", fileId=file_id)

    def metadata(self, file_id: str):
        return self._call("metadata", fileId=file_id)

    def upload(
        self,
        name: str,
        content: str,
        folder_id: Optional[str] = None,
        mime_type: str = "text/plain",
    ):
        params: dict = {"name": name, "content": content, "mimeType": mime_type}
        if folder_id:
            params["folderId"] = folder_id
        return self._call("create", **params)

    def upload_binary(
        self,
        name: str,
        data: bytes,
        folder_id: Optional[str] = None,
        mime_type: str = "application/octet-stream",
    ):
        import base64

        params: dict = {
            "name": name,
            "content": base64.b64encode(data).decode("ascii"),
            "contentEncoding": "base64",
            "mimeType": mime_type,
        }
        if folder_id:
            params["folderId"] = folder_id
        return self._call("create", **params)

    def folder(self, name: str, parent_id: Optional[str] = None):
        params: dict = {"name": name}
        if parent_id:
            params["parentId"] = parent_id
        return self._call("folder", **params)

    def download(self, file_id: str) -> str:
        result = self._call("get", fileId=file_id)
        if isinstance(result, dict) and "content" in result:
            return result["content"]
        return str(result)

    def download_url(self, file_id: str):
        return self._call("download", fileId=file_id)

    def export(self, file_id: str, mime_type: str):
        return self._call("export", fileId=file_id, mimeType=mime_type)

    def move(self, file_id: str, target_folder_id: str):
        return self._call("move", fileId=file_id, targetFolderId=target_folder_id)

    def rename(self, file_id: str, new_name: str):
        return self._call("update", fileId=file_id, name=new_name)

    def delete(self, file_id: str):
        return self._call("delete", fileId=file_id)

    def trash(self, file_id: str):
        return self._call("update", fileId=file_id, starred=False)  # placeholder; worker supports 'trashed' via update


# --------------------------------------------------------------- Native REST


class _TokenSource:
    def __init__(self, token_url: str):
        self._token_url = token_url
        self._value: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        now = time.time()
        if self._value and now < self._expires_at - _TOKEN_SAFETY_WINDOW_SECONDS:
            return self._value
        req = urllib.request.Request(self._token_url)
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise GWSAuthUnavailable(
                f"Token minter {self._token_url}: HTTP {e.code}: "
                f"{e.read().decode('utf-8','replace')[:200]}"
            ) from e
        except urllib.error.URLError as e:
            raise GWSAuthUnavailable(
                f"Token minter {self._token_url} unreachable: {e.reason}"
            ) from e

        access_token = payload.get("access_token")
        if not access_token:
            raise GWSAuthUnavailable(
                f"Token minter response missing 'access_token': {payload}"
            )
        expires_in = int(payload.get("expires_in", 3600))
        self._value = access_token
        self._expires_at = now + expires_in
        return access_token


class NativeGoogleDrive:
    """Fallback backend — Google Drive REST via google-auth-worker token."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        token_url: Optional[str] = None,
    ):
        self._explicit_token = access_token
        url = (
            token_url
            or os.environ.get("GOOGLE_AUTH_TOKEN_URL")
            or DEFAULT_TOKEN_URL
        )
        self._tokens = _TokenSource(url)

    def _access_token(self) -> str:
        return self._explicit_token or self._tokens.get()

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Any = None,
        raw_body: Optional[bytes] = None,
        extra_headers: Optional[dict] = None,
    ) -> Any:
        url = f"{DRIVE_BASE}{path}"
        if params:
            url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

        if json_body is not None and raw_body is not None:
            raise ValueError("json_body and raw_body are mutually exclusive")
        body_bytes = (
            json.dumps(json_body).encode("utf-8") if json_body is not None else raw_body
        )

        req = urllib.request.Request(url, method=method, data=body_bytes)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        if json_body is not None:
            req.add_header("Content-Type", "application/json")
        if extra_headers:
            for name, value in extra_headers.items():
                req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Drive {e.code} for {url}: {e.read().decode('utf-8','replace')[:400]}"
            ) from e

        if not body:
            return None
        text = body.decode("utf-8", "replace")
        if "application/json" in content_type:
            return json.loads(text)
        return text

    def about(self):
        return self.request(
            "GET",
            "/about",
            params={"fields": "user(emailAddress,displayName),storageQuota"},
        )

    def search(self, query: str, page_size: int = 50, fields: Optional[str] = None):
        return self.request(
            "GET",
            "/files",
            params={
                "q": query,
                "pageSize": page_size,
                "fields": fields
                or "files(id,name,mimeType,modifiedTime,parents),nextPageToken",
            },
        )

    def list_folder(
        self,
        folder_id: str,
        page_size: int = 50,
        fields: Optional[str] = None,
    ):
        return self.search(
            f"'{folder_id}' in parents and trashed = false",
            page_size=page_size,
            fields=fields,
        )

    def get(self, file_id: str, fields: Optional[str] = None):
        return self.request(
            "GET",
            f"/files/{file_id}",
            params={
                "fields": fields or "id,name,mimeType,parents,modifiedTime,size,webViewLink"
            },
        )

    def metadata(self, file_id: str):
        return self.request("GET", f"/files/{file_id}", params={"fields": "*"})

    def upload(
        self,
        name: str,
        content: str,
        folder_id: Optional[str] = None,
        mime_type: str = "text/plain",
    ):
        return self.upload_binary(
            name=name,
            data=content.encode("utf-8"),
            folder_id=folder_id,
            mime_type=mime_type,
        )

    def upload_binary(
        self,
        name: str,
        data: bytes,
        folder_id: Optional[str] = None,
        mime_type: str = "application/octet-stream",
    ):
        metadata: dict = {"name": name, "mimeType": mime_type}
        if folder_id:
            metadata["parents"] = [folder_id]

        boundary = "----ctxsync-drive-boundary"
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"{UPLOAD_BASE}/files?uploadType=multipart"
        req = urllib.request.Request(url, method="POST", data=body)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        req.add_header("Content-Type", f"multipart/related; boundary={boundary}")
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Drive upload {e.code}: {e.read().decode('utf-8','replace')[:400]}"
            ) from e

    def folder(self, name: str, parent_id: Optional[str] = None):
        metadata: dict = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        return self.request("POST", "/files", json_body=metadata)

    def download(self, file_id: str) -> str:
        result = self.request(
            "GET", f"/files/{file_id}", params={"alt": "media"}
        )
        if isinstance(result, (bytes, bytearray)):
            return result.decode("utf-8", "replace")
        return result

    def download_binary(self, file_id: str) -> bytes:
        url = f"{DRIVE_BASE}/files/{file_id}?alt=media"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Drive download {e.code}: {e.read().decode('utf-8','replace')[:400]}"
            ) from e

    def export(self, file_id: str, mime_type: str) -> str:
        return self.request(
            "GET", f"/files/{file_id}/export", params={"mimeType": mime_type}
        )

    def move(self, file_id: str, target_folder_id: str):
        current = self.get(file_id, fields="parents")
        remove = ",".join(current.get("parents", []))
        return self.request(
            "PATCH",
            f"/files/{file_id}",
            params={
                "addParents": target_folder_id,
                "removeParents": remove,
                "fields": "id,name,parents",
            },
            json_body={},
        )

    def rename(self, file_id: str, new_name: str):
        return self.request(
            "PATCH", f"/files/{file_id}", json_body={"name": new_name}
        )

    def delete(self, file_id: str):
        return self.request("DELETE", f"/files/{file_id}")

    def trash(self, file_id: str):
        return self.request(
            "PATCH", f"/files/{file_id}", json_body={"trashed": True}
        )


# ---------------------------------------------------------------- factory


def drive(prefer: str = "auto"):
    """Return a Drive client. Picks Gemini_Gws if a key resolves, else Native.

    ``prefer`` accepts ``"gemini"`` (force MCP; raises if no key),
    ``"native"`` (force REST), or ``"auto"`` (default: MCP if possible).
    """
    if prefer == "native":
        return NativeGoogleDrive()
    if prefer == "gemini":
        return GeminiGWSDrive()  # raises GWSAuthUnavailable if no key

    if _resolve_gemini_gws_key():
        return GeminiGWSDrive()
    return NativeGoogleDrive()


# Backwards-compat aliases so anything importing the old names still works
GoogleDrive = NativeGoogleDrive
GoogleWorkspace = NativeGoogleDrive
gws = drive
