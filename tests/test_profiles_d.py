"""Drop-in profiles under profiles.d/ alongside the monolithic config file (#28)."""

import tomllib

import tomli_w
from typer.testing import CliRunner

from openapi_cli4ai import config
from openapi_cli4ai.cli import app

runner = CliRunner()


def _write_config(mod, profiles: dict, active: str | None = None) -> None:
    data = {"profiles": profiles}
    if active:
        data["active_profile"] = active
    mod.CONFIG_FILE.write_text(tomli_w.dumps(data))


def _write_dropin(name: str, profile: dict) -> None:
    config.PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    (config.PROFILES_DIR / f"{name}.toml").write_text(tomli_w.dumps(profile))


def test_dropins_are_read_alongside_config_file(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    _write_config(mod, {"a": {"base_url": "https://a.example.com", "auth": {"type": "none"}}}, active="a")
    _write_dropin("b", {"base_url": "https://b.example.com", "auth": {"type": "none"}})
    data = mod.load_profiles()
    assert set(data["profiles"]) == {"a", "b"}
    assert data["active_profile"] == "a"
    assert data["profiles"]["b"]["base_url"] == "https://b.example.com"


def test_dropin_overrides_same_named_config_entry(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    _write_config(mod, {"a": {"base_url": "https://old.example.com", "auth": {"type": "none"}}})
    _write_dropin("a", {"base_url": "https://new.example.com", "auth": {"type": "none"}})
    data = mod.load_profiles()
    assert data["profiles"]["a"]["base_url"] == "https://new.example.com"


def test_no_profiles_dir_is_fine(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    assert not config.PROFILES_DIR.exists()
    assert mod.load_profiles()["profiles"] == {}


def test_save_routes_each_profile_back_to_its_source(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    _write_config(mod, {"a": {"base_url": "https://a.example.com", "auth": {"type": "none"}}}, active="a")
    _write_dropin("b", {"base_url": "https://b.example.com", "auth": {"type": "none"}})
    data = mod.load_profiles()
    data["profiles"]["b"]["base_url"] = "https://b2.example.com"
    data["active_profile"] = "b"
    mod.save_profiles(data)

    on_disk = tomllib.loads(mod.CONFIG_FILE.read_text())
    assert set(on_disk["profiles"]) == {"a"}, "drop-ins must not be copied into the config file"
    assert on_disk["active_profile"] == "b", "active_profile lives in the config file"
    assert "_profile_files" not in on_disk
    dropin = tomllib.loads((config.PROFILES_DIR / "b.toml").read_text())
    assert dropin["base_url"] == "https://b2.example.com"


def test_removing_a_dropin_profile_deletes_its_file(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    _write_dropin("b", {"base_url": "https://b.example.com", "auth": {"type": "none"}})
    result = runner.invoke(app, ["profile", "remove", "b", "--force"])
    assert result.exit_code == 0, result.output
    assert not (config.PROFILES_DIR / "b.toml").exists()
    assert "b" not in mod.load_profiles()["profiles"]


def test_catalog_install_writes_a_dropin(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    result = runner.invoke(app, ["catalog", "install", "petstore"])
    assert result.exit_code == 0, result.output
    target = config.PROFILES_DIR / "petstore.toml"
    assert target.exists()
    assert (target.stat().st_mode & 0o777) == 0o600
    assert (config.PROFILES_DIR.stat().st_mode & 0o777) == 0o700
    dropin = tomllib.loads(target.read_text())
    assert dropin["base_url"] == "https://petstore3.swagger.io/api/v3"
    # config file holds only active_profile; the profile itself is not duplicated there
    on_disk = tomllib.loads(mod.CONFIG_FILE.read_text())
    assert on_disk["active_profile"] == "petstore"
    assert on_disk.get("profiles", {}) == {}
    assert "petstore.toml" in result.output


def test_catalog_install_over_config_entry_moves_it_to_dropin(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    _write_config(mod, {"petstore": {"base_url": "https://old.example.com", "auth": {"type": "none"}}})
    result = runner.invoke(app, ["catalog", "install", "petstore", "--force"])
    assert result.exit_code == 0, result.output
    on_disk = tomllib.loads(mod.CONFIG_FILE.read_text())
    assert "petstore" not in on_disk.get("profiles", {})
    assert mod.load_profiles()["profiles"]["petstore"]["base_url"] == "https://petstore3.swagger.io/api/v3"


def test_catalog_uninstall_removes_dropin_file(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    assert runner.invoke(app, ["catalog", "install", "petstore"]).exit_code == 0
    result = runner.invoke(app, ["catalog", "uninstall", "petstore", "--force"])
    assert result.exit_code == 0, result.output
    assert not (config.PROFILES_DIR / "petstore.toml").exists()


def test_corrupt_dropin_is_a_loud_error(tmp_config):
    mod, _tmp_path, _cache_dir = tmp_config
    config.PROFILES_DIR.mkdir(parents=True)
    (config.PROFILES_DIR / "bad.toml").write_text("this is not = toml [")
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 1
    assert "bad.toml" in result.output
