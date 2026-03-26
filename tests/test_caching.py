"""Tests for spec caching logic."""

from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock


def test_spec_cache_paths(cli_module):
    """Should generate deterministic cache paths from URL."""
    profile = {"base_url": "https://example.com", "openapi_path": "/openapi.json"}
    url = cli_module._resolve_spec_url(profile)
    cache_file, meta_file = cli_module._spec_cache_paths(url)

    assert cache_file.name.startswith("spec_")
    assert cache_file.name.endswith(".json")
    assert meta_file.name.endswith(".meta")

    # Same URL should produce same cache paths
    cache_file2, meta_file2 = cli_module._spec_cache_paths(url)
    assert cache_file == cache_file2


def test_spec_cache_different_urls(cli_module):
    """Different URLs should produce different cache paths."""
    path1, _ = cli_module._spec_cache_paths("https://example.com/openapi.json")
    path2, _ = cli_module._spec_cache_paths("https://other.com/openapi.json")
    assert path1 != path2


def test_resolve_spec_url_from_path(cli_module):
    """Should build spec URL from base_url + openapi_path."""
    profile = {
        "base_url": "https://api.example.com",
        "openapi_path": "/v2/openapi.json",
    }
    url = cli_module._resolve_spec_url(profile)
    assert url == "https://api.example.com/v2/openapi.json"


def test_resolve_spec_url_from_absolute(cli_module):
    """Should use openapi_url when provided."""
    profile = {
        "base_url": "https://api.example.com",
        "openapi_url": "https://raw.githubusercontent.com/example/spec.json",
    }
    url = cli_module._resolve_spec_url(profile)
    assert url == "https://raw.githubusercontent.com/example/spec.json"


def test_resolve_spec_url_default_path(cli_module):
    """Should default to /openapi.json when no path specified."""
    profile = {"base_url": "https://api.example.com"}
    url = cli_module._resolve_spec_url(profile)
    assert url == "https://api.example.com/openapi.json"


def test_fetch_spec_uses_cache(tmp_config, petstore_spec):
    """Should use cached spec when cache is fresh."""
    mod, config_dir, cache_dir = tmp_config
    profile = {
        "base_url": "https://petstore3.swagger.io/api/v3",
        "openapi_path": "/openapi.json",
        "auth": {"type": "none"},
        "verify_ssl": True,
        "_name": "test",
    }

    # Pre-populate cache
    url = mod._resolve_spec_url(profile)
    cache_file, meta_file = mod._spec_cache_paths(url)
    cache_file.write_text(json.dumps(petstore_spec))
    meta_file.write_text(json.dumps({"fetched_at": time.time(), "url": url}))

    # Should use cache (no network call)
    result = mod.fetch_spec(profile)
    assert result["info"]["title"] == petstore_spec["info"]["title"]


def test_fetch_spec_stale_cache_triggers_fetch(tmp_config, petstore_spec):
    """Should try to fetch when cache is stale."""
    mod, config_dir, cache_dir = tmp_config
    profile = {
        "base_url": "https://petstore3.swagger.io/api/v3",
        "openapi_path": "/openapi.json",
        "auth": {"type": "none"},
        "verify_ssl": True,
        "_name": "test",
    }

    # Pre-populate cache with old timestamp
    url = mod._resolve_spec_url(profile)
    cache_file, meta_file = mod._spec_cache_paths(url)
    cache_file.write_text(json.dumps(petstore_spec))
    meta_file.write_text(json.dumps({"fetched_at": time.time() - 7200, "url": url}))  # 2 hours old

    # Mock httpx to fail — should fall back to stale cache
    with patch("openapi_cli4ai.cli.httpx.Client") as mock_client:
        mock_client.return_value.__enter__ = MagicMock(side_effect=Exception("Network error"))
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        result = mod.fetch_spec(profile)
        assert result["info"]["title"] == petstore_spec["info"]["title"]
