"""Tests for OAuth 2.0 Device Authorization Flow (RFC 8628), token exchange,
auto-discovery, and token injection."""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from click.exceptions import Exit as ClickExit


# ── Device flow endpoint discovery ────────────────────────────────────────────


def test_device_discover_from_explicit_endpoints(cli_module):
    """Should use explicit endpoints when provided."""
    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "my-cli",
    }
    result = cli_module._device_discover_endpoints(auth_config)
    assert result["device_authorization_endpoint"] == "https://auth.example.com/device"
    assert result["token_endpoint"] == "https://auth.example.com/token"
    assert result["client_id"] == "my-cli"


def test_device_discover_missing_explicit_endpoints(cli_module):
    """Should exit when explicit endpoints are incomplete."""
    auth_config = {"device_authorization_endpoint": "https://auth.example.com/device"}
    with pytest.raises((SystemExit, ClickExit)):
        cli_module._device_discover_endpoints(auth_config)


def test_device_discover_from_issuer_url(cli_module):
    """Should discover endpoints from issuer_url well-known."""
    oidc_config = {
        "device_authorization_endpoint": "https://auth.example.com/device/authorize",
        "token_endpoint": "https://auth.example.com/oauth/token",
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = oidc_config

    auth_config = {"issuer_url": "https://auth.example.com", "client_id": "my-cli"}

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=mock_response)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        result = cli_module._device_discover_endpoints(auth_config)

    assert result["device_authorization_endpoint"] == "https://auth.example.com/device/authorize"
    assert result["token_endpoint"] == "https://auth.example.com/oauth/token"
    assert result["client_id"] == "my-cli"


def test_device_discover_from_issuer_url_no_device_ep(cli_module):
    """Should exit when issuer doesn't advertise device_authorization_endpoint."""
    oidc_config = {
        "authorization_endpoint": "https://auth.example.com/authorize",
        "token_endpoint": "https://auth.example.com/token",
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = oidc_config

    auth_config = {"issuer_url": "https://auth.example.com", "client_id": "my-cli"}

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=mock_response)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises((SystemExit, ClickExit)):
            cli_module._device_discover_endpoints(auth_config)


def test_device_discover_from_config_url(cli_module):
    """Should discover endpoints from device_config_url."""
    config_data = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "discovered-client",
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = config_data

    auth_config = {"device_config_url": "https://api.example.com/auth/device-config"}

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=mock_response)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        result = cli_module._device_discover_endpoints(auth_config)

    assert result["client_id"] == "discovered-client"
    assert result["device_authorization_endpoint"] == "https://auth.example.com/device"


# ── Device flow login ────────────────────────────────────────────────────────


