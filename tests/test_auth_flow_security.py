"""Auth flow and response handling tests for openapi-cli4ai.

Tests for _oauth_bearer, _try_refresh_token, _get_password,
handle_response, and _display_error.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from openapi_cli4ai import cli as cli_mod


# ── OAuth Bearer Flow Tests ──────────────────────────────────────────────────


class TestOAuthBearer:
    """Tests for _oauth_bearer() token management."""

    def test_cached_valid_token_returns_headers(self, tmp_config):
        """Valid cached token should return auth headers without network call."""
        cli, config_path, cache_dir = tmp_config

        profile = {
            "base_url": "http://localhost:8000",
            "auth": {"type": "bearer", "token_endpoint": "/token"},
            "verify_ssl": True,
            "_name": "testprofile",
        }

        # Write a valid cached token
        token_cache = cache_dir / "testprofile_token.json"
        token_data = {
            "access_token": "valid_token_123",
            "expires_at": time.time() + 3600,
        }
        token_cache.write_text(json.dumps(token_data))

        headers = cli._oauth_bearer(profile, profile["auth"])
        assert headers == {"Authorization": "Bearer valid_token_123"}

    def test_expired_token_prompts_login(self, tmp_config):
        """Expired token with no refresh should prompt login."""
        cli, config_path, cache_dir = tmp_config

        profile = {
            "base_url": "http://localhost:8000",
            "auth": {"type": "bearer", "token_endpoint": "/token"},
            "verify_ssl": True,
            "_name": "testprofile",
        }

        # Write an expired token
        token_cache = cache_dir / "testprofile_token.json"
        token_data = {
            "access_token": "expired_token",
            "expires_at": time.time() - 3600,
        }
        token_cache.write_text(json.dumps(token_data))

        import click

        with pytest.raises(click.exceptions.Exit):
            cli._oauth_bearer(profile, profile["auth"])


# ── Try Refresh Token Tests ──────────────────────────────────────────────────


class TestTryRefreshToken:
    """Tests for _try_refresh_token()."""

    def test_successful_refresh(self, tmp_config):
        """Successful refresh should return new token data."""
        cli, config_path, cache_dir = tmp_config

        profile = {
            "base_url": "http://localhost:8000",
            "auth": {"refresh_endpoint": "/refresh"},
            "verify_ssl": True,
            "_name": "testprofile",
        }
        cached = {"refresh_token": "old_refresh", "access_token": "old_access"}

        new_token = {"access_token": "new_access", "expires_in": 3600}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = new_token

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = cli._try_refresh_token(profile, profile["auth"], cached)

        assert result is not None
        assert result["access_token"] == "new_access"
        assert "expires_at" in result

    def test_failed_refresh_returns_none(self, tmp_config):
        """Failed refresh (non-200) should return None."""
        cli, config_path, cache_dir = tmp_config

        profile = {
            "base_url": "http://localhost:8000",
            "auth": {"refresh_endpoint": "/refresh"},
            "verify_ssl": True,
            "_name": "testprofile",
        }
        cached = {"refresh_token": "bad_refresh", "access_token": "old_access"}

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = cli._try_refresh_token(profile, profile["auth"], cached)

        assert result is None

    def test_network_error_returns_none(self, tmp_config):
        """Network error during refresh should return None, not crash."""
        import httpx

        cli, config_path, cache_dir = tmp_config

        profile = {
            "base_url": "http://localhost:99999",
            "auth": {"refresh_endpoint": "/refresh"},
            "verify_ssl": True,
            "_name": "testprofile",
        }
        cached = {"refresh_token": "token", "access_token": "access"}

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("mocked")
            mock_mc.return_value = mock_client

            result = cli._try_refresh_token(profile, profile["auth"], cached)
        assert result is None


# ── Response Handling Tests ──────────────────────────────────────────────────


class TestHandleResponse:
    """Tests for handle_response()."""

    def test_200_json_response(self, capsys):
        """200 JSON response should display formatted JSON."""
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"id": 1, "name": "Rex"}
        response.reason_phrase = "OK"

        cli_mod.handle_response(response)
        captured = capsys.readouterr()
        assert "200" in captured.out

    def test_200_text_response(self, capsys):
        """200 text response should display raw text."""
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "text/plain"}
        response.text = "Hello, World!"
        response.reason_phrase = "OK"

        cli_mod.handle_response(response)
        captured = capsys.readouterr()
        assert "Hello, World!" in captured.out

    def test_400_json_error(self, capsys):
        """400 JSON error should display formatted error."""
        response = MagicMock()
        response.status_code = 400
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"message": "Bad request"}
        response.reason_phrase = "Bad Request"

        cli_mod.handle_response(response)
        captured = capsys.readouterr()
        assert "400" in captured.out

    def test_raw_flag(self, capsys):
        """--raw should print response text without formatting."""
        response = MagicMock()
        response.status_code = 200
        response.text = '{"raw": true}'

        cli_mod.handle_response(response, raw=True)
        captured = capsys.readouterr()
        assert '{"raw": true}' in captured.out

    def test_json_output_flag(self, capsys):
        """--json should output indented JSON."""
        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"id": 1}
        response.reason_phrase = "OK"

        cli_mod.handle_response(response, json_output=True)
        captured = capsys.readouterr()
        assert '"id": 1' in captured.out


# ── Display Error Tests ──────────────────────────────────────────────────────


class TestDisplayError:
    """Tests for _display_error()."""

    def test_dict_with_message_key(self, capsys):
        """Error dict with 'message' key should display message."""
        cli_mod._display_error({"message": "Something went wrong"}, 400)
        captured = capsys.readouterr()
        assert "Something went wrong" in captured.out

    def test_dict_with_error_key(self, capsys):
        """Error dict with 'error' key should display error."""
        cli_mod._display_error({"error": "unauthorized"}, 401)
        captured = capsys.readouterr()
        assert "unauthorized" in captured.out

    def test_dict_with_nested_errors(self, capsys):
        """Error dict with 'errors' list should display each."""
        cli_mod._display_error(
            {"message": "Validation failed", "errors": ["Name required", "Email invalid"]},
            422,
        )
        captured = capsys.readouterr()
        assert "Validation failed" in captured.out
        assert "Name required" in captured.out
        assert "Email invalid" in captured.out

    def test_dict_with_documentation_url(self, capsys):
        """Error dict with 'documentation_url' should display it."""
        cli_mod._display_error(
            {"message": "Not found", "documentation_url": "https://docs.example.com/errors"},
            404,
        )
        captured = capsys.readouterr()
        assert "docs.example.com" in captured.out

    def test_non_dict_error(self, capsys):
        """Non-dict error should be displayed as string."""
        cli_mod._display_error("Something broke", 500)
        captured = capsys.readouterr()
        assert "Something broke" in captured.out
