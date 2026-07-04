from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def test_xquik_profile_example_is_valid_toml() -> None:
    example_path = (
        Path(__file__).resolve().parents[1] / "examples" / "xquik.profile.toml.example"
    )

    data = tomllib.loads(example_path.read_text(encoding="utf-8"))

    assert data["active_profile"] == "xquik"
    profile = data["profiles"]["xquik"]
    assert profile["base_url"] == "https://xquik.com"
    assert profile["openapi_url"] == "https://xquik.com/openapi.json"
    assert profile["auth"] == {
        "type": "api-key",
        "env_var": "XQUIK_API_KEY",
        "header": "x-api-key",
    }
