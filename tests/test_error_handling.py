"""Error handling tests for openapi-cli4ai.

Tests that error handlers catch specific exceptions and resources are cleaned up.
"""

from __future__ import annotations

import ast
import inspect
import socket
from unittest.mock import MagicMock, patch

import click
import pytest

from openapi_cli4ai import cli as cli_mod


# ── Specific Exception Tests ─────────────────────────────────────────────────


class TestSpecificExceptions:
    """Verify that error handlers catch specific exceptions, not bare Exception."""

    def test_fetch_spec_catches_httpx_errors(self, tmp_config):
        """fetch_spec should catch httpx-specific errors, not bare Exception."""
        import httpx

        cli, config_path, cache_dir = tmp_config
        profile = {
            "base_url": "http://localhost:99999",
            "openapi_path": "/openapi.json",
            "auth": {"type": "none"},
            "_name": "test",
        }

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = httpx.ConnectError("mocked connection error")
            mock_mc.return_value = mock_client

            with pytest.raises(click.exceptions.Exit):
                cli.fetch_spec(profile, refresh=True)

    def test_try_refresh_token_catches_specific_errors(self, tmp_config):
        """_try_refresh_token should catch httpx errors, not bare Exception."""
        import httpx

        cli, config_path, cache_dir = tmp_config
        profile = {
            "base_url": "http://localhost:99999",
            "auth": {"refresh_endpoint": "/refresh"},
            "verify_ssl": True,
            "_name": "test",
        }
        cached = {"refresh_token": "old_refresh", "access_token": "old_access"}

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("mocked")
            mock_mc.return_value = mock_client

            result = cli._try_refresh_token(profile, profile["auth"], cached)
        assert result is None

    def test_oidc_refresh_catches_specific_errors(self):
        """_oidc_refresh should catch httpx errors, not bare Exception."""
        import httpx

        auth_config = {"token_url": "http://localhost:99999/token", "client_id": "test"}
        cached = {"refresh_token": "old_refresh"}

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("mocked")
            mock_mc.return_value = mock_client

            result = cli_mod._oidc_refresh(auth_config, cached, verify=True)
        assert result is None

    def test_no_bare_except_exception_in_source(self):
        """cli.py should not contain any bare 'except Exception' clauses."""
        source = inspect.getsource(cli_mod)
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                    violations.append(f"Line {node.lineno}")
        assert not violations, f"Found bare except Exception at: {violations}"


# ── OIDC Server Cleanup Tests ────────────────────────────────────────────────


class TestOIDCServerCleanup:
    """Verify that _oidc_login_browser handles server lifecycle correctly."""

    def test_port_conflict_exits_cleanly(self):
        """When the OIDC callback port is in use, exit with helpful error."""
        # Bind a port to create a conflict
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.listen(1)

            # Calling _oidc_login_browser on this port should exit cleanly
            with pytest.raises(click.exceptions.Exit):
                cli_mod._oidc_login_browser("http://auth.example.com/authorize", port, "test_state")
        finally:
            sock.close()

    def test_server_closed_after_handler(self):
        """HTTPServer should be closed even if handle_request completes normally."""

        def fake_handle_request():
            # Simulate what a real callback would do
            cli_mod._OIDCCallbackHandler.auth_code = "test_code"

        with patch("openapi_cli4ai.cli.HTTPServer") as mock_server_cls:
            mock_server = MagicMock()
            mock_server_cls.return_value = mock_server
            mock_server.handle_request.side_effect = fake_handle_request

            with patch("openapi_cli4ai.cli.webbrowser"):
                result = cli_mod._oidc_login_browser("http://auth.example.com", 9999, "s")

            mock_server.server_close.assert_called_once()
            assert result == "test_code"

    def test_custom_callback_timeout(self):
        """_oidc_login_browser should use the timeout parameter on the server."""

        def fake_handle_request():
            cli_mod._OIDCCallbackHandler.auth_code = "code"

        with patch("openapi_cli4ai.cli.HTTPServer") as mock_server_cls:
            mock_server = MagicMock()
            mock_server_cls.return_value = mock_server
            mock_server.handle_request.side_effect = fake_handle_request

            with patch("openapi_cli4ai.cli.webbrowser"):
                cli_mod._oidc_login_browser("http://auth.example.com", 9999, "s", timeout=45)

            # Verify the custom timeout was applied
            assert mock_server.timeout == 45


# ── Redundant Import Test ────────────────────────────────────────────────────


class TestRedundantImport:
    """Verify that 're' is only imported once (no late/redundant imports)."""

    def test_re_not_imported_twice(self):
        """The 're' module should only be imported once at the top of cli.py."""
        source = inspect.getsource(cli_mod)
        tree = ast.parse(source)

        re_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "re":
                        re_imports.append(node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.module == "re":
                    re_imports.append(node.lineno)

        assert len(re_imports) <= 1, (
            f"Found {len(re_imports)} imports of 're' at lines {re_imports}. Expected exactly 1 (top-level only)."
        )
