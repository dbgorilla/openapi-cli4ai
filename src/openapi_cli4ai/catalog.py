"""Profile catalog: bundled ready-made API profiles (load, search, map to a runtime profile)."""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path
from typing import Any

from rich.table import Table

from openapi_cli4ai._ui import console

CATALOG_TIERS = ("verified", "community")
_CATALOG_META_FIELDS = ("name", "description", "maintainer", "source")


def _catalog_root() -> Any:
    """Locate the catalog: bundled in the wheel, else profiles/ in a source checkout."""
    try:
        bundled = importlib.resources.files("openapi_cli4ai") / "_catalog"
        if bundled.is_dir():
            return bundled
    except (ModuleNotFoundError, FileNotFoundError, AttributeError, TypeError):
        pass
    dev = Path(__file__).resolve().parents[2] / "profiles"
    return dev if dev.is_dir() else None


def _load_catalog() -> list[dict]:
    """Return every catalog profile as a dict with _tier and _slug attached."""
    root = _catalog_root()
    entries: list[dict] = []
    if root is None:
        return entries
    for tier in CATALOG_TIERS:
        tier_dir = root / tier
        if not tier_dir.is_dir():
            continue
        for item in sorted(tier_dir.iterdir(), key=lambda p: p.name):
            if not item.name.endswith(".toml"):
                continue
            try:
                entry = tomllib.loads(item.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            entry["_tier"] = tier
            entry["_slug"] = item.name[: -len(".toml")]
            entries.append(entry)
    return entries


def _catalog_find(name: str) -> dict | None:
    for entry in _load_catalog():
        if name in (entry.get("_slug"), entry.get("name")):
            return entry
    return None


def _catalog_to_profile(entry: dict) -> dict:
    """Map a catalog entry to a runtime profile (drop catalog-only metadata)."""
    profile = {k: v for k, v in entry.items() if k not in _CATALOG_META_FIELDS and not k.startswith("_")}
    profile["verify_ssl"] = True
    return profile


def _auth_env_vars(auth: dict) -> list[str]:
    """Environment variables the user must set for this auth config."""
    keys = {
        "api-key": ("env_var",),
        "bearer": ("token_env_var",),
        "basic": ("username_env_var", "password_env_var"),
    }.get(auth.get("type", "none"), ())
    return [auth[k] for k in keys if auth.get(k)]


def _auth_uses_login(auth: dict) -> bool:
    kind = auth.get("type", "none")
    return kind in ("oidc", "device") or (kind == "bearer" and bool(auth.get("token_endpoint")))


def _auth_summary(auth: dict) -> str:
    kind = auth.get("type", "none")
    if _auth_uses_login(auth):
        return f"{kind} → run 'login'"
    env = _auth_env_vars(auth)
    return f"{kind} → set {', '.join(env)}" if env else kind


def _render_catalog(entries: list[dict], title: str) -> None:
    if not entries:
        console.print("[dim]The catalog is empty.[/dim]")
        return
    table = Table(title=f"{title} ({len(entries)})")
    table.add_column("Name", style="cyan")
    table.add_column("Tier")
    table.add_column("Description", style="green")
    for entry in sorted(entries, key=lambda e: (e.get("_tier", ""), e.get("_slug", ""))):
        tier = entry.get("_tier", "community")
        style = "bold green" if tier == "verified" else "yellow"
        table.add_row(entry.get("_slug", "?"), f"[{style}]{tier}[/{style}]", str(entry.get("description", "")))
    console.print(table)
    console.print("[dim]Install one with: openapi-cli4ai catalog install <name>[/dim]")
