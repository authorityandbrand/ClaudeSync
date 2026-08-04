"""Tests for the ergonomic wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ctxsync.easy import (
    AmbiguousProjectError,
    ClaudeProjects,
    ProjectNotFoundError,
    _pick_org,
    claude_projects,
)
from ctxsync.exceptions import ProviderError


_SENTINEL = object()


def _mock_provider(orgs=_SENTINEL, projects=_SENTINEL):
    provider = MagicMock()
    provider.get_organizations.return_value = (
        [{"id": "org-1", "name": "Only Org"}] if orgs is _SENTINEL else orgs
    )
    provider.get_projects.return_value = [] if projects is _SENTINEL else projects
    return provider


def test_pick_org_single_org_auto_selects():
    provider = _mock_provider()
    assert _pick_org(provider, None) == "org-1"


def test_pick_org_multiple_orgs_no_hint_raises():
    provider = _mock_provider(
        orgs=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    )
    with pytest.raises(ProviderError):
        _pick_org(provider, None)


def test_pick_org_honors_valid_hint():
    provider = _mock_provider(
        orgs=[{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]
    )
    assert _pick_org(provider, "b") == "b"


def test_pick_org_rejects_hint_not_in_list():
    provider = _mock_provider(orgs=[{"id": "a", "name": "A"}])
    with pytest.raises(ProviderError):
        _pick_org(provider, "wrong-hint")


def test_pick_org_no_orgs_raises():
    provider = _mock_provider(orgs=[])
    with pytest.raises(ProviderError):
        _pick_org(provider, None)


def test_find_project_by_uuid_hits_fast_path():
    provider = _mock_provider(
        projects=[
            {"id": "019f6b9b-8000-71d4-86d0-04f7599ca15b", "name": "one"},
            {"id": "019f6b4d-d7e6-7537-a504-685cb1b0b14a", "name": "two"},
        ]
    )
    cp = ClaudeProjects(provider, "org-1")
    result = cp.find_project("019f6b9b-8000-71d4-86d0-04f7599ca15b")
    assert result["name"] == "one"


def test_find_project_by_name_case_insensitive():
    provider = _mock_provider(
        projects=[{"id": "u-1", "name": "MyProject"}]
    )
    cp = ClaudeProjects(provider, "org-1")
    assert cp.find_project("myproject")["id"] == "u-1"


def test_find_project_ambiguous_raises():
    provider = _mock_provider(
        projects=[
            {"id": "u-1", "name": "Same"},
            {"id": "u-2", "name": "same"},
        ]
    )
    cp = ClaudeProjects(provider, "org-1")
    with pytest.raises(AmbiguousProjectError):
        cp.find_project("same")


def test_find_project_missing_raises():
    provider = _mock_provider(projects=[{"id": "u-1", "name": "one"}])
    cp = ClaudeProjects(provider, "org-1")
    with pytest.raises(ProjectNotFoundError):
        cp.find_project("nonexistent")


def test_docs_resolves_project_first(monkeypatch):
    provider = _mock_provider(projects=[{"id": "u-1", "name": "MyProject"}])
    provider.list_files.return_value = [{"uuid": "d-1", "file_name": "hi.md"}]
    cp = ClaudeProjects(provider, "org-1")

    result = cp.docs("MyProject")
    provider.list_files.assert_called_once_with("org-1", "u-1")
    assert result[0]["file_name"] == "hi.md"


def test_upload_resolves_project_first():
    provider = _mock_provider(projects=[{"id": "u-1", "name": "P"}])
    provider.upload_file.return_value = {"uuid": "d-99"}
    cp = ClaudeProjects(provider, "org-1")

    cp.upload("P", "file.md", "content")
    provider.upload_file.assert_called_once_with(
        "org-1", "u-1", "file.md", "content"
    )


def test_new_chat_resolves_project_and_delegates():
    provider = _mock_provider(projects=[{"id": "u-1", "name": "MyProject"}])
    provider.create_chat.return_value = {"uuid": "chat-1"}
    cp = ClaudeProjects(provider, "org-1")

    result = cp.new_chat("MyProject", name="hi", model="claude-sonnet-4-5")

    provider.create_chat.assert_called_once_with(
        "org-1", chat_name="hi", project_uuid="u-1", model="claude-sonnet-4-5"
    )
    assert result == {"uuid": "chat-1"}


def test_send_yields_provider_events():
    provider = _mock_provider()
    events = [
        {"type": "completion", "completion": "hello "},
        {"type": "completion", "completion": "world"},
    ]
    provider.send_message.return_value = iter(events)
    cp = ClaudeProjects(provider, "org-1")

    yielded = list(cp.send("chat-1", "hi", model="m", timezone="US/Central"))
    assert yielded == events
    provider.send_message.assert_called_once_with(
        "org-1", "chat-1", "hi", timezone="US/Central", model="m"
    )


def test_ask_assembles_completion_and_delta_shapes():
    provider = _mock_provider(projects=[{"id": "u-1", "name": "P"}])
    provider.create_chat.return_value = {"uuid": "chat-1"}
    # Mix the shapes ask() has to tolerate: legacy completion, new-style
    # content_block_delta, and a terminating dict with no text.
    provider.send_message.return_value = iter([
        {"type": "completion", "completion": "Hello, "},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
        {"type": "message_stop"},
    ])
    cp = ClaudeProjects(provider, "org-1")

    reply = cp.ask("P", "greet me")
    assert reply == "Hello, world"


def test_delete_chats_delegates_to_provider():
    provider = _mock_provider()
    provider.delete_chat.return_value = {"deleted": 2}
    cp = ClaudeProjects(provider, "org-1")

    result = cp.delete_chats(["c1", "c2"])
    provider.delete_chat.assert_called_once_with("org-1", ["c1", "c2"])
    assert result == {"deleted": 2}


def test_sessions_create_wires_git_kwargs():
    provider = _mock_provider()
    provider.create_session.return_value = {"id": "sess-1"}
    cp = ClaudeProjects(provider, "org-1")
    sessions = cp.sessions()

    sessions.create(
        title="job",
        environment_id="env-1",
        git_repo_url="https://github.com/o/r",
        git_repo_owner="o",
        git_repo_name="r",
        branch_name="claude/x",
    )
    provider.create_session.assert_called_once_with(
        "org-1",
        title="job",
        environment_id="env-1",
        git_repo_url="https://github.com/o/r",
        git_repo_owner="o",
        git_repo_name="r",
        branch_name="claude/x",
        model="claude-sonnet-4-5-20250929",
    )


def test_sessions_run_does_create_send_stream_and_assembles():
    provider = _mock_provider()
    provider.create_session.return_value = {"id": "sess-1"}
    provider.send_session_input.return_value = {"ok": True}
    provider.stream_session_events.return_value = iter([
        {"type": "assistant_message", "text": "part one "},
        {"type": "content_block_delta", "delta": {"text": "part two"}},
    ])
    cp = ClaudeProjects(provider, "org-1")

    text = cp.sessions().run(
        title="job",
        environment_id="env-1",
        prompt="do the thing",
        git_repo_url="https://github.com/o/r",
    )

    provider.create_session.assert_called_once()
    provider.send_session_input.assert_called_once_with(
        "org-1", "sess-1", "do the thing"
    )
    provider.stream_session_events.assert_called_once_with("org-1", "sess-1")
    assert text == "part one part two"


def test_claude_projects_entry_point_wires_org(monkeypatch):
    """Entry point should end up with an active_organization_id set."""
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-ant-sid02-mock")
    monkeypatch.setenv("ORG_ID", "org-hint")

    # Patch the provider class so we don't actually hit the network
    def _fake_provider(config, session_key):
        p = MagicMock()
        p.get_organizations.return_value = [
            {"id": "org-hint", "name": "Hinted"},
            {"id": "org-other", "name": "Other"},
        ]
        p.get_projects.return_value = []
        return p

    import ctxsync.easy as easy_module

    monkeypatch.setattr(easy_module, "ClaudeAIKVProvider", _fake_provider)
    cp = claude_projects()
    assert cp.org_id == "org-hint"
