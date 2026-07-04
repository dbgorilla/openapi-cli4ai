"""Tests for the profile catalog: loading, mapping, validation, and commands."""

from __future__ import annotations

import tomli_w
from typer.testing import CliRunner

from openapi_cli4ai.cli import app

runner = CliRunner()


def _good_entry(**overrides) -> dict:
    entry = {
        "name": "demo",
        "description": "Demo REST API",
        "maintainer": "octocat",
        "source": "https://demo.example.com/docs",
        "base_url": "https://api.demo.example.com",
        "openapi_url": "https://api.demo.example.com/openapi.json",
        "auth": {"type": "api-key", "env_var": "DEMO_KEY", "header": "x-api-key"},
        "_slug": "demo",
        "_tier": "community",
    }
    entry.update(overrides)
    return entry


# ── loader / mapping ──────────────────────────────────────────────────────────


def test_load_catalog_includes_petstore(cli_module):
    slugs = [e.get("_slug") for e in cli_module._load_catalog()]
    assert "petstore" in slugs


def test_catalog_to_profile_strips_metadata(cli_module):
    entry = _good_entry()
    profile = cli_module._catalog_to_profile(entry)
    for meta in ("name", "description", "maintainer", "source", "_slug", "_tier"):
        assert meta not in profile
    assert profile["base_url"] == "https://api.demo.example.com"
    assert profile["auth"]["type"] == "api-key"
    assert profile["verify_ssl"] is True


def test_auth_env_vars_and_login(cli_module):
    assert cli_module._auth_env_vars({"type": "api-key", "env_var": "K"}) == ["K"]
    assert cli_module._auth_env_vars({"type": "bearer", "token_env_var": "T"}) == ["T"]
    assert cli_module._auth_env_vars({"type": "none"}) == []
    assert cli_module._auth_uses_login({"type": "oidc"}) is True
    assert cli_module._auth_uses_login({"type": "bearer", "token_endpoint": "/t"}) is True
    assert cli_module._auth_uses_login({"type": "api-key"}) is False


# ── validation ────────────────────────────────────────────────────────────────


def test_validate_good_entry_offline(cli_module):
    errors, _ = cli_module._validate_catalog_entry(_good_entry(), check_spec=False)
    assert errors == []


def test_validate_rejects_ownership_mismatch(cli_module):
    entry = _good_entry(source="https://unrelated-marketing.com/docs")
    errors, _ = cli_module._validate_catalog_entry(entry, check_spec=False)
    assert any("ownership" in e for e in errors)


def test_validate_rejects_inline_secret(cli_module):
    entry = _good_entry(auth={"type": "bearer", "token": "sk_live_123"})
    errors, _ = cli_module._validate_catalog_entry(entry, check_spec=False)
    assert any("inline secret" in e for e in errors)


def test_validate_rejects_missing_field(cli_module):
    entry = _good_entry()
    del entry["maintainer"]
    errors, _ = cli_module._validate_catalog_entry(entry, check_spec=False)
    assert any("maintainer" in e for e in errors)


def test_validate_rejects_bad_auth_type(cli_module):
    entry = _good_entry(auth={"type": "magic"})
    errors, _ = cli_module._validate_catalog_entry(entry, check_spec=False)
    assert any("auth.type" in e for e in errors)


def test_validate_warns_on_promo(cli_module):
    entry = _good_entry(description="The best fastest API")
    errors, warnings = cli_module._validate_catalog_entry(entry, check_spec=False)
    assert errors == []
    assert any("promotional" in w for w in warnings)


# ── commands ──────────────────────────────────────────────────────────────────


def test_catalog_list(tmp_config):
    result = runner.invoke(app, ["catalog", "list"])
    assert result.exit_code == 0
    assert "petstore" in result.output


def test_catalog_search_hit_and_miss(tmp_config):
    hit = runner.invoke(app, ["catalog", "search", "pet"])
    assert hit.exit_code == 0
    assert "petstore" in hit.output

    miss = runner.invoke(app, ["catalog", "search", "nope_xyz"])
    assert miss.exit_code == 0
    assert "No catalog profiles match" in miss.output


def test_catalog_show(tmp_config):
    result = runner.invoke(app, ["catalog", "show", "petstore"])
    assert result.exit_code == 0
    assert "petstore" in result.output
    assert "Base URL" in result.output


def test_catalog_show_unknown(tmp_config):
    result = runner.invoke(app, ["catalog", "show", "does-not-exist"])
    assert result.exit_code == 1


def test_catalog_install_writes_profile_and_activates(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    result = runner.invoke(app, ["catalog", "install", "petstore"])
    assert result.exit_code == 0

    data = mod.load_profiles()
    assert "petstore" in data["profiles"]
    assert data["active_profile"] == "petstore"
    prof = data["profiles"]["petstore"]
    assert prof["base_url"] == "https://petstore3.swagger.io/api/v3"
    # catalog-only metadata must not leak into the runtime profile
    assert "maintainer" not in prof and "description" not in prof


def test_catalog_install_unknown(tmp_config):
    result = runner.invoke(app, ["catalog", "install", "nope"])
    assert result.exit_code == 1


def test_catalog_validate_all_offline_passes(tmp_config):
    result = runner.invoke(app, ["catalog", "validate", "--all", "--offline"])
    assert result.exit_code == 0
    assert "petstore" in result.output


def test_catalog_validate_file_catches_bad_profile(tmp_config):
    mod, tmp_path, _cache_dir = tmp_config
    bad = tmp_path / "bad.toml"
    bad.write_text(
        tomli_w.dumps(
            {
                "name": "bad",
                "description": "Something",
                "maintainer": "octocat",
                "source": "https://unrelated.com/docs",
                "base_url": "https://api.realservice.io",
                "openapi_url": "https://api.realservice.io/openapi.json",
                "auth": {"type": "none"},
            }
        )
    )
    result = runner.invoke(app, ["catalog", "validate", str(bad), "--offline"])
    assert result.exit_code == 1
    assert "ownership" in result.output.lower() or "FAIL" in result.output
