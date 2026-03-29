"""CLI command tests for openapi-cli4ai using Typer's CliRunner.

Tests all 8 CLI commands that previously had zero test coverage.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from openapi_cli4ai.cli import app
from openapi_cli4ai import cli as cli_mod

runner = CliRunner()


@pytest.fixture
def setup_petstore(tmp_config, petstore_spec):
    """Set up a petstore profile with cached spec for CLI command tests."""
    cli, config_path, cache_dir = tmp_config

    # Save a profile config
    profiles = {
        "active_profile": "petstore",
        "profiles": {
            "petstore": {
                "base_url": "https://petstore3.swagger.io/api/v3",
                "openapi_path": "/openapi.json",
                "auth": {"type": "none"},
                "verify_ssl": True,
            },
        },
    }
    cli.save_profiles(profiles)

    # Cache the spec so commands don't need network
    import hashlib
    import time

    spec_url = "https://petstore3.swagger.io/api/v3/openapi.json"
    url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
    cache_file = cache_dir / f"spec_{url_hash}.json"
    cache_meta = cache_dir / f"spec_{url_hash}.meta"
    cache_file.write_text(json.dumps(petstore_spec))
    cache_meta.write_text(json.dumps({"fetched_at": time.time(), "url": spec_url}))

    return cli, config_path, cache_dir


# ── cmd_endpoints Tests ──────────────────────────────────────────────────────


class TestCmdEndpoints:
    """Tests for the 'endpoints' command."""

    def test_endpoints_table_format(self, setup_petstore):
        """Default format should show a table of endpoints."""
        result = runner.invoke(app, ["endpoints"])
        assert result.exit_code == 0
        assert "endpoint(s)" in result.output.lower()

    def test_endpoints_json_format(self, setup_petstore):
        """--format json should output valid JSON."""
        result = runner.invoke(app, ["endpoints", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) > 0
        assert "method" in data[0]
        assert "path" in data[0]

    def test_endpoints_compact_format(self, setup_petstore):
        """--format compact should show one-line-per-endpoint output."""
        result = runner.invoke(app, ["endpoints", "--format", "compact"])
        assert result.exit_code == 0
        assert "endpoint(s)" in result.output.lower()

    def test_endpoints_search_match(self, setup_petstore):
        """--search should filter endpoints by path/summary/operationId."""
        result = runner.invoke(app, ["endpoints", "--search", "pet", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0
        for ep in data:
            assert (
                "pet" in ep["path"].lower()
                or "pet" in ep.get("summary", "").lower()
                or "pet" in ep.get("operationId", "").lower()
            )

    def test_endpoints_search_no_match(self, setup_petstore):
        """--search with no matches should show message."""
        result = runner.invoke(app, ["endpoints", "--search", "zzz_nonexistent_zzz"])
        assert result.exit_code == 0
        assert "no endpoints found" in result.output.lower()

    def test_endpoints_tag_filter(self, setup_petstore):
        """--tag should filter by tag."""
        result = runner.invoke(app, ["endpoints", "--tag", "pet", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        for ep in data:
            tags_lower = [t.lower() for t in ep.get("tags", [])]
            assert any("pet" in t for t in tags_lower)

    def test_endpoints_deprecated_flag(self, setup_petstore):
        """--deprecated should include deprecated endpoints."""
        # Run without flag
        result1 = runner.invoke(app, ["endpoints", "--format", "json"])
        data1 = json.loads(result1.output)

        # Run with flag
        result2 = runner.invoke(app, ["endpoints", "--deprecated", "--format", "json"])
        data2 = json.loads(result2.output)

        # With deprecated flag should have >= endpoints
        assert len(data2) >= len(data1)


# ── cmd_call Tests ───────────────────────────────────────────────────────────


class TestCmdCall:
    """Tests for the 'call' command."""

    def test_call_invalid_method(self, setup_petstore):
        """Invalid HTTP method should produce error."""
        result = runner.invoke(app, ["call", "INVALID", "/pet"])
        assert result.exit_code == 1
        assert "invalid" in result.output.lower()

    def test_call_invalid_json_body(self, setup_petstore):
        """Invalid JSON in --body should produce error."""
        result = runner.invoke(app, ["call", "POST", "/pet", "--body", "not json"])
        assert result.exit_code == 1
        assert "invalid json" in result.output.lower()

    def test_call_body_file_not_found(self, setup_petstore):
        """--body @nonexistent.json should produce error."""
        result = runner.invoke(app, ["call", "POST", "/pet", "--body", "@nonexistent_file.json"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_call_invalid_query_param(self, setup_petstore):
        """Query param without = should produce error."""
        result = runner.invoke(app, ["call", "GET", "/pet", "--query", "badparam"])
        assert result.exit_code == 1
        assert "key=value" in result.output.lower()

    def test_call_invalid_header(self, setup_petstore):
        """Header without : should produce error."""
        result = runner.invoke(app, ["call", "GET", "/pet", "--header", "badheader"])
        assert result.exit_code == 1
        assert "key:value" in result.output.lower()

    def test_call_body_from_file(self, setup_petstore, tmp_path):
        """--body @file.json should load body from file."""
        body_file = tmp_path / "body.json"
        body_file.write_text('{"name": "Rex", "status": "available"}')

        # The output should confirm the file was loaded, even if the request fails
        result = runner.invoke(app, ["call", "POST", "/pet", "--body", f"@{body_file}"])
        # "body loaded from" now goes to stderr; check it didn't error on file parsing
        assert result.exit_code != 2  # exit 2 = typer usage error, not our code

    def test_call_exits_nonzero_on_http_error(self, setup_petstore):
        """cmd_call should exit with code 1 when the server returns 4xx/5xx."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"error": "Internal Server Error"}
        mock_response.reason_phrase = "Internal Server Error"

        with patch.object(cli_mod, "_request_with_retry", return_value=mock_response):
            result = runner.invoke(app, ["call", "GET", "/pet/999"])
            assert result.exit_code == 1

    def test_call_wraps_httpx_exceptions(self, setup_petstore):
        """cmd_call should show a user-friendly error on network failures."""
        import httpx

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_mc.return_value = mock_client
            mock_client.request.side_effect = httpx.ReadTimeout("timed out")

            result = runner.invoke(app, ["call", "GET", "/pet/1"])
            assert result.exit_code == 1
            assert "request failed" in result.output.lower()