def test_device_login_success(cli_module, tmp_config):
    """Should complete device flow: request code, poll, cache token."""
    mod, tmp_path, cache_dir = tmp_config

    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "my-cli",
        "scopes": "openid profile",
    }
    profile = {"_name": "testprofile", "auth": auth_config, "base_url": "https://api.example.com"}

    # Device code response
    device_resp = MagicMock()
    device_resp.status_code = 200
    device_resp.json.return_value = {
        "device_code": "DEVICE123",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://auth.example.com/device",
        "verification_uri_complete": "https://auth.example.com/device?user_code=ABCD-EFGH",
        "expires_in": 600,
        "interval": 0,  # speed up test
    }

    # Token poll responses: first pending, then success
    pending_resp = MagicMock()
    pending_resp.status_code = 400
    pending_resp.json.return_value = {"error": "authorization_pending"}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {
        "access_token": "device-access-token",
        "refresh_token": "device-refresh-token",
        "expires_in": 3600,
    }

    mock_client = MagicMock()
    mock_client.post = MagicMock(side_effect=[device_resp, pending_resp, success_resp])

    with patch("httpx.Client") as MockClient, patch("webbrowser.open"), patch("time.sleep"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        mod._device_login(auth_config, "testprofile", profile, no_browser=True)

    # Verify token was cached
    token_file = cache_dir / "testprofile_token.json"
    assert token_file.exists()
    cached = json.loads(token_file.read_text())
    assert cached["access_token"] == "device-access-token"
    assert cached["refresh_token"] == "device-refresh-token"
    assert "expires_at" in cached


def test_device_login_slow_down(cli_module, tmp_config):
    """Should increase interval on slow_down error."""
    mod, tmp_path, cache_dir = tmp_config

    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "my-cli",
    }
    profile = {"_name": "testprofile", "auth": auth_config, "base_url": "https://api.example.com"}

    device_resp = MagicMock()
    device_resp.status_code = 200
    device_resp.json.return_value = {
        "device_code": "DEV123",
        "user_code": "ABCD",
        "verification_uri": "https://auth.example.com/device",
        "expires_in": 600,
        "interval": 1,
    }

    slow_resp = MagicMock()
    slow_resp.status_code = 400
    slow_resp.json.return_value = {"error": "slow_down"}

    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = {"access_token": "tok", "expires_in": 3600}

    mock_client = MagicMock()
    mock_client.post = MagicMock(side_effect=[device_resp, slow_resp, success_resp])

    sleep_calls = []

    with patch("httpx.Client") as MockClient, patch("webbrowser.open"), \
         patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        mod._device_login(auth_config, "testprofile", profile, no_browser=True)

    # After slow_down, interval should increase by 5
    assert sleep_calls[0] == 1  # initial interval
    assert sleep_calls[1] == 6  # interval + 5


def test_device_login_access_denied(cli_module, tmp_config):
    """Should exit on access_denied."""
    mod, tmp_path, cache_dir = tmp_config

    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "my-cli",
    }
    profile = {"_name": "testprofile", "auth": auth_config, "base_url": "https://api.example.com"}

    device_resp = MagicMock()
    device_resp.status_code = 200
    device_resp.json.return_value = {
        "device_code": "DEV123",
        "user_code": "ABCD",
        "verification_uri": "https://auth.example.com/device",
        "expires_in": 600,
        "interval": 0,
    }

    denied_resp = MagicMock()
    denied_resp.status_code = 400
    denied_resp.json.return_value = {"error": "access_denied"}

    mock_client = MagicMock()
    mock_client.post = MagicMock(side_effect=[device_resp, denied_resp])

    with patch("httpx.Client") as MockClient, patch("webbrowser.open"), patch("time.sleep"):
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        with pytest.raises((SystemExit, ClickExit)):
            mod._device_login(auth_config, "testprofile", profile, no_browser=True)


def test_device_login_missing_client_id(cli_module, tmp_config):
    """Should exit when client_id is missing."""
    mod, tmp_path, cache_dir = tmp_config
    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
    }
    profile = {"_name": "testprofile", "auth": auth_config, "base_url": "https://api.example.com"}
    with pytest.raises((SystemExit, ClickExit)):
        mod._device_login(auth_config, "testprofile", profile, no_browser=True)


# ── Device auth via get_auth_headers ──────────────────────────────────────────


def test_get_auth_headers_dispatches_device(cli_module, tmp_config):
    """get_auth_headers should dispatch device type to _oidc_auth (shared cache)."""
    mod, tmp_path, cache_dir = tmp_config
    token_data = {
        "access_token": "device-cached-token",
        "expires_at": time.time() + 3600,
    }
    token_file = cache_dir / "testprofile_token.json"
    token_file.write_text(json.dumps(token_data))

    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "device",
            "device_authorization_endpoint": "https://auth.example.com/device",
            "token_endpoint": "https://auth.example.com/token",
            "client_id": "my-cli",
        },
    }
    headers = mod.get_auth_headers(profile)
    assert headers == {"Authorization": "Bearer device-cached-token"}


