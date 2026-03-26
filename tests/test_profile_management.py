"""Tests for profile management."""

from __future__ import annotations


def test_load_profiles_empty(tmp_config):
    """Should return empty structure when no config file exists."""
    mod, config_dir, cache_dir = tmp_config
    data = mod.load_profiles()
    assert data["profiles"] == {}
    assert data["active_profile"] is None


def test_save_and_load_profiles(tmp_config, sample_profiles):
    """Should round-trip profile data through TOML."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(sample_profiles)

    loaded = mod.load_profiles()
    assert loaded["active_profile"] == "petstore"
    assert "petstore" in loaded["profiles"]
    assert "github" in loaded["profiles"]
    assert loaded["profiles"]["petstore"]["base_url"] == "https://petstore3.swagger.io/api/v3"


def test_get_active_profile(tmp_config, sample_profiles):
    """Should return the active profile."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(sample_profiles)

    name, profile = mod.get_active_profile()
    assert name == "petstore"
    assert profile["base_url"] == "https://petstore3.swagger.io/api/v3"


def test_get_active_profile_env_override(tmp_config, sample_profiles, monkeypatch):
    """Should use OAC_PROFILE env var when set."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(sample_profiles)
    monkeypatch.setenv("OAC_PROFILE", "github")

    name, profile = mod.get_active_profile()
    assert name == "github"
    assert profile["base_url"] == "https://api.github.com"


def test_get_active_profile_fallback_to_first(tmp_config):
    """Should fall back to first profile when active_profile is None."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(
        {
            "active_profile": None,
            "profiles": {
                "only_one": {
                    "base_url": "http://example.com",
                    "auth": {"type": "none"},
                }
            },
        }
    )

    name, profile = mod.get_active_profile()
    assert name == "only_one"


def test_profile_auth_types(tmp_config, sample_profiles):
    """Should preserve different auth type configs."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(sample_profiles)
    loaded = mod.load_profiles()

    assert loaded["profiles"]["petstore"]["auth"]["type"] == "none"
    assert loaded["profiles"]["myapp"]["auth"]["type"] == "bearer"
    assert loaded["profiles"]["myapp"]["auth"]["token_endpoint"] == "/api/auth/token"
    assert loaded["profiles"]["github"]["auth"]["type"] == "bearer"
    assert loaded["profiles"]["github"]["auth"]["token_env_var"] == "GITHUB_TOKEN"
    assert loaded["profiles"]["stripe"]["auth"]["type"] == "api-key"
    assert loaded["profiles"]["stripe"]["auth"]["env_var"] == "STRIPE_SECRET_KEY"


def test_profile_custom_headers(tmp_config, sample_profiles):
    """Should preserve custom headers in profiles."""
    mod, config_dir, cache_dir = tmp_config
    mod.save_profiles(sample_profiles)
    loaded = mod.load_profiles()

    github = loaded["profiles"]["github"]
    assert github["headers"]["Accept"] == "application/vnd.github+json"
    assert github["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
