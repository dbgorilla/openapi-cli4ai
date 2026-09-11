"""--profile is accepted anywhere in argv, not only before the subcommand (#31)."""

import tomli_w
from typer.testing import CliRunner

from openapi_cli4ai import _state
from openapi_cli4ai.cli import _hoist_profile_option, app

runner = CliRunner()


def _write_two_profiles(mod) -> None:
    mod.CONFIG_FILE.write_text(
        tomli_w.dumps(
            {
                "active_profile": "a",
                "profiles": {
                    "a": {"base_url": "https://a.example.com", "auth": {"type": "none"}},
                    "b": {"base_url": "https://b.example.com", "auth": {"type": "none"}},
                },
            }
        )
    )


def test_hoist_moves_long_form_to_front():
    assert _hoist_profile_option(["endpoints", "--profile", "x"]) == ["--profile", "x", "endpoints"]
    assert _hoist_profile_option(["endpoints", "--profile=x", "--json"]) == ["--profile=x", "endpoints", "--json"]


def test_hoist_leaves_pre_subcommand_placement_alone():
    args = ["--profile", "x", "endpoints"]
    assert _hoist_profile_option(args) == args


def test_hoist_does_not_touch_short_p():
    # `-p` is reused by subcommands (`--password/-p`), so it must stay put.
    args = ["profile", "add", "svc", "-p", "secret"]
    assert _hoist_profile_option(args) == args


def test_hoist_stops_at_double_dash():
    args = ["call", "getX", "--", "--profile", "literal"]
    assert _hoist_profile_option(args) == args


def test_hoist_ignores_trailing_profile_without_value():
    assert _hoist_profile_option(["endpoints", "--profile"]) == ["endpoints", "--profile"]


def test_profile_after_subcommand_sets_override(cli_module, tmp_config, monkeypatch):
    mod = cli_module
    _write_two_profiles(mod)
    monkeypatch.setattr(_state, "_profile_override", None)
    result = runner.invoke(app, ["profile", "list", "--profile", "b"])
    assert result.exit_code == 0, result.output
    assert _state._profile_override == "b"
    name, _ = mod.get_active_profile()
    assert name == "b"


def test_profile_before_subcommand_still_works(cli_module, tmp_config, monkeypatch):
    mod = cli_module
    _write_two_profiles(mod)
    monkeypatch.setattr(_state, "_profile_override", None)
    result = runner.invoke(app, ["--profile", "b", "profile", "list"])
    assert result.exit_code == 0, result.output
    assert _state._profile_override == "b"


def test_profile_flag_still_beats_env(cli_module, tmp_config, monkeypatch):
    mod = cli_module
    _write_two_profiles(mod)
    monkeypatch.setenv("OAC_PROFILE", "a")
    monkeypatch.setattr(_state, "_profile_override", None)
    result = runner.invoke(app, ["profile", "list", "--profile=b"])
    assert result.exit_code == 0, result.output
    name, _ = mod.get_active_profile()
    assert name == "b"
