"""Validate catalog profiles under profiles/{verified,community}/.

Run locally the same way CI does:

    uv run --with jsonschema python scripts/validate_profiles.py

Hard failures (exit 1) block a submission; warnings are annotations only.
The point is to make drive-by submissions self-qualifying before a human
looks at them.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = ROOT / "profiles"
SCHEMA_PATH = PROFILES_DIR / "profile.schema.json"
TIERS = ("verified", "community")

# Substrings that flag marketing copy in a description (warning only).
PROMO_TERMS = (
    "best",
    "fastest",
    "leading",
    "#1",
    "world-class",
    "cutting-edge",
    "revolutionary",
    "seamless",
    "powerful",
    "sign up",
    "free trial",
    "get started",
)
# Auth keys that must never carry an inline secret (reference an env var instead).
INLINE_SECRET_KEYS = ("token", "password", "secret", "api_key", "apikey", "client_secret")


def registrable_domain(host: str) -> str:
    """Last two labels of a hostname (heuristic, no public-suffix list).

    api.stripe.com -> stripe.com, petstore3.swagger.io -> swagger.io.
    """
    labels = host.lower().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.lower()


def spec_url_for(profile: dict) -> str:
    if profile.get("openapi_url"):
        return str(profile["openapi_url"])
    base = str(profile["base_url"]).rstrip("/")
    return base + str(profile["openapi_path"])


def parse_spec(text: str) -> dict:
    """Parse an OpenAPI document as JSON, falling back to YAML."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return yaml.safe_load(text)


def validate_profile(path: Path, schema: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a single profile file."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        profile = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [f"cannot parse TOML: {exc}"], warnings

    # Structural contract.
    try:
        jsonschema.validate(profile, schema)
    except jsonschema.ValidationError as exc:
        # Report the single most specific message; stop — later checks assume shape.
        loc = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return [f"schema: {loc}: {exc.message}"], warnings

    # Slug must match the file name.
    if profile["name"] != path.stem:
        errors.append(f"name '{profile['name']}' does not match file name '{path.stem}'")

    base_host = urlparse(profile["base_url"]).hostname or ""
    source_host = urlparse(profile["source"]).hostname or ""

    # Ownership heuristic: base_url and source must share a registrable domain.
    if registrable_domain(base_host) != registrable_domain(source_host):
        errors.append(
            f"ownership: base_url domain '{registrable_domain(base_host)}' != "
            f"source domain '{registrable_domain(source_host)}' "
            "(source should be the API's own developer/docs URL)"
        )

    # No inline secrets in auth.
    for key, value in profile.get("auth", {}).items():
        if key.lower() in INLINE_SECRET_KEYS and isinstance(value, str):
            errors.append(f"auth.{key} looks like an inline secret; use an *_env_var reference instead")

    # Marketing copy in description (warning only).
    lowered = profile["description"].lower()
    hits = [t for t in PROMO_TERMS if t in lowered]
    if hits:
        warnings.append(f"description contains promotional terms {hits}; keep it factual")

    # Spec must be reachable and parse as OpenAPI.
    spec_url = spec_url_for(profile)
    try:
        resp = httpx.get(spec_url, follow_redirects=True, timeout=20.0)
        resp.raise_for_status()
        spec = parse_spec(resp.text)
    except (httpx.HTTPError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"spec at {spec_url} not reachable/parseable: {exc}")
        return errors, warnings

    if not isinstance(spec, dict) or not (spec.get("openapi") or spec.get("swagger")):
        errors.append(f"spec at {spec_url} is not an OpenAPI/Swagger document")
    elif not spec.get("paths"):
        warnings.append(f"spec at {spec_url} declares no paths")

    spec_host = urlparse(spec_url).hostname or ""
    if registrable_domain(spec_host) != registrable_domain(base_host):
        warnings.append(
            f"spec host '{spec_host}' is off the API's domain "
            f"('{registrable_domain(base_host)}') — fine for CDN/GitHub-hosted specs, "
            "but double-check it is the canonical spec"
        )

    return errors, warnings


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    files = sorted(f for tier in TIERS for f in (PROFILES_DIR / tier).glob("*.toml"))

    if not files:
        print("No catalog profiles found; nothing to validate.")
        return 0

    failed = False
    for path in files:
        rel = path.relative_to(ROOT)
        errors, warnings = validate_profile(path, schema)
        for warn in warnings:
            print(f"::warning file={rel}::{warn}")
        if errors:
            failed = True
            for err in errors:
                print(f"::error file={rel}::{err}")
            print(f"FAIL {rel}")
        else:
            print(f"OK   {rel}" + ("  (with warnings)" if warnings else ""))

    if failed:
        print("\nProfile validation failed. See errors above.")
        return 1
    print(f"\nValidated {len(files)} profile(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
