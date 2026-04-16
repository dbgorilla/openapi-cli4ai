"""Comprehensive tests for auth flows and utility functions in openapi-cli4ai."""

from __future__ import annotations

import json
import sys
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.exceptions import Exit as ClickExit

from openapi_cli4ai import cli as cli_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_token_cache(cache_dir, profile_name, token_data):
    """Write a token cache file and return its path."""
    token_file = cache_dir / f"{profile_name}_token.json"
    token_file.write_text(json.dumps(token_data))
    token_file.chmod(0o600)
    return token_file


def _mock_httpx_client(mock_response):
    """Return (MockClient, mock_client_instance) wired up as a context manager."""
    mock_client = MagicMock()
    MockClient = MagicMock()
    MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
    MockClient.return_value.__exit__ = MagicMock(return_value=False)
    return MockClient, mock_client


# ===========================================================================
# 1. _oauth_bearer
# ===========================================================================


class TestOAuthBearer:
    """Tests for _oauth_bearer (lines 420-446)."""

    def test_cached_token_valid(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "myapi",
            {
                "access_token": "good-token",
                "expires_at": time.time() + 3600,
            },
        )
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"token_endpoint": "/auth/token"}
        result = mod._oauth_bearer(profile, auth_config)
        assert result == {"Authorization": "Bearer good-token"}

    def test_cached_token_expired_refresh_succeeds(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "myapi",
            {
                "access_token": "old-token",
                "refresh_token": "rt-123",
                "expires_at": time.time() - 100,
            },
        )
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {
            "token_endpoint": "/auth/token",
            "refresh_endpoint": "/auth/refresh",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._oauth_bearer(profile, auth_config)
        assert result == {"Authorization": "Bearer new-token"}

    def test_cached_token_expired_no_refresh_endpoint(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "myapi",
            {
                "access_token": "old-token",
                "expires_at": time.time() - 100,
            },
        )
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"token_endpoint": "/auth/token"}
        with pytest.raises((SystemExit, ClickExit)):
            mod._oauth_bearer(profile, auth_config)

    def test_no_cached_token(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"token_endpoint": "/auth/token"}
        with pytest.raises((SystemExit, ClickExit)):
            mod._oauth_bearer(profile, auth_config)

    def test_corrupted_cache(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        token_file = cache_dir / "myapi_token.json"
        token_file.write_text("NOT VALID JSON {{{")
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"token_endpoint": "/auth/token"}
        with pytest.raises((SystemExit, ClickExit)):
            mod._oauth_bearer(profile, auth_config)


# ===========================================================================
# 2. _try_refresh_token
# ===========================================================================


