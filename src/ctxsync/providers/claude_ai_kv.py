"""Cloud-friendly claude.ai provider.

Two problems the stock :class:`~ctxsync.providers.claude_ai.ClaudeAIProvider`
runs into from datacenter IPs:

1. Cloudflare bot fingerprints the TLS handshake (JA3/JA4) plus datacenter IP
   reputation and returns the "Just a moment..." challenge before the request
   reaches the app. curl/urllib fingerprints don't pass.
2. Any well-behaved automation on ephemeral cloud infrastructure needs a
   session key it can re-fetch on its own — hardcoding the cookie into an
   ``.env`` file goes stale in ~30 days and is what causes the recurring
   ``account_session_invalid`` failures.

This provider handles both:

* TLS fingerprint via ``curl_cffi`` (``impersonate="chrome131"``) so CF's edge
  sees a real Chrome handshake.
* The container's egress HTTPS proxy re-terminates TLS with its own profile
  and rejects impersonated handshakes, so we punch through the proxy for
  claude.ai requests. Every other tool in the environment (git, pip, gcloud,
  etc.) keeps using the proxy normally.
* Session key resolved at request time via
  :func:`ctxsync.auth.kv_session.resolve_session_key` so a companion
  browser-auth worker's rotation keeps things fresh.
* The header envelope that a real ClaudeNest desktop client sends — DESKTOP
  user-agent, ``lastActiveOrg`` cookie hint, and both cookie + Bearer
  authorization at once (matches what the existing skill scripts already
  discovered by trial).

Import lazily so the base ctxsync install stays lightweight; install with
``pip install ctxsync[cloud]`` to pull ``curl_cffi``.
"""

from __future__ import annotations

import gzip
import json
import os
from typing import Optional

from ..auth.kv_session import resolve_session_key
from ..exceptions import ProviderError
from .base_claude_ai import BaseClaudeAIProvider

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "ClaudeNest/0.12.112 Chrome/136.0.7103.177 "
    "Electron/36.8.1 Safari/537.36"
)
IMPERSONATE_PROFILE = "chrome131"

# HTTPS_PROXY vars that we need to strip for direct requests — the container's
# egress proxy re-terminates TLS and blocks the impersonated handshake.
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _import_curl_cffi():
    try:
        from curl_cffi import requests  # type: ignore

        return requests
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ProviderError(
            "The claude.ai-kv provider requires curl_cffi. "
            "Install with `pip install curl_cffi` or `pip install ctxsync[cloud]`."
        ) from exc


class ClaudeAIKVProvider(BaseClaudeAIProvider):
    """claude.ai provider using CF-KV auth resolution + Chrome TLS impersonation."""

    def __init__(self, config=None, session_key: Optional[str] = None):
        super().__init__(config)
        self._explicit_session_key = session_key
        self._resolved_session_key: Optional[str] = None
        self._requests = None

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

    def _headers(self, extra=None) -> dict:
        session_key = self._session_key()
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Referer": "https://claude.ai/",
            "Origin": "https://claude.ai",
            "Authorization": f"Bearer {session_key}",
        }
        if extra:
            headers.update(extra)
        return headers

    def _cookies(self) -> dict:
        session_key = self._session_key()
        cookies = {"sessionKey": session_key}
        org = self._organization_hint()
        if org:
            cookies["lastActiveOrg"] = org
        return cookies

    # ------------------------------------------------------------ transport

    def _client(self):
        if self._requests is None:
            self._requests = _import_curl_cffi()
        return self._requests

    def _direct_call(self, requests, method, url, **kwargs):
        """Run a curl_cffi call with HTTPS_PROXY vars temporarily removed.

        curl_cffi honors ``$HTTPS_PROXY`` from the process env even when
        ``proxies={"https": None}`` is passed, and the container's egress
        proxy re-terminates TLS in a way that rejects Chrome-impersonated
        handshakes. Strip the vars for the duration of the call only.
        """
        saved = {}
        for var in _PROXY_VARS:
            if var in os.environ:
                saved[var] = os.environ.pop(var)
        try:
            return requests.request(method, url, **kwargs)
        finally:
            os.environ.update(saved)

    def _make_request_internal(
        self, method, endpoint, data, base_url, extra_headers=None
    ):
        url = f"{base_url}{endpoint}"
        requests = self._client()

        try:
            response = self._direct_call(
                requests,
                method,
                url,
                headers=self._headers(extra_headers),
                cookies=self._cookies(),
                json=data if data is not None else None,
                impersonate=IMPERSONATE_PROFILE,
                timeout=45,
            )
        except Exception as exc:
            raise ProviderError(f"HTTP transport failed for {url}: {exc}") from exc

        if response.status_code >= 400:
            self._raise_for_status(response, url)

        body = response.content
        if response.headers.get("Content-Encoding") == "gzip":
            try:
                body = gzip.decompress(body)
            except OSError:
                pass  # curl_cffi likely already decompressed

        if not body:
            return None

        text = body.decode("utf-8", "replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Invalid JSON from {url}: {exc}; body[:200]={text[:200]!r}"
            ) from exc

    def _make_request(self, method, endpoint, data=None):
        return self._make_request_internal(method, endpoint, data, self.base_url)

    def _make_request_v1(self, method, endpoint, data=None, organization_id=None):
        base_url = self.base_url.replace("/api", "")
        extra = {"anthropic-version": "2023-06-01"}
        if organization_id:
            extra["x-organization-uuid"] = organization_id
        return self._make_request_internal(method, endpoint, data, base_url, extra)

    def _make_request_stream(self, method, endpoint, data=None):
        requests = self._client()
        return self._direct_call(
            requests,
            method,
            f"{self.base_url}{endpoint}",
            headers=self._headers({"Accept": "text/event-stream"}),
            cookies=self._cookies(),
            json=data if data is not None else None,
            impersonate=IMPERSONATE_PROFILE,
            stream=True,
            timeout=None,
        )

    def _make_request_stream_v1(self, method, endpoint, organization_id=None):
        base_url = self.base_url.replace("/api", "")
        extra = {
            "Accept": "text/event-stream",
            "anthropic-version": "2023-06-01",
        }
        if organization_id:
            extra["x-organization-uuid"] = organization_id
        requests = self._client()
        return self._direct_call(
            requests,
            method,
            f"{base_url}{endpoint}",
            headers=self._headers(extra),
            cookies=self._cookies(),
            impersonate=IMPERSONATE_PROFILE,
            stream=True,
            timeout=None,
        )

    # ------------------------------------------------------------- errors

    def _raise_for_status(self, response, url):
        try:
            payload = response.json()
        except Exception:
            payload = response.text[:400]

        code = response.status_code
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
