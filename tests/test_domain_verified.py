"""DNS TXT domain verification for catalog profiles (#26)."""

import pytest
from typer.testing import CliRunner

from openapi_cli4ai import catalog, validator
from openapi_cli4ai.cli import app

runner = CliRunner()


def _entry(**overrides) -> dict:
    entry = {
        "name": "acme",
        "description": "Acme REST API",
        "maintainer": "OctoCat",
        "source": "https://developer.acme.co.uk/docs",
        "base_url": "https://api.acme.co.uk",
        "openapi_url": "https://api.acme.co.uk/openapi.json",
        "auth": {"type": "none"},
        "_slug": "acme",
        "_tier": "community",
        "domain_verified": True,
    }
    entry.update(overrides)
    return entry


def _dns(monkeypatch, records: dict[str, list[str]]):
    seen = []

    def fake(name):
        seen.append(name)
        return records.get(name, [])

    monkeypatch.setattr(validator, "_resolve_txt", fake)
    return seen


def test_claim_holds_when_txt_names_maintainer(monkeypatch):
    seen = _dns(monkeypatch, {"_openapi-cli4ai.acme.co.uk": ["v=spf1 -all", "github=octocat"]})
    errors, _ = validator._validate_catalog_entry(_entry(), check_spec=True)
    assert not [e for e in errors if "domain_verified" in e], errors
    # looked up at the registrable domain of base_url (PSL-aware), not the full host
    assert seen == ["_openapi-cli4ai.acme.co.uk"]


def test_claim_fails_without_record(monkeypatch):
    _dns(monkeypatch, {})
    errors, _ = validator._validate_catalog_entry(_entry(), check_spec=True)
    assert any("no TXT record at _openapi-cli4ai.acme.co.uk" in e for e in errors), errors


def test_claim_fails_when_maintainer_differs(monkeypatch):
    _dns(monkeypatch, {"_openapi-cli4ai.acme.co.uk": ["github=someone-else"]})
    errors, _ = validator._validate_catalog_entry(_entry(), check_spec=True)
    assert any("none equals 'github=octocat'" in e for e in errors), errors


def test_resolver_failure_is_an_error(monkeypatch):
    def boom(name):
        raise ValueError(f"DNS lookup for {name} failed: Timeout")

    monkeypatch.setattr(validator, "_resolve_txt", boom)
    errors, _ = validator._validate_catalog_entry(_entry(), check_spec=True)
    assert any("Timeout" in e for e in errors), errors


def test_no_claim_means_no_lookup(monkeypatch):
    seen = _dns(monkeypatch, {})
    entry = _entry()
    del entry["domain_verified"]
    errors, _ = validator._validate_catalog_entry(entry, check_spec=True)
    assert not [e for e in errors if "domain_verified" in e]
    assert seen == []


def test_offline_skips_lookup(monkeypatch):
    seen = _dns(monkeypatch, {})
    errors, _ = validator._validate_catalog_entry(_entry(), check_spec=False)
    assert not [e for e in errors if "domain_verified" in e]
    assert seen == []


def test_claim_must_be_boolean():
    errors, _ = validator._validate_catalog_entry(_entry(domain_verified="yes"), check_spec=False)
    assert any("must be true or false" in e for e in errors)


@pytest.mark.parametrize("in_ci, bucket", [(True, "errors"), (False, "warnings")])
def test_missing_dnspython_errors_in_ci_warns_locally(monkeypatch, in_ci, bucket):
    def unavailable(name):
        raise validator.DnsUnavailable("dnspython is not installed")

    monkeypatch.setattr(validator, "_resolve_txt", unavailable)
    if in_ci:
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    else:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    errors, warnings = validator._validate_catalog_entry(_entry(), check_spec=True)
    target = errors if bucket == "errors" else warnings
    assert any("cannot verify without dnspython" in m for m in target)


def test_domain_verified_does_not_leak_into_runtime_profile():
    assert "domain_verified" not in catalog._catalog_to_profile(_entry())


def test_badge_in_list_and_show(monkeypatch, tmp_config):
    entries = [_entry(), _entry(name="plain", _slug="plain", domain_verified=False)]
    monkeypatch.setattr(catalog, "_load_catalog", lambda remote=True: entries)
    monkeypatch.setattr("openapi_cli4ai.cli._load_catalog", lambda remote=True: entries)
    monkeypatch.setattr("openapi_cli4ai.cli._catalog_find", lambda name: next(e for e in entries if e["_slug"] == name))
    listed = runner.invoke(app, ["catalog", "list"])
    assert listed.exit_code == 0, listed.output
    rows = [line for line in listed.output.splitlines() if "acme" in line or "plain" in line]
    assert any("✓ dns" in r for r in rows if "acme" in r)
    assert not any("✓ dns" in r for r in rows if "plain" in r)
    shown = runner.invoke(app, ["catalog", "show", "acme"])
    assert "✓ verified" in shown.output
    shown = runner.invoke(app, ["catalog", "show", "plain"])
    assert "not verified" in shown.output
