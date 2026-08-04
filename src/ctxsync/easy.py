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
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .configmanager import InMemoryConfigManager
from .exceptions import ProviderError
from .providers.claude_ai_kv import ClaudeAIKVProvider


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

    def chats(self) -> list[dict[str, Any]]:
        """List chat conversations in the current org."""
        return self._provider.get_chat_conversations(self._org_id)

    def chat(self, conversation_id: str) -> dict[str, Any]:
        return self._provider.get_chat_conversation(self._org_id, conversation_id)


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
