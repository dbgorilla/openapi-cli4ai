"""Tests for foundation infrastructure (VAL-FOUND-001, VAL-FOUND-002).

Verifies that all new globals, imports, and helper functions exist in cli.py.
"""

import importlib

import pytest


class TestFoundationGlobals:
    """VAL-FOUND-001: Helper infrastructure exists."""

    def test_err_console_exists(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert hasattr(cli, "err_console"), "err_console global not defined"

    def test_verbose_mode_exists(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert hasattr(cli, "_verbose_mode"), "_verbose_mode global not defined"

    def test_timeout_seconds_exists(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert hasattr(cli, "_timeout_seconds"), "_timeout_seconds global not defined"

    def test_max_retries_exists(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert hasattr(cli, "_max_retries"), "_max_retries global not defined"

    def test_verbose_mode_default(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert cli._verbose_mode is False

    def test_timeout_seconds_default(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert cli._timeout_seconds == 60.0

    def test_max_retries_default(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert cli._max_retries == 0


class TestFoundationHelperFunctions:
    """VAL-FOUND-002: All 11 helper functions defined."""

    EXPECTED_FUNCTIONS = [
        "_redact_headers",
        "_verbose",
        "_make_client",
        "_request_with_retry",
        "_resolve_file_path",
        "_atomic_write",
        "_safe_profile_name",
        "_save_token",
        "_require_env_var",
        "_merge_allof",
        "_safe_json_or_text",
    ]

    @pytest.mark.parametrize("func_name", EXPECTED_FUNCTIONS)
    def test_helper_function_exists(self, func_name):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        assert hasattr(cli, func_name), f"{func_name} not defined in cli module"
        assert callable(getattr(cli, func_name)), f"{func_name} is not callable"

    def test_redact_headers_works(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        result = cli._redact_headers({"Authorization": "Bearer secret123", "Content-Type": "application/json"})
        assert result["Authorization"] == "***REDACTED***"
        assert result["Content-Type"] == "application/json"

    def test_safe_profile_name_prevents_traversal(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        result = cli._safe_profile_name("../../tmp/pwn")
        assert "/" not in result
        assert ".." not in result

    def test_merge_allof_combines_properties(self):
        from openapi_cli4ai import cli

        importlib.reload(cli)
        schemas = [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
        ]
        result = cli._merge_allof(schemas)
        assert "a" in result["properties"]
        assert "b" in result["properties"]
        assert "b" in result["required"]

    def test_safe_json_or_text_with_json(self):
        """_safe_json_or_text parses JSON responses correctly."""
        import httpx

        from openapi_cli4ai import cli

        importlib.reload(cli)
        response = httpx.Response(
            200,
            content=b'{"key": "value"}',
            headers={"content-type": "application/json"},
        )
        result = cli._safe_json_or_text(response)
        assert isinstance(result, dict)
        assert result["key"] == "value"

    def test_safe_json_or_text_with_text(self):
        """_safe_json_or_text falls back to text for non-JSON."""
        import httpx

        from openapi_cli4ai import cli

        importlib.reload(cli)
        response = httpx.Response(
            200,
            content=b"plain text",
            headers={"content-type": "text/plain"},
        )
        result = cli._safe_json_or_text(response)
        assert isinstance(result, str)
        assert result == "plain text"