class TestTryRefreshToken:
    """Tests for _try_refresh_token (lines 449-476)."""

    def test_successful_refresh(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"refresh_endpoint": "/auth/refresh"}
        cached = {"access_token": "old", "refresh_token": "rt-123"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "refreshed-token",
            "expires_in": 7200,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._try_refresh_token(profile, auth_config, cached)

        assert result is not None
        assert result["access_token"] == "refreshed-token"
        # Verify cache was written
        token_file = cache_dir / "myapi_token.json"
        assert token_file.exists()

    def test_refresh_non_200(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"refresh_endpoint": "/auth/refresh"}
        cached = {"access_token": "old", "refresh_token": "rt-123"}

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._try_refresh_token(profile, auth_config, cached)
        assert result is None

    def test_no_refresh_endpoint(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {}
        cached = {"access_token": "old", "refresh_token": "rt-123"}
        result = mod._try_refresh_token(profile, auth_config, cached)
        assert result is None

    def test_network_error(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"refresh_endpoint": "/auth/refresh"}
        cached = {"access_token": "old", "refresh_token": "rt-123"}

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.side_effect = httpx.HTTPError("Connection refused")
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._try_refresh_token(profile, auth_config, cached)
        assert result is None

    def test_refresh_default_expiry_when_no_expires_in(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"_name": "myapi", "base_url": "http://localhost"}
        auth_config = {"refresh_endpoint": "/auth/refresh"}
        cached = {"access_token": "old", "refresh_token": "rt-123"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "refreshed-token"}
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._try_refresh_token(profile, auth_config, cached)

        assert result is not None
        # Should have a default 24h expiry
        assert "expires_at" in result
        assert result["expires_at"] > time.time() + 86000


# ===========================================================================
# 3. _oidc_auth with refresh
# ===========================================================================


class TestOidcAuth:
    """Tests for _oidc_auth (lines 482-511)."""

    def test_valid_cached_token(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "oidcprof",
            {
                "access_token": "valid-oidc-token",
                "expires_at": time.time() + 3600,
            },
        )
        profile = {"_name": "oidcprof"}
        auth_config = {
            "type": "oidc",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        result = mod._oidc_auth(profile, auth_config)
        assert result == {"Authorization": "Bearer valid-oidc-token"}

    def test_expired_refresh_succeeds(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "oidcprof",
            {
                "access_token": "expired-token",
                "refresh_token": "oidc-rt-123",
                "expires_at": time.time() - 100,
            },
        )
        profile = {"_name": "oidcprof", "verify_ssl": True}
        auth_config = {
            "type": "oidc",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "refreshed-oidc-token",
            "expires_in": 1800,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod._oidc_auth(profile, auth_config)
        assert result == {"Authorization": "Bearer refreshed-oidc-token"}

    def test_expired_refresh_fails(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_token_cache(
            cache_dir,
            "oidcprof",
            {
                "access_token": "expired-token",
                "refresh_token": "oidc-rt-123",
                "expires_at": time.time() - 100,
            },
        )
        profile = {"_name": "oidcprof", "verify_ssl": True}
        auth_config = {
            "type": "oidc",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises((SystemExit, ClickExit)):
                mod._oidc_auth(profile, auth_config)

    def test_corrupted_cache_prompts_login(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        token_file = cache_dir / "oidcprof_token.json"
        token_file.write_text("INVALID JSON")
        profile = {"_name": "oidcprof"}
        auth_config = {"type": "oidc", "token_url": "https://x", "client_id": "c"}
        with pytest.raises((SystemExit, ClickExit)):
            mod._oidc_auth(profile, auth_config)


# ===========================================================================
# 4. _oidc_refresh
# ===========================================================================


class TestOidcRefresh:
    """Tests for _oidc_refresh (lines 514-534)."""

    def test_successful_refresh(self):
        auth_config = {
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        cached = {"refresh_token": "rt-456"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-oidc",
            "expires_in": 3600,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = cli_mod._oidc_refresh(auth_config, cached)
        assert result == {"access_token": "new-oidc", "expires_in": 3600}

    def test_non_200(self):
        auth_config = {
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        cached = {"refresh_token": "rt-456"}
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = cli_mod._oidc_refresh(auth_config, cached)
        assert result is None

    def test_network_error(self):
        auth_config = {
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        cached = {"refresh_token": "rt-456"}
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.side_effect = httpx.HTTPError("timeout")
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = cli_mod._oidc_refresh(auth_config, cached)
        assert result is None

    def test_missing_token_url(self):
        auth_config = {"client_id": "my-client"}
        cached = {"refresh_token": "rt-456"}
        result = cli_mod._oidc_refresh(auth_config, cached)
        assert result is None

    def test_missing_client_id(self):
        auth_config = {"token_url": "https://idp.example.com/token"}
        cached = {"refresh_token": "rt-456"}
        result = cli_mod._oidc_refresh(auth_config, cached)
        assert result is None


# ===========================================================================
# 5. _oidc_login
# ===========================================================================


class TestOidcLogin:
    """Tests for _oidc_login (lines 582-640)."""

    def test_browser_mode(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        auth_config = {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
            "scopes": "openid profile",
        }
        with (
            patch.object(mod, "_oidc_login_browser", return_value="auth-code-123"),
            patch.object(mod, "_oidc_exchange_code") as mock_exchange,
        ):
            mod._oidc_login(auth_config, "testprof", no_browser=False, verify=True)
        mock_exchange.assert_called_once()
        call_kwargs = mock_exchange.call_args
        assert call_kwargs.kwargs["auth_code"] == "auth-code-123"

    def test_no_browser_mode(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        auth_config = {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        with (
            patch.object(mod, "_oidc_login_no_browser", return_value="nb-code-456"),
            patch.object(mod, "_oidc_exchange_code") as mock_exchange,
        ):
            mod._oidc_login(auth_config, "testprof", no_browser=True, verify=True)
        mock_exchange.assert_called_once()
        call_kwargs = mock_exchange.call_args
        assert call_kwargs.kwargs["auth_code"] == "nb-code-456"

    def test_missing_authorize_url(self):
        auth_config = {
            "token_url": "https://idp.example.com/token",
            "client_id": "my-client",
        }
        with pytest.raises((SystemExit, ClickExit)):
            cli_mod._oidc_login(auth_config, "testprof")

    def test_missing_token_url(self):
        auth_config = {
            "authorize_url": "https://idp.example.com/authorize",
            "client_id": "my-client",
        }
        with pytest.raises((SystemExit, ClickExit)):
            cli_mod._oidc_login(auth_config, "testprof")

    def test_missing_client_id(self):
        auth_config = {
            "authorize_url": "https://idp.example.com/authorize",
            "token_url": "https://idp.example.com/token",
        }
        with pytest.raises((SystemExit, ClickExit)):
            cli_mod._oidc_login(auth_config, "testprof")


# ===========================================================================
# 6. _oidc_login_browser
# ===========================================================================


class TestOidcLoginBrowser:
    """Tests for _oidc_login_browser (lines 643-666)."""

    def test_successful_callback(self):
        with patch("openapi_cli4ai.cli.HTTPServer") as MockServer, patch("openapi_cli4ai.cli.webbrowser"):
            mock_server = MagicMock()
            MockServer.return_value = mock_server

            def handle_request_side_effect():
                cli_mod._OIDCCallbackHandler.auth_code = "browser-code-789"
                cli_mod._OIDCCallbackHandler.error = None

            mock_server.handle_request.side_effect = handle_request_side_effect

            result = cli_mod._oidc_login_browser("https://idp/auth?...", 8484, "state-abc")

        assert result == "browser-code-789"

    def test_callback_with_error(self):
        with patch("openapi_cli4ai.cli.HTTPServer") as MockServer, patch("openapi_cli4ai.cli.webbrowser"):
            mock_server = MagicMock()
            MockServer.return_value = mock_server

            def handle_request_side_effect():
                cli_mod._OIDCCallbackHandler.auth_code = None
                cli_mod._OIDCCallbackHandler.error = "access_denied"

            mock_server.handle_request.side_effect = handle_request_side_effect

            with pytest.raises((SystemExit, ClickExit)):
                cli_mod._oidc_login_browser("https://idp/auth?...", 8484, "state-abc")

    def test_no_auth_code_received(self):
        with patch("openapi_cli4ai.cli.HTTPServer") as MockServer, patch("openapi_cli4ai.cli.webbrowser"):
            mock_server = MagicMock()
            MockServer.return_value = mock_server

            def handle_request_side_effect():
                cli_mod._OIDCCallbackHandler.auth_code = None
                cli_mod._OIDCCallbackHandler.error = None

            mock_server.handle_request.side_effect = handle_request_side_effect

            with pytest.raises((SystemExit, ClickExit)):
                cli_mod._oidc_login_browser("https://idp/auth?...", 8484, "state-abc")


# ===========================================================================
# 7. _oidc_login_no_browser
# ===========================================================================


class TestOidcLoginNoBrowser:
    """Tests for _oidc_login_no_browser (lines 669-692)."""

    def test_valid_redirect_url(self):
        redirect_url = "http://localhost:8484/callback?code=nb-code-abc&state=s123"
        with patch("typer.prompt", return_value=redirect_url):
            result = cli_mod._oidc_login_no_browser("https://idp/auth?...")
        assert result == "nb-code-abc"

    def test_redirect_url_with_error(self):
        redirect_url = "http://localhost:8484/callback?error=access_denied"
        with patch("typer.prompt", return_value=redirect_url):
            with pytest.raises((SystemExit, ClickExit)):
                cli_mod._oidc_login_no_browser("https://idp/auth?...")

    def test_redirect_url_without_code(self):
        redirect_url = "http://localhost:8484/callback?state=s123"
        with patch("typer.prompt", return_value=redirect_url):
            with pytest.raises((SystemExit, ClickExit)):
                cli_mod._oidc_login_no_browser("https://idp/auth?...")


# ===========================================================================
# 8. _oidc_exchange_code
# ===========================================================================


class TestOidcExchangeCode:
    """Tests for _oidc_exchange_code (lines 695-747)."""

    def test_successful_exchange(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "exchanged-token",
            "refresh_token": "exchanged-rt",
            "expires_in": 3600,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mod._oidc_exchange_code(
                token_url="https://idp.example.com/token",
                client_id="my-client",
                auth_code="auth-code-xyz",
                redirect_uri="http://localhost:8484/callback",
                code_verifier="verifier-123",
                profile_name="exchprof",
                verify=True,
            )
        token_file = cache_dir / "exchprof_token.json"
        assert token_file.exists()
        cached = json.loads(token_file.read_text())
        assert cached["access_token"] == "exchanged-token"
        assert "expires_at" in cached

    def test_non_200_response(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises((SystemExit, ClickExit)):
                mod._oidc_exchange_code(
                    token_url="https://idp.example.com/token",
                    client_id="my-client",
                    auth_code="bad-code",
                    redirect_uri="http://localhost:8484/callback",
                    code_verifier="verifier-123",
                    profile_name="exchprof",
                    verify=True,
                )

    def test_connect_error(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        import httpx as httpx_mod

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.side_effect = httpx_mod.ConnectError("Connection refused")
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises((SystemExit, ClickExit)):
                mod._oidc_exchange_code(
                    token_url="https://idp.example.com/token",
                    client_id="my-client",
                    auth_code="code-123",
                    redirect_uri="http://localhost:8484/callback",
                    code_verifier="verifier-123",
                    profile_name="exchprof",
                    verify=True,
                )

    def test_with_token_exchange_endpoint(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "idp-token",
            "expires_in": 3600,
        }
        exchanged_data = {
            "access_token": "local-api-token",
            "expires_in": 7200,
        }
        auth_config = {"token_exchange_endpoint": "/api/token-exchange"}
        with (
            patch("httpx.Client") as MockClient,
            patch.object(mod, "_token_exchange", return_value=exchanged_data) as mock_te,
        ):
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mod._oidc_exchange_code(
                token_url="https://idp.example.com/token",
                client_id="my-client",
                auth_code="code-xyz",
                redirect_uri="http://localhost:8484/callback",
                code_verifier="verifier-123",
                profile_name="exchprof",
                verify=True,
                auth_config=auth_config,
                base_url="http://localhost:8000",
            )
        mock_te.assert_called_once()
        token_file = cache_dir / "exchprof_token.json"
        cached = json.loads(token_file.read_text())
        assert cached["access_token"] == "local-api-token"

    def test_default_24h_expiry_when_no_expires_in(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "token-no-expiry"}
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mod._oidc_exchange_code(
                token_url="https://idp.example.com/token",
                client_id="my-client",
                auth_code="code-xyz",
                redirect_uri="http://localhost:8484/callback",
                code_verifier="verifier-123",
                profile_name="exchprof",
                verify=True,
            )
        token_file = cache_dir / "exchprof_token.json"
        cached = json.loads(token_file.read_text())
        assert cached["expires_at"] >= time.time() + 86000

    def test_respects_expires_in(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "token-with-exp",
            "expires_in": 1800,
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.post.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mod._oidc_exchange_code(
                token_url="https://idp.example.com/token",
                client_id="my-client",
                auth_code="code-xyz",
                redirect_uri="http://localhost:8484/callback",
                code_verifier="verifier-123",
                profile_name="exchprof",
                verify=True,
            )
        token_file = cache_dir / "exchprof_token.json"
        cached = json.loads(token_file.read_text())
        # expires_at should be roughly now + 1800
        assert cached["expires_at"] < time.time() + 1900
        assert cached["expires_at"] > time.time() + 1700


# ===========================================================================
# 9. fetch_spec
# ===========================================================================


class TestFetchSpec:
    """Tests for fetch_spec (lines 197-256)."""

    def _write_spec_cache(self, cache_dir, profile, spec, fetched_at=None):
        """Helper to write spec cache files matching how fetch_spec expects them."""
        import hashlib

        spec_url = cli_mod._resolve_spec_url(profile)
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        cache_file = cache_dir / f"spec_{url_hash}.json"
        meta_file = cache_dir / f"spec_{url_hash}.meta"
        cache_file.write_text(json.dumps(spec))
        meta = {"fetched_at": fetched_at or time.time(), "url": spec_url}
        meta_file.write_text(json.dumps(meta))
        return cache_file, meta_file

    def test_fresh_cache(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        spec = {"openapi": "3.0.0", "paths": {}}
        self._write_spec_cache(cache_dir, profile, spec)

        result = mod.fetch_spec(profile)
        assert result == spec

    def test_stale_cache_fetches_fresh(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        old_spec = {"openapi": "3.0.0", "paths": {"/old": {}}}
        self._write_spec_cache(cache_dir, profile, old_spec, fetched_at=time.time() - 7200)

        new_spec = {"openapi": "3.0.0", "paths": {"/new": {}}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = new_spec
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = json.dumps(new_spec)

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod.fetch_spec(profile)
        assert result == new_spec

    def test_no_cache_fetches_fresh(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        spec = {"openapi": "3.0.0", "paths": {"/fresh": {}}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = spec
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = json.dumps(spec)

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod.fetch_spec(profile)
        assert result == spec

    def test_network_error_with_stale_cache(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        stale_spec = {"openapi": "3.0.0", "paths": {"/stale": {}}}
        self._write_spec_cache(cache_dir, profile, stale_spec, fetched_at=time.time() - 7200)

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.side_effect = httpx.HTTPError("Network error")
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod.fetch_spec(profile)
        assert result == stale_spec

    def test_network_error_no_cache_exits(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.side_effect = httpx.HTTPError("Network error")
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises((SystemExit, ClickExit)):
                mod.fetch_spec(profile)

    def test_html_response_raises_error(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html; charset=utf-8"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises((SystemExit, ClickExit)):
                mod.fetch_spec(profile)

    def test_yaml_spec(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {
            "base_url": "https://api.example.com",
            "openapi_path": "/openapi.yaml",
            "auth": {"type": "none"},
        }
        yaml_text = "openapi: '3.0.0'\npaths:\n  /yaml:\n    get:\n      summary: test\n"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/x-yaml"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = yaml_text

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod.fetch_spec(profile)
        assert result["openapi"] == "3.0.0"
        assert "/yaml" in result["paths"]

    def test_corrupted_cache_fetches_fresh(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        profile = {"base_url": "https://api.example.com", "auth": {"type": "none"}}
        # Write corrupted cache
        import hashlib

        spec_url = cli_mod._resolve_spec_url(profile)
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        cache_file = cache_dir / f"spec_{url_hash}.json"
        meta_file = cache_dir / f"spec_{url_hash}.meta"
        cache_file.write_text("INVALID JSON")
        meta_file.write_text(json.dumps({"fetched_at": time.time(), "url": spec_url}))

        fresh_spec = {"openapi": "3.0.0", "paths": {"/fresh": {}}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = fresh_spec
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.get.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = mod.fetch_spec(profile)
        assert result == fresh_spec


# ===========================================================================
# 10. load_profiles
# ===========================================================================


class TestLoadProfiles:
    """Tests for load_profiles (lines 119-131)."""

    def test_config_doesnt_exist(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        # CONFIG_FILE doesn't exist yet
        result = mod.load_profiles()
        assert result == {"active_profile": None, "profiles": {}}

    def test_valid_toml(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        data = {
            "active_profile": "myapi",
            "profiles": {
                "myapi": {"base_url": "http://localhost"},
            },
        }
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        result = mod.load_profiles()
        assert result["active_profile"] == "myapi"
        assert "myapi" in result["profiles"]

    def test_invalid_toml(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        mod.CONFIG_FILE.write_text("this is not valid TOML [[[[")
        with pytest.raises((SystemExit, ClickExit)):
            mod.load_profiles()

    def test_toml_without_profiles_key(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        data = {"active_profile": "myapi"}
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        result = mod.load_profiles()
        assert result["profiles"] == {}


# ===========================================================================
# 11. get_active_profile
# ===========================================================================


class TestGetActiveProfile:
    """Tests for get_active_profile (lines 156-178)."""

    def test_no_profiles_exits(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        data = {"profiles": {}}
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        with pytest.raises((SystemExit, ClickExit)):
            mod.get_active_profile()

    def test_env_var_override(self, tmp_config, monkeypatch):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        data = {
            "active_profile": "first",
            "profiles": {
                "first": {"base_url": "http://first.example.com"},
                "second": {"base_url": "http://second.example.com"},
            },
        }
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        monkeypatch.setenv("OAC_PROFILE", "second")
        name, profile = mod.get_active_profile()
        assert name == "second"
        assert profile["base_url"] == "http://second.example.com"

    def test_active_profile_from_config(self, tmp_config, monkeypatch):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        monkeypatch.delenv("OAC_PROFILE", raising=False)
        data = {
            "active_profile": "myapi",
            "profiles": {
                "myapi": {"base_url": "http://localhost"},
                "other": {"base_url": "http://other.example.com"},
            },
        }
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        name, profile = mod.get_active_profile()
        assert name == "myapi"

    def test_nonexistent_active_profile_exits(self, tmp_config, monkeypatch):
        mod, tmp_path, cache_dir = tmp_config
        import tomli_w

        monkeypatch.delenv("OAC_PROFILE", raising=False)
        data = {
            "active_profile": "nonexistent",
            "profiles": {
                "first": {"base_url": "http://first.example.com"},
                "second": {"base_url": "http://second.example.com"},
            },
        }
        mod.CONFIG_FILE.write_text(tomli_w.dumps(data))
        with pytest.raises((SystemExit, ClickExit)):
            mod.get_active_profile()


# ===========================================================================
# 12. _resolve_env_vars
# ===========================================================================


class TestResolveEnvVars:
    """Tests for _resolve_env_vars (lines 142-153)."""

    def test_string_with_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "hello")
        result = cli_mod._resolve_env_vars("prefix-{env:MY_VAR}-suffix")
        assert result == "prefix-hello-suffix"

    def test_dict_with_nested_env_vars(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "8080")
        data = {"url": "http://{env:HOST}:{env:PORT}", "name": "test"}
        result = cli_mod._resolve_env_vars(data)
        assert result["url"] == "http://localhost:8080"
        assert result["name"] == "test"

    def test_list_with_env_vars(self, monkeypatch):
        monkeypatch.setenv("ITEM", "resolved")
        data = ["no-env", "{env:ITEM}"]
        result = cli_mod._resolve_env_vars(data)
        assert result == ["no-env", "resolved"]

    def test_no_env_vars_unchanged(self):
        result = cli_mod._resolve_env_vars("plain string")
        assert result == "plain string"

    def test_missing_env_var_resolves_to_empty(self, monkeypatch):
        monkeypatch.delenv("NONEXISTENT_VAR_XYZ", raising=False)
        result = cli_mod._resolve_env_vars("{env:NONEXISTENT_VAR_XYZ}")
        assert result == ""

    def test_non_string_passthrough(self):
        assert cli_mod._resolve_env_vars(42) == 42
        assert cli_mod._resolve_env_vars(None) is None


# ===========================================================================
# 13. handle_response
# ===========================================================================


class TestHandleResponse:
    """Tests for handle_response (lines 1064-1098)."""

    def _make_response(self, status_code=200, content_type="application/json", json_data=None, text=None, reason="OK"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = {"content-type": content_type}
        resp.reason_phrase = reason
        if json_data is not None:
            resp.json.return_value = json_data
            resp.text = text or json.dumps(json_data)
        else:
            resp.json.side_effect = json.JSONDecodeError("", "", 0)
            resp.text = text or ""
        return resp

    def test_200_json_response(self, capsys):
        resp = self._make_response(json_data={"data": "test"})
        cli_mod.handle_response(resp)
        # No assertion on exact output formatting, just verify no exception

    def test_200_json_raw_mode(self, capsys):
        resp = self._make_response(json_data={"data": "test"})
        cli_mod.handle_response(resp, raw=True)
        captured = capsys.readouterr()
        assert "data" in captured.out

    def test_200_json_json_output(self, capsys):
        resp = self._make_response(json_data={"key": "value"})
        cli_mod.handle_response(resp, json_output=True)
        captured = capsys.readouterr()
        # The output contains the JSON followed by the status line from Rich console
        # Extract just the JSON portion
        assert '"key": "value"' in captured.out

    def test_400_json_error(self):
        resp = self._make_response(status_code=400, json_data={"message": "Bad Request"}, reason="Bad Request")
        # Should call _display_error, not raise
        cli_mod.handle_response(resp)

    def test_non_json_response(self):
        resp = self._make_response(content_type="text/plain", text="Hello plain")
        cli_mod.handle_response(resp)

    def test_json_decode_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.json.side_effect = json.JSONDecodeError("err", "", 0)
        resp.text = "not really json"
        resp.reason_phrase = "OK"
        cli_mod.handle_response(resp)


# ===========================================================================
# 14. _display_error
# ===========================================================================


class TestDisplayError:
    """Tests for _display_error (lines 1101-1119)."""

    def test_dict_with_message(self):
        cli_mod._display_error({"message": "Something went wrong"}, 400)

    def test_dict_with_error(self):
        cli_mod._display_error({"error": "unauthorized"}, 401)

    def test_dict_with_detail(self):
        cli_mod._display_error({"detail": "Not found"}, 404)

    def test_string_error(self):
        cli_mod._display_error("Raw error string", 500)

    def test_dict_with_errors_list(self):
        cli_mod._display_error(
            {
                "message": "Validation failed",
                "errors": ["field1 is required", "field2 must be positive"],
            },
            422,
        )

    def test_dict_with_documentation_url(self):
        cli_mod._display_error(
            {
                "message": "Rate limited",
                "documentation_url": "https://docs.example.com/rate-limits",
            },
            429,
        )

    def test_dict_fallback_to_str(self):
        cli_mod._display_error({"unknown_key": "val"}, 500)


# ===========================================================================
# 15. make_request
# ===========================================================================


class TestMakeRequest:
    """Tests for make_request (lines 1029-1061)."""

    def test_simple_get(self):
        profile = {
            "base_url": "https://api.example.com",
            "auth": {"type": "none"},
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.request.return_value = mock_resp
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            result = cli_mod.make_request(profile, "GET", "/users")
        assert result.status_code == 200
        mc.request.assert_called_once()
        call_kwargs = mc.request.call_args
        assert call_kwargs.kwargs["method"] == "GET"
        assert "api.example.com/users" in call_kwargs.kwargs["url"]

    def test_with_extra_headers(self):
        profile = {
            "base_url": "https://api.example.com",
            "auth": {"type": "none"},
            "headers": {"Accept": "application/json"},
        }
        with patch("httpx.Client") as MockClient:
            mc = MagicMock()
            mc.request.return_value = MagicMock(status_code=200)
            MockClient.return_value.__enter__ = MagicMock(return_value=mc)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            cli_mod.make_request(profile, "POST", "/data", extra_headers={"X-Custom": "val"})
        call_kwargs = mc.request.call_args
        assert call_kwargs.kwargs["headers"]["X-Custom"] == "val"
        assert call_kwargs.kwargs["headers"]["Accept"] == "application/json"

    def test_stream_param_removed(self):
        """make_request no longer accepts a stream parameter; streaming is
        handled inline in cmd_call/cmd_run."""
        profile = {
            "base_url": "https://api.example.com",
            "auth": {"type": "none"},
        }
        with pytest.raises(TypeError, match="stream"):
            cli_mod.make_request(profile, "GET", "/stream", stream=True)


# ===========================================================================
# 16. _get_password
# ===========================================================================


class TestGetPassword:
    """Tests for _get_password (lines 1006-1025)."""

    def test_from_env_var(self, monkeypatch):
        monkeypatch.setenv("MY_PASS", "secret123")
        auth_config = {"password_env_var": "MY_PASS"}
        result = cli_mod._get_password(auth_config)
        assert result == "secret123"

    def test_from_password_file(self, tmp_path):
        pf = tmp_path / "password.txt"
        pf.write_text("file-secret\n")
        auth_config = {"password_file": str(pf)}
        result = cli_mod._get_password(auth_config)
        assert result == "file-secret"

    def test_from_stdin_piped(self, monkeypatch):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        mock_stdin.readline.return_value = "piped-secret\n"
        monkeypatch.setattr(sys, "stdin", mock_stdin)
        auth_config = {}
        result = cli_mod._get_password(auth_config)
        assert result == "piped-secret"

    def test_interactive_prompt(self, monkeypatch):
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        monkeypatch.setattr(sys, "stdin", mock_stdin)
        auth_config = {}
        with patch("typer.prompt", return_value="prompted-secret"):
            result = cli_mod._get_password(auth_config)
        assert result == "prompted-secret"


# ===========================================================================
# 17. set_insecure_mode / get_verify_ssl
# ===========================================================================


class TestInsecureMode:
    """Tests for set_insecure_mode and get_verify_ssl (lines 103-109)."""

    def test_set_insecure_returns_false(self):
        cli_mod.set_insecure_mode(True)
        assert cli_mod.get_verify_ssl() is False
        # Reset
        cli_mod.set_insecure_mode(False)

    def test_default_returns_true(self):
        cli_mod.set_insecure_mode(False)
        assert cli_mod.get_verify_ssl() is True


# ===========================================================================
# 18. _resolve_spec_url / _spec_cache_paths
# ===========================================================================


class TestResolveSpecUrl:
    """Tests for _resolve_spec_url and _spec_cache_paths (lines 182-194)."""

    def test_with_openapi_url(self):
        profile = {
            "base_url": "https://api.example.com",
            "openapi_url": "https://raw.example.com/spec.json",
        }
        result = cli_mod._resolve_spec_url(profile)
        assert result == "https://raw.example.com/spec.json"

    def test_with_openapi_path(self):
        profile = {
            "base_url": "https://api.example.com",
            "openapi_path": "/v3/api-docs",
        }
        result = cli_mod._resolve_spec_url(profile)
        assert result == "https://api.example.com/v3/api-docs"

    def test_default_path(self):
        profile = {"base_url": "https://api.example.com"}
        result = cli_mod._resolve_spec_url(profile)
        assert result == "https://api.example.com/openapi.json"

    def test_trailing_slash_base_url(self):
        profile = {"base_url": "https://api.example.com/"}
        result = cli_mod._resolve_spec_url(profile)
        assert result == "https://api.example.com/openapi.json"

    def test_spec_cache_paths_deterministic(self):
        cache1, meta1 = cli_mod._spec_cache_paths("https://api.example.com/openapi.json")
        cache2, meta2 = cli_mod._spec_cache_paths("https://api.example.com/openapi.json")
        assert cache1 == cache2
        assert meta1 == meta2

    def test_spec_cache_paths_different_urls(self):
        cache1, _ = cli_mod._spec_cache_paths("https://api1.example.com/openapi.json")
        cache2, _ = cli_mod._spec_cache_paths("https://api2.example.com/openapi.json")
        assert cache1 != cache2
