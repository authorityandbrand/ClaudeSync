# Cloud-resident auth

Two things this package assumes about a "stable" claude.ai connection from a
sandboxed cloud environment:

1. The session cookie shouldn't live in an `.env` file. Cookies expire, and
   the `.env` gets stale. Instead a companion Cloudflare Worker
   ("browser-auth-worker") is expected to log in with a real headed browser
   on a rotation schedule and post the current cookie to a KV namespace.
   This package reads that KV entry at request time.
2. Cloudflare's edge challenges requests that look like scraper cookie
   replay. The provider sends the full desktop-client envelope — real
   `Referer`, `Origin`, `lastActiveOrg` cookie, ClaudeNest Electron
   user-agent, dual `Bearer` + `Cookie` auth — which is enough for CF to
   accept the request even from a datacenter IP over the container's
   existing egress proxy. No TLS-fingerprint tricks required.

## Required credential

`CLOUDFLARE_API_TOKEN` — must have read on the KV namespace that holds
`current_session_key`.

## Optional overrides

| Var | Meaning | Default |
|---|---|---|
| `CLAUDE_AI_SESSION_KEY` | Skip KV and use this literal `sk-ant-sid…` | KV lookup |
| `CF_ACCOUNT_ID` | Cloudflare account | `e105d76aa6c851abdbd13d34d901cc7c` |
| `AUTH_STATE_KV_ID` | KV namespace id holding session state | `8e04a80e610c4ff0be68da39ec9c9cad` |
| `CLAUDE_AI_KV_KEY_NAME` | Key inside that namespace | `current_session_key` |
| `ANTHROPIC_API_KEY` | Skip KV for model calls | KV lookup |
| `CLAUDE_BRAIN_KV_ID` | KV namespace holding `anthropic:api_key` | `5411c260559e4b168d6154915fac670f` |

## Ordering

`resolve_session_key` returns the first non-empty value in:
explicit argument → env var → CF KV → raise `SessionKeyUnavailable`.

Nothing writes back to disk. Fetch is per-process and lives in memory —
the worker's next rotation will surface automatically on the next fetch.
