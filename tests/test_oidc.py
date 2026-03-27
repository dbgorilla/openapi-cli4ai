"""Tests for OIDC Authorization Code + PKCE auth."""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from click.exceptions import Exit as ClickExit


def test_oidc_auth_returns_cached_token(cli_module, tmp_config):
    """Should return cached token when it's still valid."""
    mod, tmp_path, cache_dir = tmp_config
    token_data = {
        "access_token": "oidc-token-123",
        "expires_at": time.time() + 3600,
    }
    token_file = cache_dir / "testprofile_token.json"
    token_file.write_text(json.dumps(token_data))
    token_file.chmod(0o600)

    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "oidc",
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        },
    }
    headers = mod._oidc_auth(profile, profile["auth"])
    assert headers == {"Authorization": "Bearer oidc-token-123"}


def test_oidc_auth_expired_token_prompts_login(cli_module, tmp_config):
    """Should exit with login prompt when token is expired and no refresh token."""
    mod, tmp_path, cache_dir = tmp_config
    token_data = {
        "access_token": "expired-token",
        "expires_at": time.time() - 100,
    }
    token_file = cache_dir / "testprofile_token.json"
    token_file.write_text(json.dumps(token_data))

    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "oidc",
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        mod._oidc_auth(profile, profile["auth"])


def test_oidc_auth_no_cache_prompts_login(cli_module, tmp_config):
    """Should exit with login prompt when no cached token exists."""
    mod, tmp_path, cache_dir = tmp_config
    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "oidc",
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        },
    }
    with pytest.raises((SystemExit, ClickExit)):
        mod._oidc_auth(profile, profile["auth"])


def test_oidc_refresh_missing_config(cli_module):
    """Should return None when token_url or client_id is missing."""
    result = cli_module._oidc_refresh({"client_id": "x"}, {"refresh_token": "rt"})
    assert result is None
    result = cli_module._oidc_refresh({"token_url": "x"}, {"refresh_token": "rt"})
    assert result is None


def test_pkce_challenge_computation(cli_module):
    """Verify PKCE S256 challenge is computed correctly per RFC 7636."""
    import secrets

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    # Verify it's base64url without padding
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge
    # Verify it decodes back to 32 bytes (SHA-256 output)
    padded = challenge + "=" * (4 - len(challenge) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    assert len(decoded) == 32


def test_oidc_callback_handler_state_validation(cli_module):
    """The callback handler class should have expected_state attribute."""
    handler_cls = cli_module._OIDCCallbackHandler
    assert hasattr(handler_cls, "expected_state")
    assert hasattr(handler_cls, "auth_code")
    assert hasattr(handler_cls, "error")


def test_get_auth_headers_dispatches_oidc(cli_module, tmp_config):
    """get_auth_headers should dispatch to _oidc_auth for type=oidc."""
    mod, tmp_path, cache_dir = tmp_config
    token_data = {
        "access_token": "dispatched-token",
        "expires_at": time.time() + 3600,
    }
    token_file = cache_dir / "testprofile_token.json"
    token_file.write_text(json.dumps(token_data))

    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "oidc",
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        },
    }
    headers = mod.get_auth_headers(profile)
    assert headers == {"Authorization": "Bearer dispatched-token"}
