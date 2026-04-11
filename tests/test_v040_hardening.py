"""Tests for hardening applied to v0.4.0's new functions.

Verifies that v0.4.0's _inject_token, _token_exchange, _device_login,
_device_discover_endpoints, and _try_post_login_spec_fetch use the
hardened patterns: _safe_profile_name, specific exception catches,
err_console for error output, and _make_client instead of httpx.Client.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import click.exceptions
import httpx
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_source(cli_module, func_name: str) -> str:
    """Get the source code of a function from the CLI module."""
    func = getattr(cli_module, func_name)
    return inspect.getsource(func)


# ── VAL-AUTH-005: _token_exchange catches httpx.HTTPError ────────────────────


class TestTokenExchangeHardening:
    """Verify _token_exchange uses HTTPError, _make_client, err_console."""

    def test_token_exchange_catches_http_error(self, cli_module):
        """_token_exchange catches httpx.HTTPError, not just ConnectError."""
        source = _get_source(cli_module, "_token_exchange")
        assert "httpx.HTTPError" in source, "_token_exchange should catch httpx.HTTPError"
        assert "httpx.ConnectError" not in source, "_token_exchange should NOT catch the narrow httpx.ConnectError"

    def test_token_exchange_uses_make_client(self, cli_module):
        """_token_exchange should use _make_client, not bare httpx.Client."""
        source = _get_source(cli_module, "_token_exchange")
        assert "_make_client(" in source, "_token_exchange should use _make_client factory"
        # Must not have a direct httpx.Client( call
        # Exclude comments and string literals for robustness
        lines = [line for line in source.splitlines() if not line.strip().startswith("#") and "httpx.Client(" in line]
        assert len(lines) == 0, "_token_exchange should not directly construct httpx.Client"

    def test_token_exchange_uses_err_console(self, cli_module):
        """Error output from _token_exchange goes to err_console."""
        source = _get_source(cli_module, "_token_exchange")
        # All [red] prints in this function should use err_console
        red_prints = [line.strip() for line in source.splitlines() if "[red]" in line and ".print(" in line]
        for line in red_prints:
            assert line.startswith("err_console.print("), f"Error print should use err_console: {line}"

    def test_token_exchange_propagates_http_error(self, cli_module):
        """_token_exchange exits on httpx.HTTPError (not just ConnectError)."""
        auth_config = {
            "token_exchange_endpoint": "/api/exchange",
        }
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.post = MagicMock(side_effect=httpx.ReadTimeout("Read timed out"))
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with patch.object(cli_module, "_make_client", return_value=mock_ctx):
            with pytest.raises(click.exceptions.Exit):
                cli_module._token_exchange(
                    {"access_token": "tok"},
                    auth_config,
                    "https://api.example.com",
                )


# ── VAL-AUTH-006: _device_login catches httpx.HTTPError ──────────────────────


class TestDeviceLoginHardening:
    """Verify _device_login uses HTTPError, _make_client, err_console."""

    def test_device_login_catches_http_error(self, cli_module):
        """_device_login catches httpx.HTTPError, not just ConnectError."""
        source = _get_source(cli_module, "_device_login")
        assert "httpx.HTTPError" in source, "_device_login should catch httpx.HTTPError"
        assert "httpx.ConnectError" not in source, "_device_login should NOT catch the narrow httpx.ConnectError"

    def test_device_login_uses_make_client(self, cli_module):
        """_device_login should use _make_client, not bare httpx.Client."""
        source = _get_source(cli_module, "_device_login")
        assert "_make_client(" in source, "_device_login should use _make_client factory"
        lines = [line for line in source.splitlines() if not line.strip().startswith("#") and "httpx.Client(" in line]
        assert len(lines) == 0, "_device_login should not directly construct httpx.Client"

    def test_device_login_uses_err_console(self, cli_module):
        """Error/status output from _device_login goes to err_console."""
        source = _get_source(cli_module, "_device_login")
        # All [red] prints must use err_console
        red_prints = [line.strip() for line in source.splitlines() if "[red]" in line and ".print(" in line]
        for line in red_prints:
            assert line.startswith("err_console.print("), f"Error print should use err_console: {line}"
        # [dim] status prints must use err_console
        dim_prints = [line.strip() for line in source.splitlines() if "[dim]" in line and ".print(" in line]
        for line in dim_prints:
            assert line.startswith("err_console.print("), f"Status print should use err_console: {line}"

    def test_device_login_http_error_exits(self, cli_module):
        """_device_login exits cleanly on httpx.HTTPError during device code request."""
        auth_config = {
            "client_id": "test-client",
            "scopes": "openid",
        }
        profile = {"base_url": "https://api.example.com"}

        # Mock _device_discover_endpoints to return valid endpoints
        endpoints = {
            "device_authorization_endpoint": "https://auth.example.com/device",
            "token_endpoint": "https://auth.example.com/token",
            "client_id": "test-client",
        }

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
        mock_ctx.post = MagicMock(side_effect=httpx.ReadTimeout("Read timed out"))
        mock_ctx.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(cli_module, "_device_discover_endpoints", return_value=endpoints),
            patch.object(cli_module, "_make_client", return_value=mock_ctx),
        ):
            with pytest.raises(click.exceptions.Exit):
                cli_module._device_login(auth_config, "test-profile", profile)


# ── VAL-AUTH-007: _inject_token uses _safe_profile_name ──────────────────────


class TestInjectTokenHardening:
    """Verify _inject_token uses _safe_profile_name and _save_token."""

    def test_inject_token_safe_profile_name(self, cli_module):
        """_inject_token uses _save_token (which calls _safe_profile_name)."""
        source = _get_source(cli_module, "_inject_token")
        assert "_save_token(" in source, "_inject_token should use _save_token for cache writing"
        # Verify _save_token itself uses _safe_profile_name
        save_source = _get_source(cli_module, "_save_token")
        assert "_safe_profile_name(" in save_source, "_save_token should use _safe_profile_name for path safety"

    def test_inject_token_uses_err_console(self, cli_module):
        """Error output from _inject_token goes to err_console."""
        source = _get_source(cli_module, "_inject_token")
        red_prints = [line.strip() for line in source.splitlines() if "[red]" in line and ".print(" in line]
        for line in red_prints:
            assert line.startswith("err_console.print("), f"Error print should use err_console: {line}"

    def test_inject_token_traversal_safe(self, cli_module, tmp_path, monkeypatch):
        """_inject_token with a traversal profile name doesn't write outside cache dir."""
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        monkeypatch.setattr(cli_module, "CACHE_DIR", cache_dir)

        cli_module._inject_token(
            profile_name="../../etc/evil",
            access_token="eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.fakesig",
            refresh_token="",
            from_stdin=False,
        )

        # Token file must be inside cache_dir, not escaped via traversal
        token_files = list(cache_dir.glob("*_token.json"))
        assert len(token_files) == 1, "Token file should be inside cache dir"
        assert ".." not in str(token_files[0]), "Path should not contain traversal"


