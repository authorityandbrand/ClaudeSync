"""Google Drive wrapper — native REST via a companion token minter.

The Google side stays authenticated because a Cloudflare Worker
(``google-auth-worker``) holds a valid refresh token and mints a fresh
access token on demand at::

    GET https://google-auth-worker.authorityandbrand.workers.dev/token
    -> {"access_token": "ya29.a0A…"}

That access token is what Drive expects as ``Authorization: Bearer``. So
this wrapper skips every intermediate MCP/worker layer and calls Drive's
public REST endpoints directly. If the token minter's refresh_token dies
you replace it in the *worker's* secrets — one place, not four.

Nothing here requires ``GWS_WORKER_SESSION_KEY``, ``X-Session-Key``, or
any Google credential in the caller's env. Only the token endpoint URL
is configurable, so a different account can point at its own minter.

Usage::

    from ctxsync.gws import drive
    d = drive()
    d.search("name contains 'brief' and trashed = false")
    d.upload("notes.md", "# hi", folder_id="…")
    d.upload_binary("logo.png", b"…", mime_type="image/png")
    d.download("<file-id>")
    d.folder("New folder", parent_id="…")
    d.metadata("<file-id>")
    d.move / d.rename / d.delete
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

DEFAULT_TOKEN_URL = (
    "https://google-auth-worker.authorityandbrand.workers.dev/token"
)
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

# Refresh a bit before Google's stated expiry (they claim ~1h, we treat as
# 55 min) so a token that's about to expire mid-request gets rotated first.
_TOKEN_SAFETY_WINDOW_SECONDS = 300


class GWSAuthUnavailable(ProviderError):
    """Raised when the Google access token can't be minted."""


class _TokenSource:
    """Small in-memory cache around the token minter.

    Cache lives for the process; every call within the safety window
    returns the same access token. Once we cross the window we refetch.
    """

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
                f"Token minter {self._token_url} returned HTTP {e.code}: "
                f"{e.read().decode('utf-8', 'replace')[:200]}"
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
        # Google access tokens are ~1h; the minter may or may not tell us
        # the exact expiry, so treat 3600s as the upper bound.
        expires_in = int(payload.get("expires_in", 3600))
        self._value = access_token
        self._expires_at = now + expires_in
        return access_token


class GoogleDrive:
    """Native Drive REST client backed by an auto-minted access token.

    Methods return the same dict shape Drive's REST API returns. See
    ``request`` for the full-control escape hatch when a helper doesn't
    cover the endpoint you need.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        token_url: Optional[str] = None,
    ):
        # If you pass an explicit token we honor it (useful for tests) and
        # skip the minter entirely; token expiry then becomes your problem.
        self._explicit_token = access_token
        url = (
            token_url
            or os.environ.get("GOOGLE_AUTH_TOKEN_URL")
            or DEFAULT_TOKEN_URL
        )
        self._tokens = _TokenSource(url)

    # ----------------------------------------------------- transport

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
        """Call any Drive endpoint. ``path`` is joined to the v3 base URL.

        Escape hatch for endpoints not covered by the named helpers.
        Handles token injection, gzip'd responses, and JSON decoding.
        """
        url = f"{DRIVE_BASE}{path}"
        if params:
            url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)

        if json_body is not None and raw_body is not None:
            raise ValueError("json_body and raw_body are mutually exclusive")
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
        else:
            body_bytes = raw_body

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
            self._raise_for_status(e, url)

        if not body:
            return None
        text = body.decode("utf-8", "replace")
        if "application/json" in content_type:
            return json.loads(text)
        return text

    def _raise_for_status(self, error, url):
        code = getattr(error, "code", 0)
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:
            body = "<no body>"
        raise ProviderError(f"Drive {code} for {url}: {body[:400]}")

    # -------------------------------------------------- browsing / search

    def search(self, query: str, page_size: int = 50, fields: Optional[str] = None):
        """Drive search. ``query`` is a Drive query string.

        Examples:
            ``d.search("name contains 'brief' and trashed = false")``
            ``d.search("mimeType = 'application/pdf'")``
        """
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
        """Direct children of a folder (files + subfolders)."""
        return self.search(
            f"'{folder_id}' in parents and trashed = false",
            page_size=page_size,
            fields=fields,
        )

    def get(self, file_id: str, fields: Optional[str] = None):
        """Metadata for a file."""
        return self.request(
            "GET",
            f"/files/{file_id}",
            params={
                "fields": fields or "id,name,mimeType,parents,modifiedTime,size,webViewLink"
            },
        )

    def metadata(self, file_id: str):
        """Full metadata: permissions, owners, checksums, view links."""
        return self.request(
            "GET",
            f"/files/{file_id}",
            params={"fields": "*"},
        )

    def about(self):
        """Whoami + storage quota."""
        return self.request(
            "GET",
            "/about",
            params={"fields": "user(emailAddress,displayName),storageQuota"},
        )

    # ----------------------------------------------------- upload / write

    def upload(
        self,
        name: str,
        content: str,
        folder_id: Optional[str] = None,
        mime_type: str = "text/plain",
    ):
        """Create a new text file with the given content."""
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
        """Upload bytes via Drive's multipart upload endpoint.

        Small files only — the multipart endpoint is capped at 5 MB. For
        larger objects use :meth:`resumable_upload` (not implemented; call
        :meth:`request` with the resumable protocol).
        """
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
            self._raise_for_status(e, url)

    def folder(self, name: str, parent_id: Optional[str] = None):
        """Create a folder. Returns the new folder's metadata."""
        metadata: dict = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        return self.request("POST", "/files", json_body=metadata)

    # ----------------------------------------------------------- download

    def download(self, file_id: str) -> str:
        """Return a file's text content (or Google-native export as text)."""
        result = self.request(
            "GET",
            f"/files/{file_id}",
            params={"alt": "media"},
        )
        if isinstance(result, (bytes, bytearray)):
            return result.decode("utf-8", "replace")
        return result

    def download_binary(self, file_id: str) -> bytes:
        """Return raw bytes for a file — for PDFs, images, archives, etc."""
        url = f"{DRIVE_BASE}/files/{file_id}?alt=media"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        req.add_header("User-Agent", "ctxsync-drive/0.1")
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            self._raise_for_status(e, url)

    def export(self, file_id: str, mime_type: str) -> str:
        """Export a Google-native file (Doc/Sheet/Slide) as another format."""
        return self.request(
            "GET",
            f"/files/{file_id}/export",
            params={"mimeType": mime_type},
        )

    # ----------------------------------------------------- move / rename

    def move(self, file_id: str, target_folder_id: str):
        """Move a file to a different parent folder."""
        # Discover the current parents so we can remove them cleanly
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
            "PATCH",
            f"/files/{file_id}",
            json_body={"name": new_name},
        )

    def delete(self, file_id: str):
        """PERMANENT delete. Trash it with :meth:`trash` for reversible."""
        return self.request("DELETE", f"/files/{file_id}")

    def trash(self, file_id: str):
        """Move to trash (reversible for 30 days)."""
        return self.request(
            "PATCH",
            f"/files/{file_id}",
            json_body={"trashed": True},
        )


def drive() -> GoogleDrive:
    """One-line entry point.

    >>> from ctxsync.gws import drive
    >>> d = drive()
    >>> d.about()
    {'user': {'emailAddress': '…', 'displayName': '…'}, 'storageQuota': {...}}
    """
    return GoogleDrive()


# Backwards-compat aliases
GoogleWorkspace = GoogleDrive
gws = drive
