"""Ergonomic wrapper around the claude.ai-kv provider.

The provider exposes every REST endpoint verbatim, which is more surface
than a normal caller needs. This module gives you the 10-line answer:

    >>> from ctxsync.easy import claude_projects
    >>> cp = claude_projects()
    >>> for p in cp.list_projects()[:5]:
    ...     print(p["name"])

    >>> cp.upload("Bookmarked", "notes.md", "# my notes")
    >>> cp.docs("Bookmarked")
    >>> cp.create_project("new project", description="scratch")

Projects can be addressed by uuid *or* by name — names are matched
case-insensitively against the active org. If a name is ambiguous the
call raises :class:`AmbiguousProjectError` with the candidates listed.

The active organization is auto-picked when there's only one usable org
on your account. If you have more than one, either pass ``org_id=`` to
:func:`claude_projects` or set the ``ORG_ID`` env var.

For chat driving and Claude Code Web sessions, use :meth:`ClaudeProjects.ask`
(new chat + prompt + assembled reply) or :meth:`ClaudeProjects.sessions`
(the ``Sessions`` facade for headless coding sessions).
"""

from __future__ import annotations

import os
from typing import Any, Iterator, Optional

from .configmanager import InMemoryConfigManager
from .exceptions import ProviderError
from .providers.claude_ai_kv import ClaudeAIKVProvider


