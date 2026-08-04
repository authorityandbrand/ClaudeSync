"""Unit tests for the KV session resolver."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from ctxsync.auth.kv_session import (
    SessionKeyUnavailable,
    resolve_session_key,
    resolve_anthropic_api_key,
)


def _clear_env(monkeypatch):
    for var in (
        "CLAUDE_AI_SESSION_KEY",
        "CLOUDFLARE_API_TOKEN",
        "ANTHROPIC_API_KEY",
        "CF_ACCOUNT_ID",
        "AUTH_STATE_KV_ID",
        "CLAUDE_AI_KV_KEY_NAME",
        "CLAUDE_BRAIN_KV_ID",
        "ANTHROPIC_API_KEY_KV_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_explicit_argument_wins(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-ant-sid02-from-env")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "would-fail-if-called")
    with mock.patch("ctxsync.auth.kv_session._kv_get") as kv:
        result = resolve_session_key("sk-ant-sid02-explicit")
    assert result == "sk-ant-sid02-explicit"
    kv.assert_not_called()


def test_env_var_used_when_no_argument(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-ant-sid02-from-env")
    with mock.patch("ctxsync.auth.kv_session._kv_get") as kv:
        result = resolve_session_key()
    assert result == "sk-ant-sid02-from-env"
    kv.assert_not_called()


def test_kv_fallback_when_env_missing(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    with mock.patch(
        "ctxsync.auth.kv_session._kv_get",
        return_value="sk-ant-sid02-from-kv",
    ) as kv:
        result = resolve_session_key()
    assert result == "sk-ant-sid02-from-kv"
    kv.assert_called_once()


def test_kv_rejects_non_sk_ant_sid_value(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    with mock.patch(
        "ctxsync.auth.kv_session._kv_get",
        return_value="not-a-session-key",
    ):
        with pytest.raises(SessionKeyUnavailable):
            resolve_session_key()


def test_no_credentials_raises(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(SessionKeyUnavailable):
        resolve_session_key()


def test_api_key_resolution(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    with mock.patch(
        "ctxsync.auth.kv_session._kv_get",
        return_value="sk-ant-api03-from-kv",
    ) as kv:
        result = resolve_anthropic_api_key()
    assert result == "sk-ant-api03-from-kv"
    kv.assert_called_once()