def test_get_auth_headers_dispatches_auto(cli_module, tmp_config):
    """get_auth_headers should dispatch auto type to _oidc_auth (shared cache)."""
    mod, tmp_path, cache_dir = tmp_config
    token_data = {
        "access_token": "auto-cached-token",
        "expires_at": time.time() + 3600,
    }
    token_file = cache_dir / "testprofile_token.json"
    token_file.write_text(json.dumps(token_data))

    profile = {
        "_name": "testprofile",
        "auth": {
            "type": "auto",
            "issuer_url": "https://auth.example.com",
            "client_id": "my-cli",
        },
    }
    headers = mod.get_auth_headers(profile)
    assert headers == {"Authorization": "Bearer auto-cached-token"}


# ── Token exchange ────────────────────────────────────────────────────────────


def test_token_exchange_skipped_when_not_configured(cli_module):
    """Should return original token_data when no exchange endpoint configured."""
    token_data = {"access_token": "original"}
    result = cli_module._token_exchange(token_data, {}, "https://api.example.com")
    assert result is token_data


def test_token_exchange_posts_to_endpoint(cli_module):
    """Should POST to exchange endpoint and return exchanged tokens."""
    token_data = {"access_token": "idp-token", "refresh_token": "idp-refresh"}
    auth_config = {
        "token_exchange_endpoint": "/auth/token-exchange",
        "token_exchange_body": '{"access_token": "{access_token}", "refresh_token": "{refresh_token}"}',
    }

    exchanged = {"access_token": "local-token", "expires_in": 1800}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = exchanged

    with patch("httpx.Client") as MockClient:
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        result = cli_module._token_exchange(token_data, auth_config, "https://api.example.com")

    assert result["access_token"] == "local-token"
    # Verify the POST was made with interpolated body
    call_kwargs = mock_client.post.call_args
    assert "/auth/token-exchange" in call_kwargs[0][0]
    body = call_kwargs[1]["content"]
    assert "idp-token" in body
    assert "idp-refresh" in body


def test_token_exchange_default_body(cli_module):
    """Should use default body template when token_exchange_body is not set."""
    token_data = {"access_token": "idp-token"}
    auth_config = {"token_exchange_endpoint": "/auth/exchange"}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "exchanged"}

    with patch("httpx.Client") as MockClient:
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        cli_module._token_exchange(token_data, auth_config, "https://api.example.com")

    body = mock_client.post.call_args[1]["content"]
    parsed = json.loads(body)
    assert parsed == {"access_token": "idp-token"}


def test_token_exchange_failure_exits(cli_module):
    """Should exit on non-200 exchange response."""
    token_data = {"access_token": "idp-token"}
    auth_config = {"token_exchange_endpoint": "/auth/exchange"}

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    with patch("httpx.Client") as MockClient:
        mock_client = MagicMock()
        mock_client.post.return_value = mock_response
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises((SystemExit, ClickExit)):
            cli_module._token_exchange(token_data, auth_config, "https://api.example.com")


# ── Auto-detect flow ─────────────────────────────────────────────────────────


def test_auto_detect_flow_prefers_device(cli_module):
    """Should detect device flow when device_authorization_endpoint is present."""
    oidc_config = {
        "grant_types_supported": [
            "authorization_code",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "authorization_endpoint": "https://auth.example.com/authorize",
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = oidc_config

    auth_config = {"issuer_url": "https://auth.example.com", "client_id": "my-cli"}

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=mock_response)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        result = cli_module._auto_detect_flow(auth_config)

    assert result == "device"
    assert auth_config["device_authorization_endpoint"] == "https://auth.example.com/device"
    assert auth_config["token_endpoint"] == "https://auth.example.com/token"


def test_auto_detect_flow_falls_back_to_oidc(cli_module):
    """Should fall back to OIDC PKCE when device flow is not supported."""
    oidc_config = {
        "grant_types_supported": ["authorization_code"],
        "token_endpoint": "https://auth.example.com/token",
        "authorization_endpoint": "https://auth.example.com/authorize",
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = oidc_config

    auth_config = {"issuer_url": "https://auth.example.com", "client_id": "my-cli"}

    with patch("httpx.Client") as MockClient:
        MockClient.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock(return_value=mock_response)))
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        result = cli_module._auto_detect_flow(auth_config)

    assert result == "oidc"
    assert auth_config["authorize_url"] == "https://auth.example.com/authorize"
    assert auth_config["token_url"] == "https://auth.example.com/token"


