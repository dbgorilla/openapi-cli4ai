"""Comprehensive CLI command tests via typer.testing.CliRunner.

Covers: cmd_endpoints, cmd_call, cmd_run, cmd_login, cmd_logout,
cmd_init, profile subcommands, and the main callback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import tomli_w
from typer.testing import CliRunner

from openapi_cli4ai.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(config_file: Path, data: dict) -> None:
    """Write a TOML config to the given path."""
    config_file.write_text(tomli_w.dumps(data))


def _base_config(base_url: str = "https://api.example.com") -> dict:
    return {
        "active_profile": "test",
        "profiles": {
            "test": {
                "base_url": base_url,
                "openapi_path": "/openapi.json",
                "auth": {"type": "none"},
                "verify_ssl": True,
            }
        },
    }


def _bearer_config() -> dict:
    return {
        "active_profile": "test",
        "profiles": {
            "test": {
                "base_url": "https://api.example.com",
                "openapi_path": "/openapi.json",
                "auth": {
                    "type": "bearer",
                    "token_endpoint": "/auth/token",
                    "payload": {
                        "username": "{username}",
                        "password": "{password}",
                    },
                },
                "verify_ssl": True,
            }
        },
    }


def _mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    text: str = "",
    content_type: str = "application/json",
    reason_phrase: str = "OK",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason_phrase = reason_phrase
    resp.headers = {"content-type": content_type}
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.json.side_effect = ValueError("No JSON")
        resp.text = text
    return resp


# ===========================================================================
# 1. cmd_endpoints
# ===========================================================================


class TestCmdEndpoints:
    """Tests for the 'endpoints' command."""

    def test_endpoints_table_format(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints"])
        assert result.exit_code == 0
        assert "endpoint(s)" in result.output

    def test_endpoints_json_format(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "operationId" in data[0]

    def test_endpoints_compact_format(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints", "--format", "compact"])
        assert result.exit_code == 0
        assert "endpoint(s)" in result.output
        # Compact format should have method and path on same line
        assert "/pet" in result.output

    def test_endpoints_filter_by_tag(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints", "--tag", "store", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        # All results should have the 'store' tag
        for ep in data:
            assert "store" in [t.lower() for t in ep["tags"]]

    def test_endpoints_search(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints", "--search", "findByStatus", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) >= 1
        assert any("findByStatus" in ep["path"] or "findByStatus" in ep.get("operationId", "") for ep in data)

    def test_endpoints_search_no_results(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["endpoints", "--search", "nonexistent_xyz_endpoint"])
        assert result.exit_code == 0
        assert "No endpoints found" in result.output
        assert "Tip" in result.output

    def test_endpoints_exclude_deprecated(self, tmp_config):
        """Deprecated endpoints should be excluded by default."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0"},
            "paths": {
                "/active": {
                    "get": {
                        "operationId": "getActive",
                        "summary": "Active endpoint",
                        "responses": {"200": {"description": "OK"}},
                    }
                },
                "/old": {
                    "get": {
                        "operationId": "getOld",
                        "summary": "Deprecated endpoint",
                        "deprecated": True,
                        "responses": {"200": {"description": "OK"}},
                    }
                },
            },
        }
        with patch.object(mod, "fetch_spec", return_value=spec):
            result = runner.invoke(app, ["endpoints", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        op_ids = [ep["operationId"] for ep in data]
        assert "getActive" in op_ids
        assert "getOld" not in op_ids

    def test_endpoints_include_deprecated(self, tmp_config):
        """--deprecated flag should include deprecated endpoints."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test", "version": "1.0"},
            "paths": {
                "/old": {
                    "get": {
                        "operationId": "getOld",
                        "summary": "Deprecated endpoint",
                        "deprecated": True,
                        "responses": {"200": {"description": "OK"}},
                    }
                },
            },
        }
        with patch.object(mod, "fetch_spec", return_value=spec):
            result = runner.invoke(app, ["endpoints", "--deprecated", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert any(ep["operationId"] == "getOld" for ep in data)


# ===========================================================================
# 2. cmd_call
# ===========================================================================


class TestCmdCall:
    """Tests for the 'call' command."""

    def test_simple_get(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 1, "name": "Rex"})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "GET", "/pet/1"])

        assert result.exit_code == 0
        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[0][0] == "GET"
        assert "api.example.com/pet/1" in call_args[0][1]

    def test_post_with_body_string(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 1})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        body = '{"name": "Rex", "status": "available"}'
        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "POST", "/pet", "--body", body])

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["json"] == {"name": "Rex", "status": "available"}

    def test_post_with_body_file(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        body_file = tmp_path / "payload.json"
        body_file.write_text('{"name": "Fido"}')

        mock_resp = _mock_response(200, json_data={"id": 2})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "POST", "/pet", "--body", f"@{body_file}"])

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["json"] == {"name": "Fido"}

    def test_get_with_query_params(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data=[])

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "call",
                    "GET",
                    "/pet/findByStatus",
                    "--query",
                    "status=available",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["params"] == {"status": "available"}

    def test_call_with_header(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"ok": True})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "call",
                    "GET",
                    "/data",
                    "--header",
                    "X-Custom: my-value",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert "X-Custom" in call_kwargs.kwargs["headers"]
        assert call_kwargs.kwargs["headers"]["X-Custom"] == "my-value"

    def test_invalid_http_method(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["call", "INVALID", "/pet"])
        assert result.exit_code != 0
        assert "Invalid HTTP method" in result.output

    def test_invalid_json_body(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["call", "POST", "/pet", "--body", "{bad json"])
        assert result.exit_code != 0
        assert "Invalid JSON body" in result.output

    def test_invalid_query_param_format(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["call", "GET", "/pet", "--query", "noequals"])
        assert result.exit_code != 0
        assert "Invalid query param" in result.output

    def test_missing_body_file(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["call", "POST", "/pet", "--body", "@nonexistent.json"])
        assert result.exit_code != 0
        assert "Body file not found" in result.output

    def test_invalid_header_format(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["call", "GET", "/pet", "--header", "no-colon-here"])
        assert result.exit_code != 0
        assert "Invalid header" in result.output

    def test_call_raw_output(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 1})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "GET", "/pet/1", "--raw"])

        assert result.exit_code == 0

    def test_call_json_output(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 1})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "GET", "/pet/1", "--json"])

        assert result.exit_code == 0


# ===========================================================================
# 3. cmd_run
# ===========================================================================


class TestCmdRun:
    """Tests for the 'run' command."""

    def test_run_valid_operation(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data=[{"id": 1, "name": "Rex"}])

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "findPetsByStatus",
                    "--input",
                    '{"status": "available"}',
                ],
            )

        assert result.exit_code == 0
        mock_client.request.assert_called_once()

    def test_run_with_input_json(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 1, "name": "doggie"})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "getPetById",
                    "--input",
                    '{"petId": 1}',
                ],
            )

        assert result.exit_code == 0
        call_args = mock_client.request.call_args
        # petId should be in the URL path
        assert "/pet/1" in call_args[0][1]

    def test_run_with_input_file(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        input_file = tmp_path / "input.json"
        input_file.write_text('{"status": "available"}')

        mock_resp = _mock_response(200, json_data=[])
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "findPetsByStatus",
                    "--input-file",
                    str(input_file),
                ],
            )

        assert result.exit_code == 0

    def test_run_operation_not_found_with_suggestions(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["run", "findPets"])

        assert result.exit_code != 0
        assert "not found" in result.output
        # Should suggest similar operations
        assert "Did you mean" in result.output

    def test_run_case_insensitive_lookup(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data=[])

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            # Use wrong case — should still match via case-insensitive lookup
            result = runner.invoke(
                app,
                [
                    "run",
                    "FINDPETSBYSTATUS",
                    "--input",
                    '{"status": "available"}',
                ],
            )

        assert result.exit_code == 0

    def test_run_missing_path_parameters(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
        ):
            # getPetById requires petId but we provide nothing
            result = runner.invoke(app, ["run", "getPetById"])

        assert result.exit_code != 0
        assert "Missing required path parameter" in result.output

    def test_run_operation_completely_unknown(self, tmp_config, petstore_spec):
        """Operation with no partial match should show error without suggestions."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["run", "totallyRandomXyzOperation"])

        assert result.exit_code != 0
        assert "not found" in result.output

    def test_run_input_file_not_found(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(
                app,
                [
                    "run",
                    "findPetsByStatus",
                    "--input-file",
                    "/nonexistent/file.json",
                ],
            )

        assert result.exit_code != 0
        assert "Input file not found" in result.output

    def test_run_invalid_json_input(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(
                app,
                [
                    "run",
                    "findPetsByStatus",
                    "--input",
                    "{bad json",
                ],
            )

        assert result.exit_code != 0
        assert "Invalid JSON input" in result.output

    def test_run_with_request_body(self, tmp_config, petstore_spec):
        """Test running an operation that takes a request body (addPet)."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"id": 10, "name": "Rex"})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "addPet",
                    "--input",
                    '{"name": "Rex", "photoUrls": [], "status": "available"}',
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        # Body should be passed
        assert call_kwargs.kwargs.get("json") is not None


# ===========================================================================
# 4. cmd_login
# ===========================================================================


class TestCmdLogin:
    """Tests for the 'login' command."""

    def test_login_bearer_with_username_password(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _bearer_config())

        token_response = {
            "access_token": "test-token-abc123",
            "expires_in": 3600,
        }
        mock_resp = _mock_response(200, json_data=token_response)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        # Also mock the post-login spec fetch
        with (
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
            patch.object(mod, "_try_post_login_spec_fetch"),
        ):
            result = runner.invoke(
                app,
                [
                    "login",
                    "--username",
                    "testuser",
                    "--password",
                    "testpass",
                ],
            )

        assert result.exit_code == 0
        assert "Logged in successfully" in result.output

        # Verify token was cached
        token_cache = cache_dir / "test_token.json"
        assert token_cache.exists()
        cached = json.loads(token_cache.read_text())
        assert cached["access_token"] == "test-token-abc123"

    def test_login_unsupported_auth_type(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        config = _base_config()
        config["profiles"]["test"]["auth"] = {"type": "none"}
        _write_config(mod.CONFIG_FILE, config)

        result = runner.invoke(app, ["login"])
        assert result.exit_code != 0
        assert "bearer auth + token_endpoint" in result.output or "Login is for" in result.output

    def test_login_with_access_token_injection(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(
            app,
            [
                "login",
                "--access-token",
                "my-injected-token-value",
            ],
        )

        assert result.exit_code == 0
        assert "Token injected successfully" in result.output

        # Verify token was cached
        token_cache = cache_dir / "test_token.json"
        assert token_cache.exists()
        cached = json.loads(token_cache.read_text())
        assert cached["access_token"] == "my-injected-token-value"

    def test_login_with_access_token_and_refresh_token(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(
            app,
            [
                "login",
                "--access-token",
                "access-abc",
                "--refresh-token",
                "refresh-xyz",
            ],
        )

        assert result.exit_code == 0
        token_cache = cache_dir / "test_token.json"
        cached = json.loads(token_cache.read_text())
        assert cached["access_token"] == "access-abc"
        assert cached["refresh_token"] == "refresh-xyz"

    def test_login_with_access_token_stdin(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(
            app,
            [
                "login",
                "--access-token-stdin",
            ],
            input="stdin-token-value\n",
        )

        assert result.exit_code == 0
        assert "Token injected successfully" in result.output
        token_cache = cache_dir / "test_token.json"
        cached = json.loads(token_cache.read_text())
        assert cached["access_token"] == "stdin-token-value"

    def test_login_bearer_failed_auth(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _bearer_config())

        error_resp = _mock_response(
            401,
            json_data={"error": "Invalid credentials"},
            reason_phrase="Unauthorized",
        )

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = error_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "login",
                    "--username",
                    "bad",
                    "--password",
                    "bad",
                ],
            )

        assert result.exit_code != 0

    def test_login_bearer_password_file(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _bearer_config())

        pw_file = tmp_path / "password.txt"
        pw_file.write_text("file-password\n")

        token_response = {"access_token": "tok", "expires_in": 3600}
        mock_resp = _mock_response(200, json_data=token_response)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        with (
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
            patch.object(mod, "_try_post_login_spec_fetch"),
        ):
            result = runner.invoke(
                app,
                [
                    "login",
                    "--username",
                    "user",
                    "--password-file",
                    str(pw_file),
                ],
            )

        assert result.exit_code == 0
        # Verify the password from file was used in the payload
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["password"] == "file-password"


# ===========================================================================
# 5. cmd_logout
# ===========================================================================


class TestCmdLogout:
    """Tests for the 'logout' command."""

    def test_logout_with_existing_token(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        # Create a cached token
        token_cache = cache_dir / "test_token.json"
        token_cache.write_text(json.dumps({"access_token": "tok"}))

        result = runner.invoke(app, ["logout"])
        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not token_cache.exists()

    def test_logout_with_no_token(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["logout"])
        assert result.exit_code == 0
        assert "No cached token" in result.output


# ===========================================================================
# 6. cmd_init (partial — non-interactive paths)
# ===========================================================================


class TestCmdInit:
    """Tests for the 'init' command (non-interactive paths only)."""

    def test_init_with_url_auto_detect_spec(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        # Start with empty config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        # Mock httpx.Client for auto-detection
        mock_detect_resp = MagicMock()
        mock_detect_resp.status_code = 200
        mock_detect_resp.headers = {"content-type": "application/json"}
        mock_detect_resp.json.return_value = petstore_spec

        mock_detect_client = MagicMock()
        mock_detect_client.__enter__ = MagicMock(return_value=mock_detect_client)
        mock_detect_client.__exit__ = MagicMock(return_value=False)
        mock_detect_client.get.return_value = mock_detect_resp

        with (
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_detect_client),
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
        ):
            result = runner.invoke(
                app,
                [
                    "init",
                    "petstore",
                    "--url",
                    "https://petstore3.swagger.io/api/v3",
                    "--auth",
                    "none",
                ],
            )

        assert result.exit_code == 0
        assert "Profile 'petstore' created" in result.output

        # Verify config was written
        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert "petstore" in saved["profiles"]
        assert saved["active_profile"] == "petstore"

    def test_init_with_spec_url(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(
                app,
                [
                    "init",
                    "myapi",
                    "--url",
                    "https://api.example.com",
                    "--spec-url",
                    "https://api.example.com/docs/openapi.json",
                    "--auth",
                    "none",
                ],
            )

        assert result.exit_code == 0
        assert "Profile 'myapi' created" in result.output

        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert saved["profiles"]["myapi"]["openapi_url"] == "https://api.example.com/docs/openapi.json"

    def test_init_with_spec_path(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(
                app,
                [
                    "init",
                    "myapi",
                    "--url",
                    "https://api.example.com",
                    "--spec",
                    "/v2/api-docs",
                    "--auth",
                    "none",
                ],
            )

        assert result.exit_code == 0
        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert saved["profiles"]["myapi"]["openapi_path"] == "/v2/api-docs"

    def test_init_spec_fetch_failure_still_saves(self, tmp_config):
        """If spec fetch fails, profile should still be saved with a warning."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        with patch.object(mod, "fetch_spec", side_effect=Exception("Connection refused")):
            result = runner.invoke(
                app,
                [
                    "init",
                    "broken",
                    "--url",
                    "https://api.broken.com",
                    "--spec",
                    "/openapi.json",
                    "--auth",
                    "none",
                ],
            )

        assert result.exit_code == 0
        assert "Warning" in result.output or "Profile 'broken' created" in result.output

        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert "broken" in saved["profiles"]


# ===========================================================================
# 7. Profile commands
# ===========================================================================


class TestProfileCommands:
    """Tests for profile subcommands (add, list, use, remove, show)."""

    def test_profile_add(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        result = runner.invoke(
            app,
            [
                "profile",
                "add",
                "newprof",
                "--url",
                "https://api.new.com",
                "--spec",
                "/api/openapi.json",
                "--auth",
                "none",
            ],
        )

        assert result.exit_code == 0
        assert "added" in result.output

        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert "newprof" in saved["profiles"]
        assert saved["profiles"]["newprof"]["base_url"] == "https://api.new.com"
        # First profile should become active
        assert saved["active_profile"] == "newprof"

    def test_profile_list_with_profiles(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        config = {
            "active_profile": "one",
            "profiles": {
                "one": {
                    "base_url": "https://api.one.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
                "two": {
                    "base_url": "https://api.two.com",
                    "auth": {"type": "bearer"},
                    "verify_ssl": True,
                },
            },
        }
        _write_config(mod.CONFIG_FILE, config)

        result = runner.invoke(app, ["profile", "list"])
        assert result.exit_code == 0
        assert "one" in result.output
        assert "two" in result.output
        assert "active profile" in result.output.lower() or "*" in result.output

    def test_profile_list_empty(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        result = runner.invoke(app, ["profile", "list"])
        assert result.exit_code == 0
        assert "No profiles configured" in result.output

    def test_profile_use(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        config = {
            "active_profile": "one",
            "profiles": {
                "one": {
                    "base_url": "https://api.one.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
                "two": {
                    "base_url": "https://api.two.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
            },
        }
        _write_config(mod.CONFIG_FILE, config)

        result = runner.invoke(app, ["profile", "use", "two"])
        assert result.exit_code == 0
        assert "two" in result.output

        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert saved["active_profile"] == "two"

    def test_profile_use_not_found(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["profile", "use", "nonexistent"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_profile_remove(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        config = {
            "active_profile": "victim",
            "profiles": {
                "victim": {
                    "base_url": "https://api.victim.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
                "survivor": {
                    "base_url": "https://api.survivor.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
            },
        }
        _write_config(mod.CONFIG_FILE, config)

        result = runner.invoke(app, ["profile", "remove", "victim", "--force"])
        assert result.exit_code == 0
        assert "removed" in result.output

        import tomllib

        saved = tomllib.loads(mod.CONFIG_FILE.read_text())
        assert "victim" not in saved["profiles"]
        # Active profile should switch to remaining one
        assert saved["active_profile"] == "survivor"

    def test_profile_remove_not_found(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["profile", "remove", "ghost", "--force"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_profile_show(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["profile", "show"])
        assert result.exit_code == 0
        assert "test" in result.output
        assert "api.example.com" in result.output

    def test_profile_show_named(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        config = {
            "active_profile": "one",
            "profiles": {
                "one": {
                    "base_url": "https://one.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
                "two": {
                    "base_url": "https://two.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
            },
        }
        _write_config(mod.CONFIG_FILE, config)

        result = runner.invoke(app, ["profile", "show", "two"])
        assert result.exit_code == 0
        assert "two" in result.output
        assert "two.com" in result.output

    def test_profile_show_not_found(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        result = runner.invoke(app, ["profile", "show", "missing"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_profile_remove_cleans_cache(self, tmp_config):
        """Removing a profile should clean up its cached spec and token files."""
        mod, tmp_path, cache_dir = tmp_config
        config = {
            "active_profile": "test",
            "profiles": {
                "test": {
                    "base_url": "https://api.example.com",
                    "auth": {"type": "none"},
                    "verify_ssl": True,
                },
            },
        }
        _write_config(mod.CONFIG_FILE, config)

        # Create cache files for this profile
        (cache_dir / "test_token.json").write_text("{}")
        (cache_dir / "test_spec.json").write_text("{}")

        result = runner.invoke(app, ["profile", "remove", "test", "--force"])
        assert result.exit_code == 0

        # Cache files should be cleaned up
        assert not (cache_dir / "test_token.json").exists()
        assert not (cache_dir / "test_spec.json").exists()


# ===========================================================================
# 8. main callback
# ===========================================================================


class TestMainCallback:
    """Tests for the main app callback (--version, --insecure, no subcommand)."""

    def test_version_flag(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config

        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "openapi-cli4ai" in result.output
        assert mod.VERSION in result.output

    def test_no_subcommand_shows_help(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config

        result = runner.invoke(app, [])
        # no_args_is_help=True causes exit code 0 or 2 depending on typer version
        assert result.exit_code in (0, 2)
        # Should show usage/help info
        assert "openapi-cli4ai" in result.output.lower() or "Usage" in result.output or "endpoints" in result.output

    def test_insecure_flag(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        # Reset insecure mode before test
        mod.set_insecure_mode(False)
        assert mod.get_verify_ssl() is True

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(app, ["--insecure", "endpoints", "--format", "json"])

        assert result.exit_code == 0
        # After invoking with --insecure, the flag should have been set
        # (though it resets per invocation, we verify the command ran OK)

    def test_help_flag(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config

        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "openapi-cli4ai" in result.output.lower() or "REST API" in result.output


# ===========================================================================
# Edge cases / extra coverage
# ===========================================================================


class TestEdgeCases:
    """Additional edge-case tests for better coverage."""

    def test_call_path_without_leading_slash(self, tmp_config):
        """Path without leading slash should still work."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"ok": True})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(app, ["call", "GET", "pet/1"])

        assert result.exit_code == 0
        call_args = mock_client.request.call_args
        assert "/pet/1" in call_args[0][1]

    def test_no_profiles_configured(self, tmp_config):
        """Commands should fail gracefully when no profiles exist."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, {"profiles": {}})

        result = runner.invoke(app, ["endpoints"])
        assert result.exit_code != 0
        assert "No profiles configured" in result.output

    def test_run_no_input(self, tmp_config, petstore_spec):
        """Running an operation with no required params and no input should work."""
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={"available": 10})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch.object(mod, "fetch_spec", return_value=petstore_spec),
            patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client),
        ):
            # getInventory has no required params
            result = runner.invoke(app, ["run", "getInventory"])

        assert result.exit_code == 0

    def test_endpoints_with_refresh_flag(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        with patch.object(mod, "fetch_spec", return_value=petstore_spec) as mock_fetch:
            result = runner.invoke(app, ["endpoints", "--refresh", "--format", "json"])

        assert result.exit_code == 0
        mock_fetch.assert_called_once()
        # Verify refresh=True was passed
        assert mock_fetch.call_args.kwargs.get("refresh") is True

    def test_call_body_file_invalid_json(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not valid json {{{")

        result = runner.invoke(app, ["call", "POST", "/pet", "--body", f"@{bad_file}"])
        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_run_input_file_invalid_json(self, tmp_config, petstore_spec):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        bad_file = tmp_path / "bad_input.json"
        bad_file.write_text("{not valid")

        with patch.object(mod, "fetch_spec", return_value=petstore_spec):
            result = runner.invoke(
                app,
                [
                    "run",
                    "findPetsByStatus",
                    "--input-file",
                    str(bad_file),
                ],
            )

        assert result.exit_code != 0
        assert "Invalid JSON" in result.output

    def test_login_with_jwt_access_token(self, tmp_config):
        """Injecting a JWT token should extract the exp claim."""
        import base64

        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())

        # Build a fake JWT with exp claim
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        exp_time = int(time.time()) + 7200
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "user", "exp": exp_time}).encode()).rstrip(b"=").decode()
        sig = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
        jwt_token = f"{header}.{payload}.{sig}"

        result = runner.invoke(
            app,
            [
                "login",
                "--access-token",
                jwt_token,
            ],
        )

        assert result.exit_code == 0
        token_cache = cache_dir / "test_token.json"
        cached = json.loads(token_cache.read_text())
        assert cached["expires_at"] == exp_time

    def test_call_multiple_query_params(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data=[])

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "call",
                    "GET",
                    "/search",
                    "--query",
                    "q=test",
                    "--query",
                    "limit=10",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["params"] == {"q": "test", "limit": "10"}

    def test_call_multiple_headers(self, tmp_config):
        mod, tmp_path, cache_dir = tmp_config
        _write_config(mod.CONFIG_FILE, _base_config())
        mock_resp = _mock_response(200, json_data={})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with patch("openapi_cli4ai.cli.httpx.Client", return_value=mock_client):
            result = runner.invoke(
                app,
                [
                    "call",
                    "GET",
                    "/data",
                    "--header",
                    "X-First: one",
                    "--header",
                    "X-Second: two",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = mock_client.request.call_args
        assert call_kwargs.kwargs["headers"]["X-First"] == "one"
        assert call_kwargs.kwargs["headers"]["X-Second"] == "two"