# ── VAL-AUTH-008: _try_post_login_spec_fetch specific exceptions ─────────────


class TestTryPostLoginSpecFetchHardening:
    """Verify _try_post_login_spec_fetch uses specific exception types."""

    def test_try_post_login_specific_exceptions(self, cli_module):
        """_try_post_login_spec_fetch should NOT have 'except (typer.Exit, Exception):'."""
        source = _get_source(cli_module, "_try_post_login_spec_fetch")
        # Must not have the overly-broad pattern
        assert "except (typer.Exit, Exception)" not in source, (
            "_try_post_login_spec_fetch should use specific exception types, not bare 'except (typer.Exit, Exception):'"
        )
        # Should have specific types
        assert "httpx.HTTPError" in source or "typer.Exit" in source, (
            "_try_post_login_spec_fetch should catch specific exception types"
        )

    def test_try_post_login_no_bare_except(self, cli_module):
        """No bare 'except Exception:' in _try_post_login_spec_fetch."""
        source = _get_source(cli_module, "_try_post_login_spec_fetch")
        # Check for "except Exception:" (bare, not in a tuple)
        lines = source.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped == "except Exception:" or stripped == "except Exception as e:":
                pytest.fail(f"_try_post_login_spec_fetch has bare 'except Exception': {stripped}")


# ── VAL-AUTH-009: v0.4.0 error messages route to err_console ────────────────


class TestErrConsoleRouting:
    """Verify v0.4.0 functions route errors to err_console, not console."""

    def test_device_discover_endpoints_uses_err_console(self, cli_module):
        """Error output from _device_discover_endpoints goes to err_console."""
        source = _get_source(cli_module, "_device_discover_endpoints")
        red_prints = [line.strip() for line in source.splitlines() if "[red]" in line and ".print(" in line]
        for line in red_prints:
            assert line.startswith("err_console.print("), f"Error print should use err_console: {line}"

    def test_new_functions_use_err_console(self, cli_module):
        """All v0.4.0 functions use err_console for error messages."""
        functions_to_check = [
            "_token_exchange",
            "_device_discover_endpoints",
            "_device_login",
            "_inject_token",
        ]
        for func_name in functions_to_check:
            source = _get_source(cli_module, func_name)
            lines = source.splitlines()
            for line in lines:
                stripped = line.strip()
                # Skip lines that aren't print calls with [red]
                if "[red]" not in stripped or ".print(" not in stripped:
                    continue
                # Must use err_console, not plain console
                assert stripped.startswith("err_console.print("), (
                    f"{func_name}: error print should use err_console: {stripped}"
                )


# ── VAL-AUTH-010: v0.4.0 functions use _make_client ─────────────────────────


class TestMakeClientUsage:
    """Verify v0.4.0 functions use _make_client, not hardcoded httpx.Client."""

    def test_new_functions_use_make_client(self, cli_module):
        """All v0.4.0 functions that need HTTP clients use _make_client."""
        functions_with_http = [
            "_token_exchange",
            "_device_discover_endpoints",
            "_device_login",
            "_auto_detect_flow",
        ]
        for func_name in functions_with_http:
            source = _get_source(cli_module, func_name)
            # Must use _make_client
            assert "_make_client(" in source, f"{func_name} should use _make_client factory"
            # Must not have direct httpx.Client(
            code_lines = [
                line for line in source.splitlines() if not line.strip().startswith("#") and "httpx.Client(" in line
            ]
            assert len(code_lines) == 0, f"{func_name} should not directly construct httpx.Client: {code_lines}"

    def test_no_hardcoded_httpx_client_in_v040_functions(self, cli_module):
        """Double-check: grep-equivalent source scan for httpx.Client( in new functions."""
        target_functions = [
            "_token_exchange",
            "_device_discover_endpoints",
            "_device_login",
            "_inject_token",
            "_auto_detect_flow",
            "_try_post_login_spec_fetch",
        ]
        violations = []
        for func_name in target_functions:
            source = _get_source(cli_module, func_name)
            for i, line in enumerate(source.splitlines(), 1):
                if "httpx.Client(" in line and not line.strip().startswith("#"):
                    violations.append(f"{func_name}:{i}: {line.strip()}")
        assert not violations, "Found hardcoded httpx.Client in v0.4.0 functions:\n" + "\n".join(violations)
