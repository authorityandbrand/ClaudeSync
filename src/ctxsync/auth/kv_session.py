"""Resolve a live claude.ai sessionKey from Cloudflare KV.

Resolution order:
  1. explicit ``session_key`` argument
  2. ``CLAUDE_AI_SESSION_KEY`` env var
  3. Cloudflare KV: ``AUTH_STATE/current_session_key`` fetched with
     ``CLOUDFLARE_API_TOKEN``

The CF KV path is what a companion ``browser-auth-worker`` rotates on a
schedule, which is why this stays valid across the cookie's natural expiry
without a human re-pasting anything.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_ACCOUNT_ID = "e105d76aa6c851abdbd13d34d901cc7c"
DEFAULT_AUTH_STATE_KV_ID = "8e04a80e610c4ff0be68da39ec9c9cad"
DEFAULT_KEY_NAME = "current_session_key"


class SessionKeyUnavailable(RuntimeError):
    """Raised when a live claude.ai sessionKey can't be resolved."""


def _kv_get(account_id: str, namespace_id: str, key: str, cf_token: str) -> str:
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/storage/kv/namespaces/{namespace_id}/values/{key}"
    )
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {cf_token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8").strip()


def resolve_session_key(session_key: str | None = None) -> str:
    if session_key:
        return session_key

    env_key = os.environ.get("CLAUDE_AI_SESSION_KEY")
    if env_key:
        return env_key

    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not cf_token:
        raise SessionKeyUnavailable(
            "no session_key argument, no CLAUDE_AI_SESSION_KEY env var, "
            "and no CLOUDFLARE_API_TOKEN to fetch one from KV"
        )

    account_id = os.environ.get("CF_ACCOUNT_ID", DEFAULT_ACCOUNT_ID)
    namespace_id = os.environ.get("AUTH_STATE_KV_ID", DEFAULT_AUTH_STATE_KV_ID)
    key_name = os.environ.get("CLAUDE_AI_KV_KEY_NAME", DEFAULT_KEY_NAME)

    try:
        value = _kv_get(account_id, namespace_id, key_name, cf_token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise SessionKeyUnavailable(
            f"CF KV {namespace_id}/{key_name}: HTTP {e.code} {body}"
        ) from e
    except urllib.error.URLError as e:
        raise SessionKeyUnavailable(f"CF KV fetch failed: {e.reason}") from e

    if not value.startswith("sk-ant-sid"):
        raise SessionKeyUnavailable(
            f"CF KV {namespace_id}/{key_name} did not return an sk-ant-sid* token"
        )
    return value


def resolve_anthropic_api_key(api_key: str | None = None) -> str:
    """Same resolution shape for the model API key.

    Order: explicit arg → ``ANTHROPIC_API_KEY`` env → CF KV
    ``claude-brain-kv/anthropic:api_key``.
    """
    if api_key:
        return api_key

    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not cf_token:
        raise SessionKeyUnavailable(
            "no api_key argument, no ANTHROPIC_API_KEY env var, "
            "and no CLOUDFLARE_API_TOKEN to fetch one from KV"
        )

    account_id = os.environ.get("CF_ACCOUNT_ID", DEFAULT_ACCOUNT_ID)
    namespace_id = os.environ.get(
        "CLAUDE_BRAIN_KV_ID", "5411c260559e4b168d6154915fac670f"
    )
    key_name = os.environ.get("ANTHROPIC_API_KEY_KV_NAME", "anthropic:api_key")
    try:
        return _kv_get(account_id, namespace_id, key_name, cf_token)
    except urllib.error.HTTPError as e:
        raise SessionKeyUnavailable(
            f"CF KV {namespace_id}/{key_name}: HTTP {e.code}"
        ) from e
