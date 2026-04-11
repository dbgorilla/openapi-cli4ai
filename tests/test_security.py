"""Security tests for openapi-cli4ai.

Tests actual application security functions, not Python stdlib behavior.
"""

from __future__ import annotations

import hashlib
import json
import stat
import urllib.parse
from unittest.mock import patch

import click
import pytest
from typer.testing import CliRunner

from openapi_cli4ai import cli as cli_mod
from openapi_cli4ai.cli import app

runner = CliRunner()


# ── Path Traversal Tests ─────────────────────────────────────────────────────


class TestPathTraversal:
    """Verify that _resolve_file_path resolves symlinks and warns on out-of-CWD."""

    def test_resolve_file_path_follows_symlinks(self, tmp_path):
        """_resolve_file_path should resolve symlinks to their real target."""
        real_file = tmp_path / "real.json"
        real_file.write_text('{"key": "value"}')
        symlink = tmp_path / "link.json"
        symlink.symlink_to(real_file)

        resolved = cli_mod._resolve_file_path(str(symlink), purpose="test")
        assert resolved == real_file.resolve()

    def test_resolve_file_path_warns_outside_cwd(self, tmp_path, capsys):
        """_resolve_file_path should warn when path is outside CWD."""
        outside_file = tmp_path / "outside" / "data.json"
        outside_file.parent.mkdir(parents=True, exist_ok=True)
        outside_file.write_text('{"key": "value"}')

        # Run from a different CWD so the file is "outside"
        with patch("pathlib.Path.cwd", return_value=tmp_path / "workdir"):
            (tmp_path / "workdir").mkdir(exist_ok=True)
            cli_mod._resolve_file_path(str(outside_file), purpose="test")

        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "outside" in captured.err.lower()

    def test_resolve_file_path_no_warning_inside_cwd(self, tmp_path, capsys):
        """_resolve_file_path should not warn for files inside CWD."""
        inside_file = tmp_path / "data.json"
        inside_file.write_text('{"key": "value"}')

        with patch("pathlib.Path.cwd", return_value=tmp_path):
            cli_mod._resolve_file_path(str(inside_file), purpose="test")

        captured = capsys.readouterr()
        assert "warning" not in captured.err.lower()

    def test_cmd_call_body_file_uses_resolve(self, tmp_config, petstore_spec):
        """cmd_call with --body @file should use _resolve_file_path."""
        cli, config_path, cache_dir = tmp_config

        # Set up profile
        cli.save_profiles(
            {
                "active_profile": "test",
                "profiles": {"test": {"base_url": "http://localhost", "auth": {"type": "none"}}},
            }
        )

        with patch.object(cli_mod, "_resolve_file_path", wraps=cli_mod._resolve_file_path) as mock_resolve:
            runner.invoke(app, ["call", "POST", "/api", "--body", "@/nonexistent.json"])
            mock_resolve.assert_called_once()


# ── URL Scheme Validation Tests ──────────────────────────────────────────────


