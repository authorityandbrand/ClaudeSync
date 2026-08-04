#!/usr/bin/env python3
"""Push a Gemini_Gws key to Cloudflare KV.

Reads the key from stdin (never argv — args leak into ``ps`` and shell
history). Writes to ``AUTH_STATE/gemini_gws_key`` by default; override
via env or flags. Reads it back after write to prove it landed.

Usage::

    # Prompted, echo suppressed:
    python scripts/push_gemini_gws_key.py

    # Piped in from a password manager / clipboard:
    pbpaste | python scripts/push_gemini_gws_key.py

Requires ``CLOUDFLARE_API_TOKEN`` in the env, plus optionally
``CF_ACCOUNT_ID`` (defaults to your account).
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ACCOUNT_ID = "e105d76aa6c851abdbd13d34d901cc7c"
DEFAULT_KV_ID = "8e04a80e610c4ff0be68da39ec9c9cad"  # AUTH_STATE — same one
                                                    # that holds current_session_key
DEFAULT_KEY_NAME = "gemini_gws_key"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", default=os.environ.get("CF_ACCOUNT_ID", DEFAULT_ACCOUNT_ID))
    parser.add_argument("--kv-id", default=os.environ.get("GEMINI_GWS_KV_NAMESPACE", DEFAULT_KV_ID))
    parser.add_argument("--key-name", default=os.environ.get("GEMINI_GWS_KV_KEY", DEFAULT_KEY_NAME))
    return parser.parse_args()


def read_key_stdin() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Paste Gemini_Gws key (echo suppressed): ").strip()
    return sys.stdin.read().strip()


def cf_request(method, url, cf_token, body=None):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", f"Bearer {cf_token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not cf_token:
        sys.stderr.write("Missing CLOUDFLARE_API_TOKEN env var\n")
        return 2

    args = parse_args()
    key = read_key_stdin()
    if not key:
        sys.stderr.write("Empty key value — nothing to push\n")
        return 2

    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:8]
    print(f"Pushing key (len={len(key)}, sha8={fingerprint}) to KV "
          f"{args.kv_id}/{args.key_name}", file=sys.stderr)

    base = (
        f"https://api.cloudflare.com/client/v4/accounts/{args.account_id}"
        f"/storage/kv/namespaces/{args.kv_id}/values/{args.key_name}"
    )

    # Write
    write_status, write_body = cf_request(
        "PUT", base, cf_token, body=key.encode("utf-8")
    )
    if write_status >= 400:
        sys.stderr.write(f"KV write failed [{write_status}]: {write_body[:400]}\n")
        return 1
    print(f"  ✓ PUT {write_status}", file=sys.stderr)

    # Verify
    read_status, read_body = cf_request("GET", base, cf_token)
    if read_status >= 400:
        sys.stderr.write(f"KV readback failed [{read_status}]: {read_body[:400]}\n")
        return 1
    read_fingerprint = hashlib.sha256(read_body.strip().encode()).hexdigest()[:8]
    if read_fingerprint == fingerprint:
        print(f"  ✓ GET {read_status}, sha8 matches", file=sys.stderr)
    else:
        sys.stderr.write(
            f"KV readback sha8 mismatch: wrote {fingerprint}, "
            f"read {read_fingerprint}\n"
        )
        return 1

    print(
        "\nSet these in every environment that needs Drive access:\n"
        f"  export GEMINI_GWS_KV_NAMESPACE={args.kv_id}\n"
        f"  export GEMINI_GWS_KV_KEY={args.key_name}\n"
        "  # (CLOUDFLARE_API_TOKEN + CF_ACCOUNT_ID already required for KV read)\n"
        "\nOr for a direct-set (no KV round-trip):\n"
        "  export GEMINI_GWS_KEY=<paste-the-key-here>\n",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
