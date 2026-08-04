"""Cloud-friendly claude.ai provider.

The stock :class:`~ctxsync.providers.claude_ai.ClaudeAIProvider` fails from
sandboxed cloud environments (Claude Code Web, CI, any datacenter IP) for
one root cause and one operational cause:

Root cause — the request envelope. Cloudflare's edge in front of claude.ai
challenges requests that look like cookie replay from a scraper: missing
``Referer``, missing ``Origin``, no ``lastActiveOrg`` cookie, generic
``python-urllib`` user-agent. When the envelope matches what a real installed
ClaudeNest desktop client sends, CF lets the request through even from a
datacenter IP via the container's egress proxy. (We spent a while chasing
TLS-fingerprint bypasses with ``curl_cffi``/``impersonate`` — turns out
those aren't needed if the envelope is right.)

Operational cause — stale ``.env`` cookies. A session key hardcoded into an
``.env`` file goes stale in ~30 days; every consumer that reads the same
``.env`` breaks together. The fix is to resolve the key at request time via
:func:`ctxsync.auth.kv_session.resolve_session_key`, which reads from a
companion ``browser-auth-worker``'s Cloudflare KV entry so its rotation
surfaces automatically without a human paste step.

This provider centralizes both fixes.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
import os
from typing import Optional

from ..auth.kv_session import resolve_session_key
from ..exceptions import ProviderError
from ..http import urlopen_with_retry
from .base_claude_ai import BaseClaudeAIProvider

# What a real installed ClaudeNest desktop client sends. The specific version
# doesn't matter much — what matters is that it looks like an installed
# Electron client rather than a headless script. If Anthropic starts blocking
# this exact UA, bump the version; the shape is what CF is filtering on.
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "ClaudeNest/0.12.112 Chrome/136.0.7103.177 "
    "Electron/36.8.1 Safari/537.36"
)


class ClaudeAIKVProvider(BaseClaudeAIProvider):
    """claude.ai provider using CF-KV auth resolution + DESKTOP header envelope.

    Uses stock :mod:`urllib` — the container's egress proxy handles TLS
    correctly, and CF only cares about the request envelope. No extra
    runtime dependencies.
    """

    def __init__(self, config=None, session_key: Optional[str] = None):
        super().__init__(config)
        self._explicit_session_key = session_key
        self._resolved_session_key: Optional[str] = None

    # ------------------------------------------------------------------ auth

    def _session_key(self) -> str:
        if self._resolved_session_key is None:
            self._resolved_session_key = resolve_session_key(
                self._explicit_session_key
            )
        return self._resolved_session_key

    def _organization_hint(self) -> Optional[str]:
        return (
            self.config.get("active_organization_id")
            or os.environ.get("ORG_ID")
            or os.environ.get("CLAUDE_CODE_ORGANIZATION_UUID")
        )

    def _cookie_header(self) -> str:
        session_key = self._session_key()
        parts = [f"sessionKey={session_key}"]
        org = self._organization_hint()
        if org:
            parts.append(f"lastActiveOrg={org}")
        return "; ".join(parts)

    def _headers(self, extra=None) -> dict:
        session_key = self._session_key()
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            # Referer + Origin are what CF filters on for cookie-authenticated
            # calls. Without them the same key + cookie combo gets challenged.
            "Referer": "https://claude.ai/",
            "Origin": "https://claude.ai",
            # Bearer is redundant with the sessionKey cookie on most endpoints
            # but a few internal ones consult it as a fallback.
            "Authorization": f"Bearer {session_key}",
            "Cookie": self._cookie_header(),
        }
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------ transport

    def _make_request_internal(
        self, method, endpoint, data, base_url, extra_headers=None, _retry_401=True
    ):
        url = f"{base_url}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data is not None else None

        req = urllib.request.Request(url, method=method, data=body)
        for name, value in self._headers(extra_headers).items():
            req.add_header(name, value)

        # 120s handles the very-large-list endpoints (chat_conversations on
        # heavy orgs returns thousands of rows in one shot with no pagination).
        try:
            with urlopen_with_retry(req, timeout=120) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if not raw:
                    return None
                text = raw.decode("utf-8", "replace")
                return json.loads(text)
        except urllib.error.HTTPError as e:
            # A stale-but-syntactically-valid KV key looks fresh to us but
            # gets rejected by claude.ai. Clear the cache, re-resolve, and
            # retry exactly once — the KV rotator may have already updated.
            if e.code == 401 and _retry_401:
                self._resolved_session_key = None
                return self._make_request_internal(
                    method, endpoint, data, base_url, extra_headers,
                    _retry_401=False,
                )
            self._raise_for_status(e, url)
        except urllib.error.URLError as e:
            raise ProviderError(f"HTTP transport failed for {url}: {e}") from e
        except json.JSONDecodeError as e:
            raise ProviderError(
                f"Invalid JSON from {url}: {e}; body[:200]={text[:200]!r}"
            ) from e

    def _make_request(self, method, endpoint, data=None):
        return self._make_request_internal(method, endpoint, data, self.base_url)

    def _make_request_v1(self, method, endpoint, data=None, organization_id=None):
        base_url = self.base_url.replace("/api", "")
        extra = {"anthropic-version": "2023-06-01"}
        if organization_id:
            extra["x-organization-uuid"] = organization_id
        return self._make_request_internal(method, endpoint, data, base_url, extra)

    def _make_request_stream(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, method=method, data=body)
        # Force identity on the SSE stream — a gzip'd stream can't be parsed
        # event-by-event by SSEClient, and the server honors Accept-Encoding.
        stream_headers = self._headers(
            {"Accept": "text/event-stream", "Accept-Encoding": "identity"}
        )
        for name, value in stream_headers.items():
            req.add_header(name, value)
        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self._raise_for_status(e, url)

    def _make_request_stream_v1(self, method, endpoint, organization_id=None):
        base_url = self.base_url.replace("/api", "")
        extra = {
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
            "anthropic-version": "2023-06-01",
        }
        if organization_id:
            extra["x-organization-uuid"] = organization_id
        url = f"{base_url}{endpoint}"
        req = urllib.request.Request(url, method=method)
        for name, value in self._headers(extra).items():
            req.add_header(name, value)
        try:
            return urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            self._raise_for_status(e, url)

    # ------------------------------------------------------------- errors

    def _raise_for_status(self, error, url):
        code = getattr(error, "code", 0)
        try:
            raw = error.read()
            if error.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = body[:400]
        except Exception:
            payload = "<no body>"

        if code in (401, 403):
            raise ProviderError(
                f"claude.ai {code} for {url}: {payload}\n"
                "Session key rejected. Refresh browser-auth-worker or set "
                "CLAUDE_AI_SESSION_KEY explicitly."
            )
        raise ProviderError(f"claude.ai {code} for {url}: {payload}")

    # --------------------------------------------------- ctxsync integration

    def login(self):
        """Force a session-key resolution and validate against the org list."""
        session_key = self._session_key()
        organizations = self.get_organizations()
        if not organizations:
            raise ProviderError(
                "No usable organizations returned; sessionKey may lack the "
                "required capabilities (chat + claude_pro/raven/claude_max)."
            )
        return session_key, None