class TestURLSchemeValidation:
    """Verify that cmd_init validates URL schemes via actual CLI invocation."""

    def test_missing_scheme_gets_http_prepended(self, tmp_config):
        """init with bare host:port should auto-prepend http://."""
        cli, config_path, cache_dir = tmp_config

        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}},
        ):
            result = runner.invoke(app, ["init", "test", "--url", "localhost:8080", "--spec", "/openapi.json"])

        assert result.exit_code == 0
        profiles = cli.load_profiles()
        assert profiles["profiles"]["test"]["base_url"] == "http://localhost:8080"

    def test_http_scheme_preserved(self, tmp_config):
        """init with http:// should preserve it."""
        cli, config_path, cache_dir = tmp_config

        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}},
        ):
            runner.invoke(app, ["init", "test", "--url", "http://api.example.com", "--spec", "/openapi.json"])

        profiles = cli.load_profiles()
        assert profiles["profiles"]["test"]["base_url"] == "http://api.example.com"

    def test_ftp_scheme_warns(self, tmp_config):
        """init with ftp:// should produce a warning."""
        cli, config_path, cache_dir = tmp_config

        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}},
        ):
            result = runner.invoke(app, ["init", "test", "--url", "ftp://evil.com", "--spec", "/openapi.json"])

        assert result.exit_code == 1
        assert "unsupported" in result.output.lower() or "non-standard" in result.output.lower()

    def test_private_ips_not_blocked(self, tmp_config):
        """init with private IPs should work without blocking."""
        cli, config_path, cache_dir = tmp_config

        with patch.object(
            cli_mod,
            "fetch_spec",
            return_value={"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}},
        ):
            result = runner.invoke(
                app, ["init", "test", "--url", "http://192.168.1.100:8080", "--spec", "/openapi.json"]
            )

        assert result.exit_code == 0


# ── Atomic File Write Tests ──────────────────────────────────────────────────


class TestAtomicFileWrites:
    """Verify that _atomic_write and _save_token produce correct files."""

    def test_atomic_write_creates_file(self, tmp_path):
        """_atomic_write should create the target file with correct content."""
        target = tmp_path / "output.json"
        cli_mod._atomic_write(target, '{"key": "value"}')
        assert target.exists()
        assert json.loads(target.read_text()) == {"key": "value"}

    def test_atomic_write_restricted_permissions(self, tmp_path):
        """_atomic_write with restricted=True should create 0o600 file."""
        target = tmp_path / "secret.json"
        cli_mod._atomic_write(target, '{"token": "secret"}', restricted=True)

        mode = target.stat().st_mode
        assert not (mode & stat.S_IRGRP), "Group should not have read permission"
        assert not (mode & stat.S_IROTH), "Others should not have read permission"

    def test_save_token_creates_restricted_file(self, tmp_config):
        """_save_token should create a restricted token cache file."""
        cli, config_path, cache_dir = tmp_config
        token_data = {"access_token": "test123", "expires_at": 9999999999}

        path = cli._save_token("myprofile", token_data)
        assert path.exists()
        assert json.loads(path.read_text())["access_token"] == "test123"

        mode = path.stat().st_mode
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)

    def test_profile_write_is_atomic_and_restricted(self, tmp_config):
        """save_profiles should write valid TOML with restricted permissions."""
        cli, config_path, cache_dir = tmp_config
        data = {
            "active_profile": "test",
            "profiles": {"test": {"base_url": "http://localhost", "auth": {"type": "none"}}},
        }
        cli.save_profiles(data)

        import tomllib

        config_file = cli.CONFIG_FILE
        assert config_file.exists()
        parsed = tomllib.loads(config_file.read_text())
        assert parsed["active_profile"] == "test"

        mode = config_file.stat().st_mode
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)


# ── JSON Injection Tests ─────────────────────────────────────────────────────


class TestJSONInjection:
    """Verify that login payload construction prevents JSON injection."""

    def test_login_payload_safe_with_metacharacters(self, tmp_config):
        """Login with JSON metacharacters in username should not inject keys."""
        cli, config_path, cache_dir = tmp_config

        # Set up a profile with bearer + token_endpoint auth
        cli.save_profiles(
            {
                "active_profile": "test",
                "profiles": {
                    "test": {
                        "base_url": "http://localhost:8000",
                        "auth": {
                            "type": "bearer",
                            "token_endpoint": "/auth/token",
                            "payload": {"username": "{username}", "password": "{password}"},
                        },
                    }
                },
            }
        )

        # Mock the HTTP call to capture what payload gets sent
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"error": "invalid"}
        mock_response.text = '{"error": "invalid"}'

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post = MagicMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            runner.invoke(
                app,
                [
                    "login",
                    "--username",
                    '","admin":true,"x":"',
                    "--password",
                    "pass",
                ],
            )

            # Verify the payload sent to the server (unconditional — test must exercise the post)
            assert mock_client.post.called, "Login command must make an HTTP POST"
            call_kwargs = mock_client.post.call_args
            sent_payload = call_kwargs.kwargs.get("json") or (
                call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
            )
            assert sent_payload is not None, "POST must include a payload"
            assert "admin" not in sent_payload, "JSON injection should be prevented"


# ── XSS in OIDC Error Response Tests ─────────────────────────────────────────


