#!/usr/bin/env python3
"""Sanity-check the claude.ai-kv provider end-to-end.

Run from a container/environment where ``CLOUDFLARE_API_TOKEN`` is set and
the AUTH_STATE KV namespace holds a fresh session key. Prints org + project
counts. Never prints the session key itself.

    python scripts/probe_claude_ai_kv.py
"""

from __future__ import annotations

import os
import sys

from ctxsync.configmanager import InMemoryConfigManager
from ctxsync.providers.claude_ai_kv import ClaudeAIKVProvider


def main() -> int:
    config = InMemoryConfigManager()
    org_hint = (
        os.environ.get("ORG_ID")
        or os.environ.get("CLAUDE_CODE_ORGANIZATION_UUID")
    )
    if org_hint:
        config.set("active_organization_id", org_hint)

    provider = ClaudeAIKVProvider(config=config)
    session_key, _ = provider.login()
    print(f"login OK  key_prefix={session_key[:22]}…  len={len(session_key)}")

    orgs = provider.get_organizations()
    print(f"orgs: {len(orgs)}")
    for org in orgs[:5]:
        print(f"  {org['id']}  {org['name']!r}")

    if not orgs:
        return 1

    target_org = org_hint or orgs[0]["id"]
    projects = provider.get_projects(target_org)
    print(f"\nprojects in {target_org}: {len(projects)} active")
    for project in projects[:10]:
        print(f"  {project['id']}  {project['name']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
