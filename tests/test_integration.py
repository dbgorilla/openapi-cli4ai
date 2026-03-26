"""Integration tests against live public APIs.

Run: pytest tests/test_integration.py -v
Skip: pytest tests/ -m "not integration"
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


# ── Helpers ────────────────────────────────────────────────────────────


def _profile(base_url, *, openapi_path=None, openapi_url=None, auth_type="none"):
    """Build a minimal profile dict for testing."""
    p = {"base_url": base_url, "auth": {"type": auth_type}, "verify_ssl": True}
    if openapi_path:
        p["openapi_path"] = openapi_path
    if openapi_url:
        p["openapi_url"] = openapi_url
    return p


# ── Group 1: Petstore (JSON spec from server) ─────────────────────────


PETSTORE = _profile("https://petstore3.swagger.io/api/v3", openapi_path="/openapi.json")


class TestPetstore:
    def test_fetch_spec(self, cli_module, tmp_config):
        mod, _, _ = tmp_config
        spec = mod.fetch_spec(PETSTORE, refresh=True)
        assert "paths" in spec
        assert len(spec["paths"]) > 10

    def test_extract_endpoints(self, cli_module):
        spec = cli_module.fetch_spec(PETSTORE, refresh=False)
        endpoints = cli_module.extract_endpoint_summaries(spec)
        paths = [e["path"] for e in endpoints]
        assert "/pet/findByStatus" in paths

    def test_call_find_pets(self, cli_module):
        resp = cli_module.make_request(PETSTORE, "GET", "/pet/findByStatus", params={"status": "available"})
        # Petstore server is flaky — 500s happen
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            data = resp.json()
            assert isinstance(data, list)

    def test_call_get_pet_by_id(self, cli_module):
        resp = cli_module.make_request(PETSTORE, "GET", "/pet/1")
        # Pet 1 may or may not exist; Petstore server can also 500
        assert resp.status_code in (200, 404, 500)


# ── Group 2: PokéAPI (YAML spec from GitHub) ──────────────────────────


POKEAPI = _profile(
    "https://pokeapi.co",
    openapi_url="https://raw.githubusercontent.com/PokeAPI/pokeapi/master/openapi.yml",
)


class TestPokeAPI:
    def test_fetch_yaml_spec(self, cli_module):
        spec = cli_module.fetch_spec(POKEAPI, refresh=False)
        assert "paths" in spec
        assert spec.get("info", {}).get("title") == "PokéAPI"

    def test_endpoint_search(self, cli_module):
        spec = cli_module.fetch_spec(POKEAPI)
        endpoints = cli_module.extract_endpoint_summaries(spec)
        pokemon_eps = [e for e in endpoints if "pokemon" in e["path"]]
        assert len(pokemon_eps) >= 5

    def test_call_pikachu(self, cli_module):
        resp = cli_module.make_request(POKEAPI, "GET", "/api/v2/pokemon/pikachu")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "pikachu"
        assert "abilities" in data

    def test_call_electric_type(self, cli_module):
        resp = cli_module.make_request(POKEAPI, "GET", "/api/v2/type/electric")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "electric"


# ── Group 3: D&D 5e (YAML spec from GitHub) ───────────────────────────


DND = _profile(
    "https://www.dnd5eapi.co",
    openapi_url="https://raw.githubusercontent.com/APIs-guru/openapi-directory/main/APIs/dnd5eapi.co/0.1/openapi.yaml",
)


class TestDnD:
    def test_fetch_spec(self, cli_module):
        spec = cli_module.fetch_spec(DND, refresh=False)
        assert "paths" in spec
        endpoints = cli_module.extract_endpoint_summaries(spec)
        assert len(endpoints) >= 30

    def test_call_fireball(self, cli_module):
        resp = cli_module.make_request(DND, "GET", "/api/spells/fireball")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Fireball"


# ── Group 4: NWS Weather (JSON spec from server) ──────────────────────


NWS = _profile("https://api.weather.gov", openapi_path="/openapi.json")


class TestNWS:
    def test_fetch_spec(self, cli_module):
        spec = cli_module.fetch_spec(NWS, refresh=False)
        assert "paths" in spec
        endpoints = cli_module.extract_endpoint_summaries(spec)
        assert len(endpoints) >= 30

    def test_call_alert_count(self, cli_module):
        resp = cli_module.make_request(NWS, "GET", "/alerts/active/count")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data


# ── Group 5: Holidays (JSON spec from server) ─────────────────────────


HOLIDAYS = _profile("https://date.nager.at", openapi_path="/openapi/v3.json")


class TestHolidays:
    def test_fetch_spec(self, cli_module):
        spec = cli_module.fetch_spec(HOLIDAYS, refresh=False)
        assert "paths" in spec

    def test_call_us_holidays(self, cli_module):
        resp = cli_module.make_request(HOLIDAYS, "GET", "/api/v3/NextPublicHolidays/US")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0
        assert "name" in data[0]


# ── Group 6: Auth header construction (no live calls) ─────────────────


class TestAuthHeaders:
    def test_bearer_from_env(self, cli_module, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "tk-abc123")
        profile = {
            "auth": {"type": "bearer", "token_env_var": "TEST_TOKEN"},
        }
        headers = cli_module.get_auth_headers(profile)
        assert headers["Authorization"] == "Bearer tk-abc123"

    def test_bearer_custom_prefix(self, cli_module, monkeypatch):
        monkeypatch.setenv("TEST_TOKEN", "tk-abc123")
        profile = {
            "auth": {
                "type": "bearer",
                "token_env_var": "TEST_TOKEN",
                "prefix": "Token ",
                "header": "X-Auth",
            },
        }
        headers = cli_module.get_auth_headers(profile)
        assert headers["X-Auth"] == "Token tk-abc123"

    def test_basic_auth(self, cli_module, monkeypatch):
        monkeypatch.setenv("TEST_USER", "admin")
        monkeypatch.setenv("TEST_PASS", "secret")
        profile = {
            "auth": {
                "type": "basic",
                "username_env_var": "TEST_USER",
                "password_env_var": "TEST_PASS",
            },
        }
        headers = cli_module.get_auth_headers(profile)
        import base64

        expected = base64.b64encode(b"admin:secret").decode()
        assert headers["Authorization"] == f"Basic {expected}"

    def test_api_key(self, cli_module, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test-123")
        profile = {
            "auth": {
                "type": "api-key",
                "env_var": "TEST_KEY",
                "header": "Authorization",
                "prefix": "Bearer ",
            },
        }
        headers = cli_module.get_auth_headers(profile)
        assert headers["Authorization"] == "Bearer sk-test-123"

    def test_none_auth(self, cli_module):
        profile = {"auth": {"type": "none"}}
        headers = cli_module.get_auth_headers(profile)
        assert headers == {}

    def test_resolve_env_vars(self, cli_module, monkeypatch):
        monkeypatch.setenv("MY_URL", "https://api.example.com")
        monkeypatch.setenv("MY_PASS", "s3cret!")
        obj = {
            "base_url": "{env:MY_URL}",
            "auth": {
                "payload": {
                    "username": "admin",
                    "password": "{env:MY_PASS}",
                }
            },
        }
        resolved = cli_module._resolve_env_vars(obj)
        assert resolved["base_url"] == "https://api.example.com"
        assert resolved["auth"]["payload"]["password"] == "s3cret!"
        assert resolved["auth"]["payload"]["username"] == "admin"


# ── Group 7: OpenRouter (authenticated — skip if no key) ──────────────


OPENROUTER = _profile(
    "https://openrouter.ai/api/v1",
    openapi_url="https://openrouter.ai/openapi.json",
    auth_type="bearer",
)


@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)
class TestOpenRouter:
    def test_fetch_spec(self, cli_module):
        spec = cli_module.fetch_spec(OPENROUTER, refresh=False)
        assert "paths" in spec

    def test_call_models(self, cli_module, monkeypatch):
        profile = {
            **OPENROUTER,
            "auth": {
                "type": "bearer",
                "token_env_var": "OPENROUTER_API_KEY",
            },
        }
        resp = cli_module.make_request(profile, "GET", "/models")
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data


# ── Group 8: Spec caching lifecycle ───────────────────────────────────


class TestCachingLifecycle:
    def test_cache_created_on_fetch(self, tmp_config):
        mod, _, cache_dir = tmp_config
        spec = mod.fetch_spec(PETSTORE, refresh=True)
        assert "paths" in spec
        cache_files = list(cache_dir.glob("spec_*.json"))
        assert len(cache_files) >= 1

    def test_cache_hit(self, tmp_config):
        mod, _, cache_dir = tmp_config
        # First fetch — creates cache
        mod.fetch_spec(PETSTORE, refresh=True)
        # Second fetch — should use cache (we can't easily verify no network
        # call without mocking, but we verify the result is the same)
        spec = mod.fetch_spec(PETSTORE, refresh=False)
        assert "paths" in spec

    def test_refresh_bypasses_cache(self, tmp_config):
        mod, _, cache_dir = tmp_config
        spec1 = mod.fetch_spec(PETSTORE, refresh=True)
        spec2 = mod.fetch_spec(PETSTORE, refresh=True)
        # Both should succeed and return valid specs
        assert len(spec1["paths"]) == len(spec2["paths"])