def test_auto_detect_flow_missing_issuer_url(cli_module):
    """Should exit when issuer_url is missing."""
    with pytest.raises((SystemExit, ClickExit)):
        cli_module._auto_detect_flow({})


# ── Token injection ──────────────────────────────────────────────────────────


def test_inject_token_plain(cli_module, tmp_config):
    """Should cache a plain access token with 1h default expiry."""
    mod, tmp_path, cache_dir = tmp_config
    mod._inject_token("testprofile", "plain-token-123", "", False)

    token_file = cache_dir / "testprofile_token.json"
    assert token_file.exists()
    cached = json.loads(token_file.read_text())
    assert cached["access_token"] == "plain-token-123"
    assert "refresh_token" not in cached
    assert cached["expires_at"] > time.time()
    assert cached["expires_at"] <= time.time() + 3601


def test_inject_token_with_refresh(cli_module, tmp_config):
    """Should cache both access and refresh tokens."""
    mod, tmp_path, cache_dir = tmp_config
    mod._inject_token("testprofile", "at-123", "rt-456", False)

    cached = json.loads((cache_dir / "testprofile_token.json").read_text())
    assert cached["access_token"] == "at-123"
    assert cached["refresh_token"] == "rt-456"


def test_inject_token_jwt_expiry(cli_module, tmp_config):
    """Should extract exp from JWT payload for expires_at."""
    mod, tmp_path, cache_dir = tmp_config

    # Build a fake JWT with exp claim
    exp_time = int(time.time()) + 7200
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user", "exp": exp_time}).encode()).rstrip(b"=").decode()
    fake_jwt = f"{header}.{payload}.fakesig"

    mod._inject_token("testprofile", fake_jwt, "", False)

    cached = json.loads((cache_dir / "testprofile_token.json").read_text())
    assert cached["expires_at"] == exp_time


def test_inject_token_empty_exits(cli_module, tmp_config):
    """Should exit when no access token provided."""
    mod, tmp_path, cache_dir = tmp_config
    with pytest.raises((SystemExit, ClickExit)):
        mod._inject_token("testprofile", "", "", False)


def test_inject_token_from_stdin(cli_module, tmp_config):
    """Should read token from stdin when from_stdin=True."""
    mod, tmp_path, cache_dir = tmp_config

    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        mock_stdin.read.return_value = "  stdin-token-789  \n"
        mod._inject_token("testprofile", "", "", True)

    cached = json.loads((cache_dir / "testprofile_token.json").read_text())
    assert cached["access_token"] == "stdin-token-789"


# ── Device flow with token exchange ──────────────────────────────────────────


