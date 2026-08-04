# ctxsync

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![PyPI](https://badge.fury.io/py/ctxsync.svg)](https://pypi.org/project/ctxsync/)
[![Release](https://img.shields.io/github/release/jahwag/ctxsync.svg)](https://github.com/jahwag/ctxsync/releases)
[![Build Status](https://github.com/jahwag/ctxsync/actions/workflows/python-package.yml/badge.svg)](https://github.com/jahwag/ctxsync/actions/workflows/python-package.yml)
[![Issues](https://img.shields.io/github/issues/jahwag/ctxsync)](https://github.com/jahwag/ctxsync/issues)
[![Code Style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Dependencies](https://img.shields.io/librariesio/github/jahwag/ctxsync)](https://github.com/jahwag/ctxsync/network/dependencies)
[![Last Commit](https://img.shields.io/github/last-commit/jahwag/ctxsync.svg)](https://github.com/jahwag/ctxsync/commits/main)
[![Sponsor jahwag](https://img.shields.io/badge/Sponsor-♥-red)](https://github.com/sponsors/jahwag)


ctxsync (formerly known as ClaudeSync) bridges your local development environment with Claude.ai projects, enabling seamless synchronization to enhance your AI-powered workflow.

> **Renamed from ClaudeSync**: the `claudesync` PyPI package is deprecated — install `ctxsync` instead. Your existing configuration is picked up automatically: `~/.claudesync` is migrated on first run and project-local `.claudesync` directories keep working.

![ctxsync in Action](ctxsync.gif)

## ⚠️ Disclaimer

ctxsync is an independent, open-source project **not affiliated** with Anthropic or Claude.ai. By using ctxsync, you agree to:

1. Use it at your own risk.
2. Acknowledge potential violation of Anthropic's Terms of Service.
3. Assume responsibility for any consequences.
4. Understand that Anthropic does not support this tool.

Please review [Anthropic's Terms of Service](https://www.anthropic.com/legal/consumer-terms) before using ctxsync.

## 🌟 Features

- **File sync**: Synchronize local files with [Claude.ai projects](https://www.anthropic.com/news/projects).
- **Python API**: Drive Claude.ai projects and Google Drive from your own scripts (`ctxsync.easy`, `ctxsync.gws`).
- **Cloud / KV auth**: Run from any container or CI runner — session key resolves from a Cloudflare KV namespace, no interactive login required.
- **Companion CLIs**: `ctxsync-easy` (projects, chats, sessions) and `ctxsync-drive` (Google Drive ops) sit alongside the classic `ctxsync push/pull`.
- **Cross-Platform**: Compatible with [Windows, macOS, and Linux](https://github.com/jahwag/ctxsync/releases).
- **Configurable**: Plenty of [configuration options](https://github.com/jahwag/ctxsync/wiki/Quick-reference).

## ⚙️ Prerequisites

### 📄 Supported Claude.ai plans

| [Plan](https://www.anthropic.com/pricing)   | Supported |
|--------|-----------|
| Pro    | ✅        |
| Team   | ✅        |
| Max    | ✅        |
| Free   | ❌        |

### 💻 Software

- **Python**: ≥ [3.10](https://www.python.org/downloads/)
- **pip**: [Python package installer](https://pip.pypa.io/en/stable/installation/)

### 🔑 Credentials

Two ways to auth, pick whichever fits:

- **Local / interactive** — `ctxsync auth login` stores the session key via your SSH key (see [GitHub's guide](https://docs.github.com/en/authentication/connecting-to-github-with-ssh) to generate one). Used by the classic `ctxsync push/pull` flow.
- **Cloud / KV** — set `CLOUDFLARE_API_TOKEN` and the Python API + `ctxsync-easy` CLI resolve the current session key from a Cloudflare KV namespace at request time. No `.env` cookie, no SSH key needed. Details in [`src/ctxsync/auth/README.md`](src/ctxsync/auth/README.md).

## 🐍 Python API + cloud auth

The `ctxsync.easy` module is a one-line-per-task facade over the claude.ai
provider — designed for automation scripts and notebooks that just need to
drive a project without dealing with the raw REST surface.

```python
from ctxsync.easy import claude_projects

cp = claude_projects()                                 # auto-picks org
cp.list_projects()                                     # most-recent first
cp.find_project("Bookmarked")                          # uuid or case-insensitive name
cp.docs("Bookmarked")                                  # list docs in a project
cp.upload("Bookmarked", "notes.md", "# hello")         # add a doc
cp.delete_doc("Bookmarked", "<doc-uuid>")              # remove one
cp.create_project("scratch", description="notes")      # new project
cp.archive_project("scratch")                          # archive it

# Chat driving
cp.ask("Bookmarked", "Summarize this project's docs")  # → assembled reply text
for event in cp.send("<chat-uuid>", "hi"): ...         # → raw SSE dicts
cp.chats()                                             # list conversations

# Claude Code Web sessions
sessions = cp.sessions()
sessions.list_environments()
sessions.run(title="fix bug", environment_id="env_...", prompt="...")
```

Google Drive gets the same treatment — one module, two swappable backends,
same public API on both:

```python
from ctxsync.gws import drive

d = drive()                                            # auto-picks backend
d.about()                                              # {user, storageQuota}
d.search("name contains 'q3'")                         # Drive query string
d.list_folder("<folder-id>")
d.upload("notes.md", "# hi", folder_id="<folder-id>")  # text
d.upload_binary("logo.png", png_bytes, folder_id="<folder-id>", mime_type="image/png")
d.folder("New folder", parent_id="<folder-id>")        # mkdir
d.move("<file-id>", "<target-folder-id>")
d.rename("<file-id>", "renamed.md")
d.trash("<file-id>")                                   # reversible
d.delete("<file-id>")                                  # permanent
d.download("<file-id>")                                # text
```

Both backends are live-verified:

- **Gemini_Gws MCP** (preferred) — routes through a shared Cloudflare Worker that owns a persistent Google OAuth session. Zero token handling on your side; the worker rotates its own refresh token. Selected when `GEMINI_GWS_KEY` resolves (either from env or KV).
- **Native REST** (fallback) — mints an access token from `google-auth-worker` and calls `www.googleapis.com/drive/v3/*` directly. Selected when no Gemini_Gws key is available.

`drive()` picks the first backend whose auth resolves. Force one with
`drive(prefer="gemini")` or `drive(prefer="native")`.

### Companion CLIs

`ctxsync-easy` mirrors the Python API from the shell:

```shell
ctxsync-easy orgs                                      # list usable orgs
ctxsync-easy projects [--all] [--json]                 # list projects
ctxsync-easy docs <project>                            # list docs in a project
ctxsync-easy upload <project> <name> [<path>|-]        # upload (- reads stdin)
ctxsync-easy create <name> [--desc TEXT]
ctxsync-easy archive <project>
ctxsync-easy ask <project> "prompt" [--model MODEL]    # one-shot chat
ctxsync-easy chats [--limit N]
```

`ctxsync-drive` does the same for Google Drive:

```shell
ctxsync-drive about
ctxsync-drive ls <folder-id> [--max N]
ctxsync-drive search "<query>" [--max N]
ctxsync-drive upload <path>|- [--folder-id ID] [--name NAME] [--mime TYPE]
ctxsync-drive mkdir <name> [--parent-id ID]
ctxsync-drive mv <file-id> <target-folder-id>
ctxsync-drive rename <file-id> <new-name>
ctxsync-drive trash <file-id>                          # reversible
ctxsync-drive rm <file-id> --yes                       # permanent
ctxsync-drive download <file-id> <path>
ctxsync-drive sync <folder-id> <project> [--dry-run] [--include GLOB] [--exclude GLOB]
```

`--json` on either CLI emits JSON instead of pretty tables.
`--backend {auto,gemini,native}` on `ctxsync-drive` forces a specific Drive backend.

## 🚀 Classic Quick Start

The original file-sync flow is unchanged — this is still the primary path for
most users pushing a local repo up to a Claude.ai project.

1. **Install ctxsync**
    ```shell
    pip install ctxsync
    ```

2. **Authenticate**
    ```shell
    ctxsync auth login
    ```

3. **Create a Project**
    ```shell
    ctxsync project create
    ```

4. **Start Syncing***
    ```shell
    ctxsync push
    ```
    **This is a one-way sync. Files not present locally will be removed from the Claude.ai project unless pruning is [disabled](https://github.com/jahwag/ctxsync/wiki/Quick-reference#pruning-remote).*

📚 [Detailed Guides & FAQs](https://github.com/jahwag/ctxsync/wiki)

## 🤝 Support & Contribute

Enjoying ctxsync? Support us by:

- ⭐ [Starring the Repository](https://github.com/jahwag/ctxsync)
- 🐛 [Reporting Issues](https://github.com/jahwag/ctxsync/issues)
- 🌍 [Contributing](CONTRIBUTING.md)
- 💬 [Join Our Discord](https://discord.gg/pR4qeMH4u4)
- 💖 [Sponsor Us](https://github.com/sponsors/jahwag)

Your contributions help improve ctxsync!

---

[Contributors](https://github.com/jahwag/ctxsync/graphs/contributors) • [License](https://github.com/jahwag/ctxsync/blob/master/LICENSE) • [Report Bug](https://github.com/jahwag/ctxsync/issues) • [Request Feature](https://github.com/jahwag/ctxsync/issues/new?labels=enhancement&template=feature_request.md)• [Sponsor](https://github.com/sponsors/jahwag)
