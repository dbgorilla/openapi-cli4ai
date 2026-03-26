"""Tests for auth header generation."""

from __future__ import annotations

import base64

import pytest
from click.exceptions import Exit as ClickExit


def test_auth_none(cli_module):
    """Should return empty headers for auth type none."""
    profile = {"auth": {"type": "none"}}
    assert cli_module.get_auth_headers(profile) == {}


def test_bearer_from_env_var(cli_module, monkeypatch):
    """Should build Bearer header from env var."""
    monkeypatch.setenv("MY_TOKEN", "test-token-123")
    profile = {
        "auth": {
            "type": "bearer",
            "token_env_var": "MY_TOKEN",
        },
    }
    headers = cli_module.get_auth_headers(profile)
    assert headers == {"Authorization": "Bearer test-token-123"}


def test_bearer_custom_prefix(cli_module, monkeypatch):
    """Should use custom prefix for bearer token."""
    monkeypatch.setenv("MY_TOKEN", "test-token-123")
    profile = {
        "auth": {
            "type": "bearer",
            "token_env_var": "MY_TOKEN",
            "prefix": "Token ",
            "header": "X-Auth",
        },
    }
    headers = cli_module.get_auth_headers(profile)
    assert headers == {"X-Auth": "Token test-token-123"}


def test_bearer_missing_env_var(cli_module, monkeypatch):
    """Should exit with error when bearer env var is not set."""
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    profile = {
        "auth": {
            "type": "bearer",
            "token_env_var": "MISSING_TOKEN",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        cli_module.get_auth_headers(profile)


def test_api_key_auth(cli_module, monkeypatch):
    """Should build API key header."""
    monkeypatch.setenv("MY_API_KEY", "sk-test-key")
    profile = {
        "auth": {
            "type": "api-key",
            "env_var": "MY_API_KEY",
            "header": "Authorization",
            "prefix": "Bearer ",
        },
    }
    headers = cli_module.get_auth_headers(profile)
    assert headers == {"Authorization": "Bearer sk-test-key"}


def test_api_key_custom_header(cli_module, monkeypatch):
    """Should support custom header names for API keys."""
    monkeypatch.setenv("MY_KEY", "key-123")
    profile = {
        "auth": {
            "type": "api-key",
            "env_var": "MY_KEY",
            "header": "X-API-Key",
            "prefix": "",
        },
    }
    headers = cli_module.get_auth_headers(profile)
    assert headers == {"X-API-Key": "key-123"}


def test_api_key_missing_env_var(cli_module, monkeypatch):
    """Should exit with error when API key env var is not set."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    profile = {
        "auth": {
            "type": "api-key",
            "env_var": "MISSING_KEY",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        cli_module.get_auth_headers(profile)


def test_basic_auth(cli_module, monkeypatch):
    """Should build Basic auth header."""
    monkeypatch.setenv("MY_USER", "admin")
    monkeypatch.setenv("MY_PASS", "secret")
    profile = {
        "auth": {
            "type": "basic",
            "username_env_var": "MY_USER",
            "password_env_var": "MY_PASS",
        },
    }
    headers = cli_module.get_auth_headers(profile)
    expected = base64.b64encode(b"admin:secret").decode()
    assert headers == {"Authorization": f"Basic {expected}"}


def test_basic_auth_missing_vars(cli_module, monkeypatch):
    """Should exit when basic auth env vars are missing."""
    monkeypatch.delenv("MISSING_USER", raising=False)
    monkeypatch.delenv("MISSING_PASS", raising=False)
    profile = {
        "auth": {
            "type": "basic",
            "username_env_var": "MISSING_USER",
            "password_env_var": "MISSING_PASS",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        cli_module.get_auth_headers(profile)


def test_unknown_auth_type(cli_module):
    """Should exit for unknown auth types."""
    profile = {"auth": {"type": "oauth2-flow"}}
    with pytest.raises((SystemExit, ClickExit)):
        cli_module.get_auth_headers(profile)


def test_quiet_mode_no_output(cli_module, monkeypatch, capsys):
    """Should not print errors in quiet mode."""
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    profile = {
        "auth": {
            "type": "bearer",
            "token_env_var": "MISSING_TOKEN",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        cli_module.get_auth_headers(profile, quiet=True)
    # In quiet mode, the captured output should be empty
    captured = capsys.readouterr()
    assert "MISSING_TOKEN" not in captured.out