def test_device_login_with_token_exchange(cli_module, tmp_config):
    """Device flow should perform token exchange when configured."""
    mod, tmp_path, cache_dir = tmp_config

    auth_config = {
        "device_authorization_endpoint": "https://auth.example.com/device",
        "token_endpoint": "https://auth.example.com/token",
        "client_id": "my-cli",
        "token_exchange_endpoint": "/auth/exchange",
    }
    profile = {"_name": "testprofile", "auth": auth_config, "base_url": "https://api.example.com"}

    device_resp = MagicMock()
    device_resp.status_code = 200
    device_resp.json.return_value = {
        "device_code": "DEV",
        "user_code": "CODE",
        "verification_uri": "https://auth.example.com/device",
        "expires_in": 600,
        "interval": 0,
    }

    token_resp = MagicMock()
    token_resp.status_code = 200
    token_resp.json.return_value = {"access_token": "idp-token", "expires_in": 3600}

    exchange_resp = MagicMock()
    exchange_resp.status_code = 200
    exchange_resp.json.return_value = {"access_token": "local-api-token", "expires_in": 1800}

    # 3 httpx.Client contexts: device code request, polling, token exchange
    device_code_client = MagicMock()
    device_code_client.post.return_value = device_resp

    poll_client = MagicMock()
    poll_client.post.return_value = token_resp

    exchange_client = MagicMock()
    exchange_client.post.return_value = exchange_resp

    clients = iter([device_code_client, poll_client, exchange_client])

    with patch("httpx.Client") as MockClient, patch("webbrowser.open"), patch("time.sleep"):
        def make_context(*args, **kwargs):
            ctx = MagicMock()
            c = next(clients)
            ctx.__enter__ = MagicMock(return_value=c)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        MockClient.side_effect = make_context
        mod._device_login(auth_config, "testprofile", profile, no_browser=True)

    cached = json.loads((cache_dir / "testprofile_token.json").read_text())
    assert cached["access_token"] == "local-api-token"


# ── Init helpers ─────────────────────────────────────────────────────────────


def test_init_device_auth_with_flags(cli_module):
    """_init_device_auth should populate profile from flags without prompting."""
    profile = {"auth": {"type": "device"}}
    cli_module._init_device_auth(
        profile,
        device_config_url=None,
        issuer_url="https://auth.example.com",
        client_id="my-cli",
        scopes="openid email",
        token_exchange_endpoint="/auth/exchange",
    )
    assert profile["auth"]["issuer_url"] == "https://auth.example.com"
    assert profile["auth"]["client_id"] == "my-cli"
    assert profile["auth"]["scopes"] == "openid email"
    assert profile["auth"]["token_exchange_endpoint"] == "/auth/exchange"


def test_init_device_auth_with_config_url(cli_module):
    """_init_device_auth should use device_config_url when provided."""
    profile = {"auth": {"type": "device"}}
    cli_module._init_device_auth(
        profile,
        device_config_url="https://api.example.com/auth/device-config",
        issuer_url=None,
        client_id="my-cli",
        scopes=None,
        token_exchange_endpoint=None,
    )
    assert profile["auth"]["device_config_url"] == "https://api.example.com/auth/device-config"
    assert profile["auth"]["client_id"] == "my-cli"


def test_init_auto_auth_with_flags(cli_module):
    """_init_auto_auth should populate profile from flags."""
    profile = {"auth": {"type": "auto"}}
    cli_module._init_auto_auth(
        profile,
        issuer_url="https://auth.example.com",
        client_id="my-cli",
        scopes="openid",
        token_exchange_endpoint=None,
    )
    assert profile["auth"]["issuer_url"] == "https://auth.example.com"
    assert profile["auth"]["client_id"] == "my-cli"
    assert profile["auth"]["scopes"] == "openid"
    assert "token_exchange_endpoint" not in profile["auth"]


def test_init_oidc_auth_with_all_flags(cli_module, monkeypatch):
    """_init_oidc_auth should populate profile fully from flags."""
    profile = {"auth": {"type": "oidc"}}
    # Mock the redirect_uri prompt (returns empty) and callback port prompt (returns "8484")
    prompt_responses = iter(["", "8484"])
    monkeypatch.setattr("typer.prompt", lambda *a, **kw: next(prompt_responses))
    cli_module._init_oidc_auth(
        profile,
        authorize_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/token",
        client_id="my-client",
        scopes="openid profile",
        issuer_url=None,
        token_exchange_endpoint="/auth/exchange",
    )
    assert profile["auth"]["authorize_url"] == "https://auth.example.com/authorize"
    assert profile["auth"]["token_url"] == "https://auth.example.com/token"
    assert profile["auth"]["client_id"] == "my-client"
    assert profile["auth"]["scopes"] == "openid profile"
    assert profile["auth"]["token_exchange_endpoint"] == "/auth/exchange"