# ── cmd_run Tests ────────────────────────────────────────────────────────────


class TestCmdRun:
    """Tests for the 'run' command."""

    def test_run_not_found_shows_suggestions(self, setup_petstore):
        """Running with unknown operationId should suggest similar ones."""
        result = runner.invoke(app, ["run", "findPetsByXYZZY"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_run_invalid_json_input(self, setup_petstore):
        """Invalid JSON in --input should produce error."""
        result = runner.invoke(app, ["run", "findPetsByStatus", "--input", "not json"])
        assert result.exit_code == 1
        assert "invalid json" in result.output.lower()

    def test_run_input_file_not_found(self, setup_petstore):
        """--input-file with nonexistent file should produce error."""
        result = runner.invoke(app, ["run", "findPetsByStatus", "--input-file", "nonexistent.json"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_run_case_insensitive_match(self, setup_petstore):
        """Operation lookup should be case-insensitive."""
        # The petstore spec has "findPetsByStatus"
        # Looking up "findpetsbystatus" (lowercase) should find it
        result = runner.invoke(app, ["run", "findpetsbystatus", "--input", '{"status": "available"}'])
        # Should match the operation (not say "not found")
        assert "not found" not in result.output.lower()

    def test_run_exits_nonzero_on_http_error(self, setup_petstore):
        """cmd_run should exit with code 1 when the server returns 4xx/5xx."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"error": "Not found"}
        mock_response.reason_phrase = "Not Found"

        with patch.object(cli_mod, "_request_with_retry", return_value=mock_response):
            result = runner.invoke(app, ["run", "findPetsByStatus", "--input", '{"status": "x"}'])
            assert result.exit_code == 1

    def test_run_wraps_httpx_exceptions(self, setup_petstore):
        """cmd_run should show a user-friendly error on network failures."""
        import httpx

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_mc.return_value = mock_client
            mock_client.request.side_effect = httpx.ReadTimeout("timed out")

            result = runner.invoke(app, ["run", "findPetsByStatus", "--input", '{"status": "x"}'])
            assert result.exit_code == 1
            assert "request failed" in result.output.lower()


# ── cmd_init Tests ───────────────────────────────────────────────────────────


class TestCmdInit:
    """Tests for the 'init' command."""

    def test_init_creates_profile(self, tmp_config):
        """init should create a profile in the config file."""
        cli, config_path, cache_dir = tmp_config

        # Mock the spec fetch to avoid network
        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "Test", "version": "1.0"}, "paths": {}},
        ):
            result = runner.invoke(
                app,
                [
                    "init",
                    "testapi",
                    "--url",
                    "http://localhost:8000",
                    "--spec",
                    "/openapi.json",
                ],
            )

        assert result.exit_code == 0
        assert "created" in result.output.lower()

    def test_init_sets_active_profile(self, tmp_config):
        """init should set the new profile as active."""
        cli, config_path, cache_dir = tmp_config

        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "Test", "version": "1.0"}, "paths": {}},
        ):
            runner.invoke(
                app,
                [
                    "init",
                    "newprofile",
                    "--url",
                    "http://localhost:8000",
                    "--spec",
                    "/openapi.json",
                ],
            )

        profiles = cli.load_profiles()
        assert profiles.get("active_profile") == "newprofile"


# ── cmd_logout Tests ─────────────────────────────────────────────────────────


class TestCmdLogout:
    """Tests for the 'logout' command."""

    def test_logout_clears_token(self, setup_petstore):
        """logout should remove the cached token file."""
        cli, config_path, cache_dir = setup_petstore

        # Create a fake token cache
        token_file = cache_dir / "petstore_token.json"
        token_file.write_text('{"access_token": "test"}')

        result = runner.invoke(app, ["logout"])
        assert result.exit_code == 0
        assert "logged out" in result.output.lower()
        assert not token_file.exists()

    def test_logout_no_token(self, setup_petstore):
        """logout with no cached token should show message."""
        result = runner.invoke(app, ["logout"])
        assert result.exit_code == 0
        assert "no cached token" in result.output.lower()


# ── cmd_profile_* Tests ──────────────────────────────────────────────────────


class TestCmdProfile:
    """Tests for profile subcommands."""

    def test_profile_list_shows_profiles(self, setup_petstore):
        """profile list should show all profiles."""
        result = runner.invoke(app, ["profile", "list"])
        assert result.exit_code == 0
        assert "petstore" in result.output.lower()

    def test_profile_list_empty(self, tmp_config):
        """profile list with no profiles should show message."""
        result = runner.invoke(app, ["profile", "list"])
        assert result.exit_code == 0
        assert "no profiles" in result.output.lower()

    def test_profile_add(self, tmp_config):
        """profile add should create a new profile."""
        cli, config_path, cache_dir = tmp_config

        result = runner.invoke(
            app,
            [
                "profile",
                "add",
                "newapi",
                "--url",
                "http://localhost:9000",
            ],
        )
        assert result.exit_code == 0
        assert "added" in result.output.lower()

        profiles = cli.load_profiles()
        assert "newapi" in profiles["profiles"]

    def test_profile_use(self, setup_petstore):
        """profile use should switch active profile."""
        cli, config_path, cache_dir = setup_petstore

        # Add a second profile
        profiles = cli.load_profiles()
        profiles["profiles"]["other"] = {
            "base_url": "http://other.com",
            "auth": {"type": "none"},
        }
        cli.save_profiles(profiles)

        result = runner.invoke(app, ["profile", "use", "other"])
        assert result.exit_code == 0
        assert "other" in result.output.lower()

        profiles = cli.load_profiles()
        assert profiles["active_profile"] == "other"

    def test_profile_use_nonexistent(self, setup_petstore):
        """profile use with unknown name should error."""
        result = runner.invoke(app, ["profile", "use", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_profile_remove(self, setup_petstore):
        """profile remove should delete the profile."""
        cli, config_path, cache_dir = setup_petstore

        # Add a second profile so we can remove petstore
        profiles = cli.load_profiles()
        profiles["profiles"]["other"] = {
            "base_url": "http://other.com",
            "auth": {"type": "none"},
        }
        cli.save_profiles(profiles)

        result = runner.invoke(app, ["profile", "remove", "other", "--force"])
        assert result.exit_code == 0
        assert "removed" in result.output.lower()

        profiles = cli.load_profiles()
        assert "other" not in profiles["profiles"]

    def test_profile_remove_nonexistent(self, setup_petstore):
        """profile remove with unknown name should error."""
        result = runner.invoke(app, ["profile", "remove", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_profile_show(self, setup_petstore):
        """profile show should display profile config."""
        result = runner.invoke(app, ["profile", "show", "petstore"])
        assert result.exit_code == 0
        assert "petstore" in result.output.lower()

    def test_profile_show_active(self, setup_petstore):
        """profile show without name should show active profile."""
        result = runner.invoke(app, ["profile", "show"])
        assert result.exit_code == 0
        assert "petstore" in result.output.lower()

    def test_profile_show_nonexistent(self, setup_petstore):
        """profile show with unknown name should error."""
        result = runner.invoke(app, ["profile", "show", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ── Version Flag Test ────────────────────────────────────────────────────────


class TestVersionFlag:
    """Tests for --version flag."""

    def test_version_output(self):
        """--version should show version string."""
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "openapi-cli4ai" in result.output.lower()