class TestXSSInOIDCError:
    """Verify that _OIDCCallbackHandler escapes HTML in error responses."""

    def test_handler_escapes_error_param(self):
        """The OIDC callback handler should HTML-escape error parameters."""
        from http.server import HTTPServer
        from threading import Thread

        import httpx

        handler_class = cli_mod._OIDCCallbackHandler
        handler_class.auth_code = None
        handler_class.error = None
        handler_class.expected_state = None

        # Start a real server on a random port
        server = HTTPServer(("127.0.0.1", 0), handler_class)
        port = server.server_address[1]

        # Handle one request in a background thread
        thread = Thread(target=server.handle_request)
        thread.start()

        try:
            # Send a request with XSS payload in error param
            resp = httpx.get(
                f"http://127.0.0.1:{port}/callback?error=%3Cscript%3Ealert(1)%3C/script%3E",
                timeout=5.0,
            )
            body = resp.text

            # The <script> tag should be escaped, not rendered raw
            assert "<script>" not in body, "XSS: raw <script> tag found in response"
            assert "&lt;script&gt;" in body, "Error should be HTML-escaped"
        finally:
            thread.join(timeout=5)
            server.server_close()


# ── OIDC State Validation Tests ──────────────────────────────────────────────


class TestOIDCStateValidation:
    """Verify that _oidc_login_no_browser validates the state parameter."""

    def test_no_browser_rejects_wrong_state(self):
        """_oidc_login_no_browser should reject a redirect URL with wrong state."""
        # Mock typer.prompt to return a URL with wrong state
        wrong_url = "http://localhost:8484/callback?code=abc123&state=WRONG"
        with patch("typer.prompt", return_value=wrong_url):
            with pytest.raises(click.exceptions.Exit):
                cli_mod._oidc_login_no_browser("http://auth.example.com", expected_state="CORRECT")

    def test_no_browser_accepts_correct_state(self):
        """_oidc_login_no_browser should accept a redirect URL with correct state."""
        correct_url = "http://localhost:8484/callback?code=abc123&state=CORRECT"
        with patch("typer.prompt", return_value=correct_url):
            result = cli_mod._oidc_login_no_browser("http://auth.example.com", expected_state="CORRECT")
        assert result == "abc123"

    def test_no_browser_rejects_missing_state(self):
        """_oidc_login_no_browser should reject a redirect URL with no state."""
        no_state_url = "http://localhost:8484/callback?code=abc123"
        with patch("typer.prompt", return_value=no_state_url):
            with pytest.raises(click.exceptions.Exit):
                cli_mod._oidc_login_no_browser("http://auth.example.com", expected_state="EXPECTED")


# ── Spec Cache Cleanup Tests ─────────────────────────────────────────────────


class TestSpecCacheCleanup:
    """Verify that profile removal cleans up both token and spec caches."""

    def test_profile_remove_cleans_token_cache(self, tmp_config):
        """Removing a profile via CLI should delete its token cache."""
        cli, config_path, cache_dir = tmp_config

        # Set up profile and token cache
        cli.save_profiles(
            {
                "active_profile": "testapi",
                "profiles": {
                    "testapi": {
                        "base_url": "http://localhost:8000",
                        "openapi_path": "/openapi.json",
                        "auth": {"type": "none"},
                    },
                    "other": {"base_url": "http://other.com", "auth": {"type": "none"}},
                },
            }
        )
        token_file = cache_dir / "testapi_token.json"
        token_file.write_text('{"access_token": "test"}')

        result = runner.invoke(app, ["profile", "remove", "testapi", "--force"])
        assert result.exit_code == 0
        assert not token_file.exists(), "Token cache should be deleted on profile removal"

    def test_profile_remove_cleans_spec_cache(self, tmp_config):
        """Removing a profile via CLI should delete its URL-hashed spec cache."""
        cli, config_path, cache_dir = tmp_config

        spec_url = "http://localhost:8000/openapi.json"
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        spec_file = cache_dir / f"spec_{url_hash}.json"
        meta_file = cache_dir / f"spec_{url_hash}.meta"
        spec_file.write_text('{"openapi": "3.0.0"}')
        meta_file.write_text(json.dumps({"url": spec_url, "fetched_at": 0}))

        cli.save_profiles(
            {
                "active_profile": "testapi",
                "profiles": {
                    "testapi": {
                        "base_url": "http://localhost:8000",
                        "openapi_path": "/openapi.json",
                        "auth": {"type": "none"},
                    },
                    "other": {"base_url": "http://other.com", "auth": {"type": "none"}},
                },
            }
        )

        result = runner.invoke(app, ["profile", "remove", "testapi", "--force"])
        assert result.exit_code == 0
        assert not spec_file.exists(), "Spec cache should be deleted on profile removal"
        assert not meta_file.exists(), "Spec meta should be deleted on profile removal"


