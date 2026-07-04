"""Tests for spec caching logic."""

from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock

import httpx


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

    # Mock _make_client to fail — should fall back to stale cache
    with patch("openapi_cli4ai.cli._make_client") as mock_make_client:
        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(side_effect=httpx.ConnectError("Network error"))
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_make_client.return_value = mock_ctx
        result = mod.fetch_spec(profile)
        assert result["info"]["title"] == petstore_spec["info"]["title"]


def _fetch_with_mocked_response(mod, profile, response):
    """Run fetch_spec with _make_client mocked to return `response` from a fresh fetch."""
    # raise_for_status() needs a request attached to the response.
    response.request = httpx.Request("GET", profile.get("openapi_url", profile["base_url"]))
    mock_client = MagicMock()
    mock_client.get = MagicMock(return_value=response)
    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_client)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    with patch("openapi_cli4ai.cli._make_client", return_value=mock_ctx):
        return mod.fetch_spec(profile)


def test_fetch_spec_parses_yaml_vnd_oai_media_type(tmp_config):
    """Should parse a YAML spec served as application/vnd.oai.openapi (e.g. Codecov)."""
    mod, _config_dir, _cache_dir = tmp_config
    profile = {
        "base_url": "https://api.codecov.io/api/v2",
        "openapi_url": "https://api.codecov.io/api/v2/schema/",
        "auth": {"type": "none"},
        "verify_ssl": True,
        "_name": "codecov",
    }
    yaml_spec = (
        "openapi: 3.0.3\n"
        "info:\n  title: Codecov API\n  version: 2.0.0\n"
        "paths:\n  /repos/:\n    get:\n      summary: List repos\n"
    )
    response = httpx.Response(
        200,
        content=yaml_spec.encode(),
        headers={"content-type": "application/vnd.oai.openapi; charset=utf-8"},
    )
    spec = _fetch_with_mocked_response(mod, profile, response)
    assert spec["info"]["title"] == "Codecov API"
    assert "/repos/" in spec["paths"]


def test_fetch_spec_falls_back_to_yaml_on_mislabeled_json(tmp_config):
    """Should fall back to YAML when a body advertised as JSON is actually YAML."""
    mod, _config_dir, _cache_dir = tmp_config
    profile = {
        "base_url": "https://example.com",
        "openapi_url": "https://example.com/schema",
        "auth": {"type": "none"},
        "verify_ssl": True,
        "_name": "mislabeled",
    }
    yaml_spec = "openapi: 3.0.3\ninfo:\n  title: Mislabeled\n  version: 1.0.0\npaths: {}\n"
    response = httpx.Response(
        200,
        content=yaml_spec.encode(),
        headers={"content-type": "application/json"},  # server lies
    )
    spec = _fetch_with_mocked_response(mod, profile, response)
    assert spec["info"]["title"] == "Mislabeled"


def test_fetch_spec_parses_vnd_oai_openapi_json_variant(tmp_config):
    """The +json variant should still parse via the JSON path."""
    mod, _config_dir, _cache_dir = tmp_config
    profile = {
        "base_url": "https://api.codecov.io/api/v2",
        "openapi_url": "https://api.codecov.io/api/v2/schema/?format=json",
        "auth": {"type": "none"},
        "verify_ssl": True,
        "_name": "codecovjson",
    }
    json_spec = json.dumps({"openapi": "3.0.3", "info": {"title": "Codecov API", "version": "2.0.0"}, "paths": {}})
    response = httpx.Response(
        200,
        content=json_spec.encode(),
        headers={"content-type": "application/vnd.oai.openapi+json"},
    )
    spec = _fetch_with_mocked_response(mod, profile, response)
    assert spec["info"]["title"] == "Codecov API"