# Text extraction from streamed events. Claude.ai's SSE emits a few
# shapes across chat vs session and old vs new API versions — rather than
# spread the field-name knowledge through every caller, one aggregator
# pulls whatever text is there.
def _event_text(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    # Chat "completion" events carry the delta as a plain "completion" str.
    completion = event.get("completion")
    if isinstance(completion, str):
        return completion
    # Newer content_block_delta / message_delta events tuck the text under
    # delta.text (chat) or delta.output_text_delta (some session variants).
    delta = event.get("delta")
    if isinstance(delta, dict):
        for key in ("text", "output_text_delta", "text_delta"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
    # Session assistant_message shape: {"type":"assistant_message","text":"..."}
    text = event.get("text")
    if isinstance(text, str):
        return text
    return ""


class AmbiguousProjectError(ProviderError):
    """Raised when a project name matches more than one project."""


class ProjectNotFoundError(ProviderError):
    """Raised when a project name doesn't match any project."""


class ClaudeProjects:
    """Thin, human-friendly facade over :class:`ClaudeAIKVProvider`.

    Constructed via :func:`claude_projects`; you rarely instantiate this
    directly. All methods return plain dicts/lists so results are easy to
    pretty-print, JSON-dump, or feed to another tool.
    """

    def __init__(self, provider: ClaudeAIKVProvider, org_id: str):
        self._provider = provider
        self._org_id = org_id
        # A small per-instance cache of (project_name_lower -> [uuid,...])
        # keeps repeated name lookups from paging the whole project list.
        self._project_index: Optional[dict[str, list[str]]] = None

    # ---------------------------------------------------------------- info

    @property
    def org_id(self) -> str:
        return self._org_id

    def organizations(self) -> list[dict[str, Any]]:
        return self._provider.get_organizations()

    # ------------------------------------------------------------ projects

    def list_projects(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return active projects in the current org, most-recently-updated first."""
        return self._provider.get_projects(self._org_id, include_archived=include_archived)

    def find_project(self, name_or_id: str) -> dict[str, Any]:
        """Resolve a project by uuid or (case-insensitive) name.

        Raises :class:`ProjectNotFoundError` if nothing matches, or
        :class:`AmbiguousProjectError` if the name matches more than one.
        """
        # UUID fast path — claude.ai project uuids include hyphens
        if "-" in name_or_id and len(name_or_id) >= 32:
            for project in self.list_projects(include_archived=True):
                if project["id"] == name_or_id:
                    return project

        # Name path — build a lower-cased index on first miss
        matches = [
            p
            for p in self.list_projects(include_archived=True)
            if p["name"].lower() == name_or_id.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ProjectNotFoundError(f"No project named {name_or_id!r}")
        raise AmbiguousProjectError(
            f"{len(matches)} projects match {name_or_id!r}: "
            + ", ".join(p["id"] for p in matches)
        )

    def create_project(self, name: str, description: str = "") -> dict[str, Any]:
        return self._provider.create_project(self._org_id, name, description)

    def archive_project(self, name_or_id: str) -> dict[str, Any]:
        project = self.find_project(name_or_id)
        return self._provider.archive_project(self._org_id, project["id"])

    # ------------------------------------------------------------ documents

    def docs(self, project: str) -> list[dict[str, Any]]:
        """List documents in a project. `project` accepts uuid or name."""
        project_id = self.find_project(project)["id"]
        return self._provider.list_files(self._org_id, project_id)

    def upload(self, project: str, file_name: str, content: str) -> dict[str, Any]:
        """Upload a file to a project. Overwrites nothing — creates a new doc.

        Delete the old one first with :meth:`delete_doc` if you need
        replace-semantics.
        """
        project_id = self.find_project(project)["id"]
        return self._provider.upload_file(self._org_id, project_id, file_name, content)

    def delete_doc(self, project: str, file_uuid: str) -> Any:
        project_id = self.find_project(project)["id"]
        return self._provider.delete_file(self._org_id, project_id, file_uuid)

    # ------------------------------------------------------------- chats

    def chats(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """List chat conversations in the current org.

        The claude.ai endpoint returns the FULL history in one shot with no
        pagination, so on heavy orgs this can take a while (60–90 s). Pass
        ``limit`` to slice the result client-side once it arrives.
        """
        result = self._provider.get_chat_conversations(self._org_id)
        if limit and isinstance(result, list):
            return result[:limit]
        return result

    def chat(self, conversation_id: str) -> dict[str, Any]:
        return self._provider.get_chat_conversation(self._org_id, conversation_id)

    # -------------------------------------------------- published artifacts

    def artifacts(self) -> list[dict[str, Any]]:
        """List all published artifacts across the current org's chats."""
        return self._provider.get_published_artifacts(self._org_id)

    def artifact(self, artifact_uuid: str) -> Any:
        """Return the body of a published artifact by its uuid."""
        return self._provider.get_artifact_content(self._org_id, artifact_uuid)

    # ---------------------------------------------------- chat driving

    def new_chat(
        self,
        project: str,
        name: str = "",
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new chat in ``project`` (uuid or name)."""
        project_id = self.find_project(project)["id"]
        return self._provider.create_chat(
            self._org_id,
            chat_name=name,
            project_uuid=project_id,
            model=model,
        )

    def send(
        self,
        chat_id: str,
        prompt: str,
        model: Optional[str] = None,
        timezone: str = "UTC",
    ) -> Iterator[dict[str, Any]]:
        """Stream events from ``send_message``. Yields raw parsed dicts.

        For a synchronous "just give me the reply" call, use :meth:`ask`
        instead — it consumes this iterator and returns assembled text.
        """
        yield from self._provider.send_message(
            self._org_id,
            chat_id,
            prompt,
            timezone=timezone,
            model=model,
        )

    def ask(
        self,
        project: str,
        prompt: str,
        model: Optional[str] = None,
    ) -> str:
        """One-shot: new chat + send + assemble → the assistant's reply."""
        chat = self.new_chat(project, model=model)
        chat_id = chat.get("uuid") or chat.get("id")
        if not chat_id:
            raise ProviderError(
                f"create_chat returned no id/uuid; payload was {chat!r}"
            )
        parts: list[str] = []
        for event in self.send(chat_id, prompt, model=model):
            parts.append(_event_text(event))
        return "".join(parts)

    def delete_chats(self, conversation_uuids: list[str]) -> Any:
        """Bulk-delete chat conversations."""
        return self._provider.delete_chat(self._org_id, conversation_uuids)

    # ------------------------------------------------ sessions facade

    def sessions(self) -> "Sessions":
        """Return the :class:`Sessions` helper bound to the current org."""
        return Sessions(self._provider, self._org_id)


class Sessions:
    """Helper for Claude Code Web sessions (the ``/v1/sessions`` endpoints).

    Distinct from chat conversations: sessions get a container + a git
    repo attached and can push branches back to GitHub. Use this from a
    driver script that spawns a headless coding session, waits for it to
    finish, and reads back the assembled output.
    """

    def __init__(self, provider: ClaudeAIKVProvider, org_id: str):
        self._provider = provider
        self._org_id = org_id

    def list_environments(self) -> list[dict[str, Any]]:
        return self._provider.get_environments(self._org_id)

    def list_sessions(self) -> Any:
        """List existing Claude Code Web sessions in the current org.

        Distinct from :meth:`create` — this returns sessions that already
        exist (yours + any shared), useful for driver scripts that need to
        pick up where a previous run left off.
        """
        return self._provider.get_sessions(self._org_id)

    def code_repos(self, skip_status: bool = True) -> Any:
        """List code repositories available for Claude Code Web sessions.

        ``skip_status=True`` (default) is much faster; drop it if you need
        each repo's build/PR state annotated in the response.
        """
        return self._provider.get_code_repos(
            self._org_id, skip_status=skip_status
        )

    def create(
        self,
        title: str,
        environment_id: str,
        git_repo_url: Optional[str] = None,
        git_repo_owner: Optional[str] = None,
        git_repo_name: Optional[str] = None,
        branch_name: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929",
    ) -> dict[str, Any]:
        return self._provider.create_session(
            self._org_id,
            title=title,
            environment_id=environment_id,
            git_repo_url=git_repo_url,
            git_repo_owner=git_repo_owner,
            git_repo_name=git_repo_name,
            branch_name=branch_name,
            model=model,
        )

    def send_input(self, session_id: str, prompt: str) -> Any:
        return self._provider.send_session_input(self._org_id, session_id, prompt)

    def stream_events(self, session_id: str) -> Iterator[dict[str, Any]]:
        yield from self._provider.stream_session_events(self._org_id, session_id)

    def archive(self, session_id: str) -> Any:
        return self._provider.archive_session(self._org_id, session_id)

    def run(
        self,
        title: str,
        environment_id: str,
        prompt: str,
        **repo_kwargs: Any,
    ) -> str:
        """create → send_input → stream_events → return assembled text."""
        session = self.create(title, environment_id, **repo_kwargs)
        session_id = session.get("id") or session.get("uuid")
        if not session_id:
            raise ProviderError(
                f"create_session returned no id/uuid; payload was {session!r}"
            )
        self.send_input(session_id, prompt)
        parts: list[str] = []
        for event in self.stream_events(session_id):
            parts.append(_event_text(event))
        return "".join(parts)


def _pick_org(provider: ClaudeAIKVProvider, org_hint: Optional[str]) -> str:
    orgs = provider.get_organizations()
    if not orgs:
        raise ProviderError(
            "No usable organizations for this session key. The account may "
            "not have the required capabilities (chat + claude_pro/raven/"
            "claude_max)."
        )
    if org_hint:
        for org in orgs:
            if org["id"] == org_hint:
                return org["id"]
        # Hint was set but doesn't match — fall through to auto-pick with
        # a warning-shaped ProviderError so the caller knows their hint was
        # ignored rather than silently.
        raise ProviderError(
            f"ORG_ID hint {org_hint!r} is not in the accessible org list: "
            + ", ".join(f"{o['id']} ({o['name']!r})" for o in orgs)
        )
    if len(orgs) == 1:
        return orgs[0]["id"]
    raise ProviderError(
        f"Multiple orgs accessible ({len(orgs)}); pass org_id= explicitly or "
        "set ORG_ID: "
        + ", ".join(f"{o['id']} ({o['name']!r})" for o in orgs)
    )


def claude_projects(
    session_key: Optional[str] = None,
    org_id: Optional[str] = None,
) -> ClaudeProjects:
    """One-line entry point.

    ``session_key`` and ``org_id`` are optional — omit them and the wrapper
    resolves ``session_key`` via
    :func:`ctxsync.auth.kv_session.resolve_session_key` and ``org_id`` via
    the ``ORG_ID`` / ``CLAUDE_CODE_ORGANIZATION_UUID`` env vars, falling
    back to auto-pick when there's only one usable org.
    """
    config = InMemoryConfigManager()
    org_hint = (
        org_id
        or os.environ.get("ORG_ID")
        or os.environ.get("CLAUDE_CODE_ORGANIZATION_UUID")
    )
    if org_hint:
        config.set("active_organization_id", org_hint)

    provider = ClaudeAIKVProvider(config=config, session_key=session_key)
    resolved_org = _pick_org(provider, org_hint)
    config.set("active_organization_id", resolved_org)
    return ClaudeProjects(provider=provider, org_id=resolved_org)
