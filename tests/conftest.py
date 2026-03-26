"""Shared test fixtures for openapi-cli4ai tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openapi_cli4ai import cli as cli_mod


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: live API tests (may be slow)")


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def petstore_spec() -> dict:
    """Load the Petstore OpenAPI spec fixture."""
    return json.loads((FIXTURES_DIR / "petstore_spec.json").read_text())


@pytest.fixture
def cli_module():
    """Import the CLI module for unit testing."""
    return cli_mod


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Set up temporary config and cache directories."""
    config_file = tmp_path / "config.toml"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    monkeypatch.setattr(cli_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cli_mod, "CACHE_DIR", cache_dir)

    return cli_mod, tmp_path, cache_dir


@pytest.fixture
def sample_profiles() -> dict:
    """Sample profile configurations for testing."""
    return {
        "active_profile": "petstore",
        "profiles": {
            "petstore": {
                "base_url": "https://petstore3.swagger.io/api/v3",
                "openapi_path": "/openapi.json",
                "auth": {"type": "none"},
                "verify_ssl": True,
            },
            "myapp": {
                "base_url": "http://localhost:8000",
                "auth": {
                    "type": "bearer",
                    "token_endpoint": "/api/auth/token",
                    "refresh_endpoint": "/api/auth/refresh",
                    "payload": {
                        "username": "{username}",
                        "password": "{password}",
                    },
                },
                "verify_ssl": False,
            },
            "github": {
                "base_url": "https://api.github.com",
                "openapi_url": "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json",
                "auth": {
                    "type": "bearer",
                    "token_env_var": "GITHUB_TOKEN",
                },
                "headers": {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                "verify_ssl": True,
            },
            "stripe": {
                "base_url": "https://api.stripe.com",
                "auth": {
                    "type": "api-key",
                    "env_var": "STRIPE_SECRET_KEY",
                    "header": "Authorization",
                    "prefix": "Bearer ",
                },
                "verify_ssl": True,
            },
        },
    }