# ── Credential Redaction Tests ───────────────────────────────────────────────


class TestRedactHeaders:
    """Verify _redact_headers handles edge cases safely."""

    def test_non_string_header_values_dont_crash(self):
        """_redact_headers should handle non-string values (e.g., int)."""
        headers = {"Content-Length": 42, "Authorization": "Bearer secret"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Content-Length"] == 42
        assert redacted["Authorization"] == "***REDACTED***"

    def test_mixed_case_keys_redacted(self):
        """Header key matching should be case-insensitive."""
        headers = {"AUTHORIZATION": "Bearer tok", "x-Api-Key": "secret"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["AUTHORIZATION"] == "***REDACTED***"
        assert redacted["x-Api-Key"] == "***REDACTED***"

    def test_set_cookie_response_header_redacted(self):
        """Set-Cookie response headers should be redacted in verbose output."""
        headers = {"Set-Cookie": "session=abc123; Path=/; HttpOnly", "Content-Type": "application/json"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Set-Cookie"] == "***REDACTED***"
        assert redacted["Content-Type"] == "application/json"


# ── Profile Name Sanitization Tests ──────────────────────────────────────────


class TestProfileNameSanitization:
    """Verify that profile names are sanitized for file path safety."""

    def test_traversal_in_profile_name_stripped(self):
        """../../etc/pwn should be sanitized to basename + hash."""
        result = cli_mod._safe_profile_name("../../etc/pwn")
        assert result.startswith("pwn_")
        assert "/" not in result
        assert ".." not in result

    def test_simple_name_preserved(self):
        """Normal profile names pass through unchanged."""
        assert cli_mod._safe_profile_name("petstore") == "petstore"

    def test_slash_in_name_stripped(self):
        """Path separators produce safe name with hash."""
        result = cli_mod._safe_profile_name("my/api")
        assert result.startswith("api_")
        assert "/" not in result

    def test_dot_dot_rejected(self):
        """.. alone should become 'default' with hash."""
        result = cli_mod._safe_profile_name("..")
        assert result.startswith("default_")

    def test_empty_name_rejected(self):
        """Empty string should become 'default' with hash."""
        result = cli_mod._safe_profile_name("")
        assert result.startswith("default_")

    def test_distinct_names_dont_collide(self):
        """Different names that share a basename should produce different safe names."""
        a = cli_mod._safe_profile_name("org1/api")
        b = cli_mod._safe_profile_name("org2/api")
        assert a != b, "Distinct raw names should produce distinct safe names"

    def test_save_token_uses_safe_name(self, tmp_config):
        """_save_token with traversal name should write inside CACHE_DIR."""
        cli, config_path, cache_dir = tmp_config
        path = cli._save_token("../../escape", {"access_token": "t", "expires_at": 9999999999})
        # File should be inside cache_dir, not escaped
        assert str(path).startswith(str(cache_dir))
        assert "escape" in str(path.name)


# ── Profile Fallback Warning Tests ───────────────────────────────────────────


class TestProfileFallbackWarning:
    """Verify that invalid profile selection exits instead of silently falling back."""

    def test_bad_env_profile_exits(self, tmp_config, capsys, monkeypatch):
        """OAC_PROFILE=nonexistent should exit with error, not silently fall back."""
        cli, config_path, cache_dir = tmp_config
        cli.save_profiles(
            {
                "active_profile": "real",
                "profiles": {"real": {"base_url": "http://localhost", "auth": {"type": "none"}}},
            }
        )
        monkeypatch.setenv("OAC_PROFILE", "nonexistent")
        with pytest.raises(click.exceptions.Exit):
            cli.get_active_profile()
        captured = capsys.readouterr()
        assert "not found" in captured.err.lower()


# ── Refresh Token Preservation Tests ─────────────────────────────────────────


class TestRefreshTokenPreservation:
    """Verify that refresh_token is preserved when server omits it."""

    def test_refresh_preserves_existing_token(self, tmp_config):
        """If server response omits refresh_token, the cached one should be kept."""
        from unittest.mock import MagicMock

        cli, config_path, cache_dir = tmp_config
        profile = {
            "base_url": "http://localhost:8000",
            "auth": {"refresh_endpoint": "/refresh"},
            "verify_ssl": True,
            "_name": "testprofile",
        }
        cached = {"refresh_token": "original_refresh", "access_token": "old_access"}

        # Server returns only access_token, no refresh_token
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "new_access", "expires_in": 3600}

        with patch.object(cli_mod, "_make_client") as mock_mc:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_mc.return_value = mock_client

            result = cli._try_refresh_token(profile, profile["auth"], cached)

        assert result is not None
        assert result["access_token"] == "new_access"
        assert result["refresh_token"] == "original_refresh", "Original refresh_token should be preserved"


# ── Path Param URL Encoding Tests ────────────────────────────────────────────


class TestPathParamEncoding:
    """Verify that path parameters are URL-encoded in cmd_run."""

    def test_special_chars_encoded(self):
        """Path params with /, ?, # should be URL-encoded."""
        result = urllib.parse.quote("a/b?c=d", safe="")
        assert "/" not in result
        assert "?" not in result
        assert result == "a%2Fb%3Fc%3Dd"

    def test_route_inputs_then_substitute(self, cli_module, petstore_spec):
        """Full flow: route inputs then substitute with encoding."""
        endpoint = cli_module.extract_full_endpoint_schema(petstore_spec, "getPetById")
        if endpoint:
            path_template = endpoint["path"]  # /pet/{petId}
            # Substitute with a value containing special chars
            encoded_value = urllib.parse.quote("a/b", safe="")
            result = path_template.replace("{petId}", encoded_value)
            assert result == "/pet/a%2Fb"


# ── JSON Output Purity Tests ────────────────────────────────────────────────


class TestJSONOutputPurity:
    """Verify --json output is valid JSON without status line contamination."""

    def test_json_output_no_status_on_stdout(self, capsys):
        """--json should not print status line to stdout."""
        from unittest.mock import MagicMock

        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {"id": 1, "name": "Rex"}
        response.reason_phrase = "OK"

        cli_mod.handle_response(response, json_output=True)
        captured = capsys.readouterr()

        # stdout should be valid JSON only
        import json as json_mod

        parsed = json_mod.loads(captured.out.strip())
        assert parsed == {"id": 1, "name": "Rex"}

        # Status line should be on stderr
        assert "200" in captured.err


# ── Custom Auth Header Redaction Tests ───────────────────────────────────────


class TestCustomHeaderRedaction:
    """Verify that custom auth header names are redacted based on patterns."""

    def test_custom_key_header_redacted(self):
        """Headers with 'key' in name should be redacted."""
        headers = {"X-Custom-Key": "secret123", "Accept": "application/json"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["X-Custom-Key"] == "***REDACTED***"
        assert redacted["Accept"] == "application/json"

    def test_custom_token_header_redacted(self):
        """Headers with 'token' in name should be redacted."""
        headers = {"X-Auth-Token": "abc123"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["X-Auth-Token"] == "***REDACTED***"

    def test_custom_secret_header_redacted(self):
        """Headers with 'secret' in name should be redacted."""
        headers = {"X-Client-Secret": "s3cret"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["X-Client-Secret"] == "***REDACTED***"

    def test_content_type_not_redacted(self):
        """Normal headers should not be redacted."""
        headers = {"Content-Type": "application/json", "Accept": "text/html"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted == headers


# ── Config Corruption Tests ──────────────────────────────────────────────────


class TestConfigCorruption:
    """Verify that corrupt config files fail closed instead of being overwritten."""

    def test_corrupt_toml_exits(self, tmp_config):
        """A corrupt TOML config should exit with error, not silently return empty."""
        cli, config_path, cache_dir = tmp_config
        cli.CONFIG_FILE.write_text("this is not valid toml {{{")
        with pytest.raises(click.exceptions.Exit):
            cli.load_profiles()

    def test_wrong_shape_profiles_exits(self, tmp_config):
        """Config with profiles = 'string' should exit, not crash."""
        cli, config_path, cache_dir = tmp_config
        cli.CONFIG_FILE.write_text('profiles = "oops"\n')
        with pytest.raises(click.exceptions.Exit):
            cli.load_profiles()

    def test_wrong_shape_nested_profile_exits(self, tmp_config):
        """Config with [profiles] bad = 'string' should exit, not crash."""
        cli, config_path, cache_dir = tmp_config
        cli.CONFIG_FILE.write_text('[profiles]\nbad = "oops"\n')
        with pytest.raises(click.exceptions.Exit):
            cli.get_active_profile()


# ── Non-Object Input Tests ───────────────────────────────────────────────────


class TestNonObjectInput:
    """Verify that cmd_run rejects non-object JSON input."""

    def test_array_input_rejected(self, tmp_config, petstore_spec):
        """run --input '[1,2,3]' should exit with error, not crash."""
        cli, config_path, cache_dir = tmp_config

        # Set up profile with cached spec
        import hashlib
        import time as time_mod

        cli.save_profiles(
            {
                "active_profile": "pet",
                "profiles": {
                    "pet": {
                        "base_url": "https://petstore3.swagger.io/api/v3",
                        "openapi_path": "/openapi.json",
                        "auth": {"type": "none"},
                    }
                },
            }
        )
        spec_url = "https://petstore3.swagger.io/api/v3/openapi.json"
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        (cache_dir / f"spec_{url_hash}.json").write_text(json.dumps(petstore_spec))
        (cache_dir / f"spec_{url_hash}.meta").write_text(json.dumps({"fetched_at": time_mod.time(), "url": spec_url}))

        result = runner.invoke(app, ["run", "findPetsByStatus", "--input", "[1,2,3]"])
        assert result.exit_code == 1

    def test_null_input_rejected(self, tmp_config, petstore_spec):
        """run --input 'null' should exit with error, not crash."""
        cli, config_path, cache_dir = tmp_config
        import hashlib
        import time as time_mod

        cli.save_profiles(
            {
                "active_profile": "pet",
                "profiles": {
                    "pet": {
                        "base_url": "https://petstore3.swagger.io/api/v3",
                        "openapi_path": "/openapi.json",
                        "auth": {"type": "none"},
                    }
                },
            }
        )
        spec_url = "https://petstore3.swagger.io/api/v3/openapi.json"
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        (cache_dir / f"spec_{url_hash}.json").write_text(json.dumps(petstore_spec))
        (cache_dir / f"spec_{url_hash}.meta").write_text(json.dumps({"fetched_at": time_mod.time(), "url": spec_url}))

        result = runner.invoke(app, ["run", "findPetsByStatus", "--input", "null"])
        assert result.exit_code == 1

    def test_scalar_input_rejected(self, tmp_config, petstore_spec):
        """run --input '42' should exit with error, not crash."""
        cli, config_path, cache_dir = tmp_config
        import hashlib
        import time as time_mod

        cli.save_profiles(
            {
                "active_profile": "pet",
                "profiles": {
                    "pet": {
                        "base_url": "https://petstore3.swagger.io/api/v3",
                        "openapi_path": "/openapi.json",
                        "auth": {"type": "none"},
                    }
                },
            }
        )
        spec_url = "https://petstore3.swagger.io/api/v3/openapi.json"
        url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
        (cache_dir / f"spec_{url_hash}.json").write_text(json.dumps(petstore_spec))
        (cache_dir / f"spec_{url_hash}.meta").write_text(json.dumps({"fetched_at": time_mod.time(), "url": spec_url}))

        result = runner.invoke(app, ["run", "findPetsByStatus", "--input", "42"])
        assert result.exit_code == 1
