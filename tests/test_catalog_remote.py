"""Remote catalog index: TTL cache, merge over bundled, and offline fallback (#27)."""

import json
import time

import httpx
import pytest
from typer.testing import CliRunner

from openapi_cli4ai import catalog
from openapi_cli4ai.cli import app

runner = CliRunner()


def _remote_entry(slug="remote-api", **overrides) -> dict:
    record = {
        "name": slug,
        "description": "Remote demo API",
        "maintainer": "octocat",
        "source": "https://remote.example.com/docs",
        "base_url": "https://api.remote.example.com",
        "openapi_url": "https://api.remote.example.com/openapi.json",
        "auth": {"type": "none"},
        "tier": "community",
        "slug": slug,
    }
    record.update(overrides)
    return record


def _index(*records) -> dict:
    return {"version": catalog.CATALOG_INDEX_VERSION, "profiles": list(records)}


@pytest.fixture
def online(monkeypatch, tmp_config):
    """Unset the suite-wide offline switch; tmp_config keeps the cache in tmp."""
    monkeypatch.delenv(catalog.CATALOG_OFFLINE_ENV, raising=False)
    return tmp_config


def test_offline_env_skips_remote(monkeypatch, tmp_config):
    calls = []
    monkeypatch.setattr(catalog, "_download_index", lambda: calls.append(1) or _index(_remote_entry()))
    slugs = {e["_slug"] for e in catalog._load_catalog()}
    assert calls == []
    assert "remote-api" not in slugs and "petstore" in slugs


def test_remote_entries_merge_over_bundled(online, monkeypatch):
    remote_petstore = _remote_entry(
        "petstore",
        tier="verified",
        description="Petstore (from main)",
        source="https://petstore3.swagger.io",
        base_url="https://petstore3.swagger.io/api/v3",
        openapi_url="https://petstore3.swagger.io/api/v3/openapi.json",
    )
    monkeypatch.setattr(catalog, "_download_index", lambda: _index(_remote_entry(), remote_petstore))
    by_slug = {e["_slug"]: e for e in catalog._load_catalog()}
    assert "remote-api" in by_slug, "new profile on main is visible before a release"
    assert by_slug["petstore"]["description"] == "Petstore (from main)", "remote wins on the same slug"
    assert "xquik" in by_slug, "bundled entries not in the index are kept"


def test_remote_index_is_cached_for_ttl(online, monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, "_download_index", lambda: calls.append(1) or _index(_remote_entry()))
    catalog._load_catalog()
    catalog._load_catalog()
    assert len(calls) == 1
    assert catalog._index_cache_path().exists()
    # expire the cache: next load downloads again
    cached = json.loads(catalog._index_cache_path().read_text())
    cached["fetched_at"] = time.time() - catalog.CATALOG_INDEX_TTL - 1
    catalog._index_cache_path().write_text(json.dumps(cached))
    catalog._load_catalog()
    assert len(calls) == 2


def test_fetch_failure_uses_stale_cache_then_bundled(online, monkeypatch):
    def boom():
        raise httpx.ConnectError("offline")

    # no cache yet: bundled only
    monkeypatch.setattr(catalog, "_download_index", boom)
    slugs = {e["_slug"] for e in catalog._load_catalog()}
    assert slugs == {"petstore", "xquik"}

    # a stale cache beats bundled when the fetch fails
    catalog.config.ensure_dirs()
    catalog._index_cache_path().write_text(
        json.dumps({"fetched_at": time.time() - catalog.CATALOG_INDEX_TTL - 1, "index": _index(_remote_entry())})
    )
    slugs = {e["_slug"] for e in catalog._load_catalog()}
    assert "remote-api" in slugs


def test_malformed_index_is_ignored(online, monkeypatch):
    monkeypatch.setattr(catalog, "_download_index", lambda: {"version": 99, "profiles": "nope"})
    slugs = {e["_slug"] for e in catalog._load_catalog()}
    assert slugs == {"petstore", "xquik"}
    assert not catalog._index_cache_path().exists(), "a malformed document is not cached"


def test_invalid_remote_entries_are_skipped_not_fatal(online, monkeypatch):
    bad_tier = _remote_entry("bad-tier", tier="trusted")
    bad_slug = _remote_entry("Bad Slug")
    inline_secret = _remote_entry("leaky", auth={"type": "api-key", "env_var": "X", "token": "sk-live"})
    cross_domain = _remote_entry("squat", source="https://other.example.org/docs")
    monkeypatch.setattr(
        catalog, "_download_index", lambda: _index(bad_tier, bad_slug, inline_secret, cross_domain, _remote_entry())
    )
    slugs = {e["_slug"] for e in catalog._load_catalog()}
    assert "remote-api" in slugs
    assert not {"bad-tier", "Bad Slug", "leaky", "squat"} & slugs


def test_oversized_index_is_rejected(online, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"x" * (catalog._CATALOG_INDEX_MAX_BYTES + 1))

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError):
        catalog._download_index()


def test_catalog_install_from_remote_entry(online, monkeypatch):
    mod, _tmp_path, _cache_dir = online
    monkeypatch.setattr(catalog, "_download_index", lambda: _index(_remote_entry()))
    result = runner.invoke(app, ["catalog", "install", "remote-api", "--yes"])
    assert result.exit_code == 0, result.output
    assert mod.load_profiles()["profiles"]["remote-api"]["base_url"] == "https://api.remote.example.com"


def test_validate_all_uses_bundled_only(online, monkeypatch):
    monkeypatch.setattr(catalog, "_download_index", lambda: _index(_remote_entry("not-a-file")))
    result = runner.invoke(app, ["catalog", "validate", "--all", "--offline"])
    assert result.exit_code == 0, result.output
    assert "not-a-file" not in result.output


def test_index_command_round_trips_and_check(tmp_config, tmp_path):
    target = tmp_path / "index.json"
    result = runner.invoke(app, ["catalog", "index", str(target)])
    assert result.exit_code == 0, result.output
    index = json.loads(target.read_text())
    assert index["version"] == catalog.CATALOG_INDEX_VERSION
    assert {p["slug"] for p in index["profiles"]} == {"petstore", "xquik"}
    entries = catalog._index_to_entries(index)
    assert {e["_slug"] for e in entries} == {"petstore", "xquik"}
    assert runner.invoke(app, ["catalog", "index", str(target), "--check"]).exit_code == 0
    target.write_text("{}")
    assert runner.invoke(app, ["catalog", "index", str(target), "--check"]).exit_code == 1


def test_committed_index_is_current():
    """profiles/index.json in this checkout must match the TOML files (CI enforces the same)."""
    result = runner.invoke(app, ["catalog", "index", "--check"])
    assert result.exit_code == 0, result.output
