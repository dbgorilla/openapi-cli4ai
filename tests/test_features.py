"""Tests for new features: --verbose, --timeout, --retries, redaction."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from openapi_cli4ai import cli as cli_mod
from openapi_cli4ai.cli import app

runner = CliRunner()


# ── Credential Redaction Tests ───────────────────────────────────────────────


class TestRedactHeaders:
    """Verify that sensitive headers are redacted in verbose output."""

    def test_authorization_bearer_redacted(self):
        headers = {"Authorization": "Bearer secret_token_123", "Accept": "application/json"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Authorization"] == "***REDACTED***"
        assert redacted["Accept"] == "application/json"

    def test_authorization_basic_redacted(self):
        headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Authorization"] == "***REDACTED***"

    def test_api_key_header_redacted(self):
        headers = {"X-API-Key": "sk_live_abc123"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["X-API-Key"] == "***REDACTED***"

    def test_cookie_header_redacted(self):
        headers = {"Cookie": "session=abc123"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Cookie"] == "***REDACTED***"

    def test_non_sensitive_headers_preserved(self):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted == headers

    def test_empty_headers(self):
        assert cli_mod._redact_headers({}) == {}

    def test_token_prefix_redacted(self):
        headers = {"Authorization": "Token abc123"}
        redacted = cli_mod._redact_headers(headers)
        assert redacted["Authorization"] == "***REDACTED***"


# ── Verbose Mode Tests ───────────────────────────────────────────────────────


class TestVerboseMode:
    """Verify that --verbose flag produces debug output."""

    def test_verbose_flag_accepted(self, tmp_config, petstore_spec):
        """--verbose should be accepted as a global option."""
        cli, config_path, cache_dir = tmp_config
        # Just verify the flag is accepted (no error)
        result = runner.invoke(app, ["--verbose", "--version"])
        assert result.exit_code == 0

    def test_verbose_prints_when_enabled(self, capsys):
        """_verbose() should print a message to stderr when verbose mode is on."""
        cli_mod._verbose_mode = True
        try:
            cli_mod._verbose("test verbose output")
        finally:
            cli_mod._verbose_mode = False

        captured = capsys.readouterr()
        assert "test verbose output" in captured.err

    def test_verbose_silent_when_disabled(self, capsys):
        """_verbose() should not print when verbose mode is off."""
        cli_mod._verbose_mode = False
        cli_mod._verbose("test message")
        captured = capsys.readouterr()
        assert "test message" not in captured.err


# ── Timeout Tests ────────────────────────────────────────────────────────────


class TestTimeoutFlag:
    """Verify that --timeout flag configures HTTP timeout."""

    def test_timeout_flag_accepted(self):
        """--timeout should be accepted as a global option."""
        result = runner.invoke(app, ["--timeout", "30", "--version"])
        assert result.exit_code == 0

    def test_make_client_uses_timeout(self):
        """_make_client() should create a client with the configured timeout."""
        old = cli_mod._timeout_seconds
        cli_mod._timeout_seconds = 42.0
        try:
            with cli_mod._make_client(verify=False) as client:
                # Verify timeout was passed to the client (httpx stores it as a Timeout object)
                assert client.timeout.connect == 42.0
        finally:
            cli_mod._timeout_seconds = old


# ── Retry Tests ──────────────────────────────────────────────────────────────


class TestRetryWithBackoff:
    """Verify retry logic for 429/503 responses."""

    def test_retries_flag_accepted(self):
        """--retries should be accepted as a global option."""
        result = runner.invoke(app, ["--retries", "3", "--version"])
        assert result.exit_code == 0

    def test_no_retry_on_success(self):
        """Successful responses should not trigger retry."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.request.return_value = mock_response

        old = cli_mod._max_retries
        cli_mod._max_retries = 3
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 200
            assert mock_client.request.call_count == 1
        finally:
            cli_mod._max_retries = old

    def test_retry_on_429(self):
        """429 responses should trigger retry up to max_retries."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {}
        response_200 = MagicMock()
        response_200.status_code = 200

        mock_client.request.side_effect = [response_429, response_200]

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 2
        cli_mod._verbose_mode = False
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 200
            assert mock_client.request.call_count == 2
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_retry_on_503(self):
        """503 responses should trigger retry."""
        mock_client = MagicMock()
        response_503 = MagicMock()
        response_503.status_code = 503
        response_503.headers = {}
        response_200 = MagicMock()
        response_200.status_code = 200

        mock_client.request.side_effect = [response_503, response_200]

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 2
        cli_mod._verbose_mode = False
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 200
            assert mock_client.request.call_count == 2
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_no_retry_on_400(self):
        """4xx errors (other than 429) should not trigger retry."""
        mock_client = MagicMock()
        response_400 = MagicMock()
        response_400.status_code = 400

        mock_client.request.return_value = response_400

        old = cli_mod._max_retries
        cli_mod._max_retries = 3
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 400
            assert mock_client.request.call_count == 1
        finally:
            cli_mod._max_retries = old

    def test_max_retries_exhausted(self):
        """Should return last response when retries exhausted."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {}

        mock_client.request.return_value = response_429

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 1
        cli_mod._verbose_mode = False
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 429
            assert mock_client.request.call_count == 2  # initial + 1 retry
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_respects_retry_after_header(self):
        """Should use Retry-After header value for wait time."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {"retry-after": "0.01"}
        response_200 = MagicMock()
        response_200.status_code = 200

        mock_client.request.side_effect = [response_429, response_200]

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 2
        cli_mod._verbose_mode = False
        try:
            start = time.monotonic()
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            elapsed = time.monotonic() - start
            assert result.status_code == 200
            # Should have waited at least 0.01s (Retry-After value)
            assert elapsed >= 0.01
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_retry_after_capped_at_300s(self):
        """Retry-After: 99999 should be capped — sleep should not exceed 300s."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {"retry-after": "99999"}
        response_200 = MagicMock()
        response_200.status_code = 200

        mock_client.request.side_effect = [response_429, response_200]

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 2
        cli_mod._verbose_mode = False
        sleep_values = []
        try:
            with patch("time.sleep", side_effect=lambda s: sleep_values.append(s)):
                cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert len(sleep_values) == 1
            assert sleep_values[0] <= 300.0, f"Sleep should be capped at 300s, got {sleep_values[0]}"
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_aggregate_retry_cap(self):
        """Total retry wait should not exceed 600s aggregate cap."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429
        response_429.headers = {"retry-after": "250"}

        mock_client.request.return_value = response_429

        old_retries = cli_mod._max_retries
        old_verbose = cli_mod._verbose_mode
        cli_mod._max_retries = 5
        cli_mod._verbose_mode = False
        sleep_values = []
        try:
            with patch("time.sleep", side_effect=lambda s: sleep_values.append(s)):
                result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            total_sleep = sum(sleep_values)
            assert total_sleep <= 600.0, f"Aggregate sleep {total_sleep:.0f}s exceeds 600s cap"
            assert result.status_code == 429
        finally:
            cli_mod._max_retries = old_retries
            cli_mod._verbose_mode = old_verbose

    def test_zero_retries_means_no_retry(self):
        """With --retries 0 (default), no retry should happen."""
        mock_client = MagicMock()
        response_429 = MagicMock()
        response_429.status_code = 429

        mock_client.request.return_value = response_429

        old = cli_mod._max_retries
        cli_mod._max_retries = 0
        try:
            result = cli_mod._request_with_retry(mock_client, "GET", "http://example.com/api")
            assert result.status_code == 429
            assert mock_client.request.call_count == 1
        finally:
            cli_mod._max_retries = old
