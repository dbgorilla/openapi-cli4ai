"""Config file, cache directory, and active-profile resolution.

Precedence for the active profile: ``--profile`` flag > ``OAC_PROFILE`` env
var > ``active_profile`` in the config file.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
import typer

from openapi_cli4ai import _state
from openapi_cli4ai._ui import _verbose, err_console

APP_NAME = "openapi-cli4ai"
CONFIG_FILE = Path.home() / ".openapi-cli4ai.toml"
CACHE_DIR = Path.home() / ".cache" / APP_NAME
CACHE_TTL = 3600  # 1 hour
ENV_PREFIX = "OAC_"


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)


def _atomic_write(target: Path, content: str, restricted: bool = False) -> None:
    """Write content to a file atomically using temp file + rename.

    Prevents partial writes from corrupting files. If restricted=True,
    sets 0o600 permissions (owner read/write only) for credential files.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077) if restricted else None
    try:
        fd, temp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(temp_path, target)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
    finally:
        if old_umask is not None:
            os.umask(old_umask)


def _safe_profile_name(name: str) -> str:
    """Sanitize a profile name for use in file paths.

    Strips path separators and traversal sequences to prevent
    writing files outside CACHE_DIR. Appends a short hash to
    avoid collisions when different raw names sanitize to the
    same basename (e.g., "a/b" and "c/b" both become "b").
    """
    # Remove any path components — only the basename matters
    safe = Path(name).name
    # Reject empty or dot-only names
    if not safe or safe in (".", ".."):
        safe = "default"
    # If the name was sanitized (different from input), append a hash
    # to avoid collisions between distinct names that share a basename
    if safe != name:
        name_hash = hashlib.sha256(name.encode()).hexdigest()[:8]
        safe = f"{safe}_{name_hash}"
    return safe


def load_profiles() -> dict:
    """Load profiles from TOML config file."""
    if not CONFIG_FILE.exists():
        return {"active_profile": None, "profiles": {}}
    try:
        data = tomllib.loads(CONFIG_FILE.read_text())
        if not isinstance(data, dict):
            err_console.print(f"[red]Error: Config file has unexpected structure ({CONFIG_FILE})[/red]")
            err_console.print("[dim]Expected a TOML table with [profiles]. Fix or delete the file.[/dim]")
            raise typer.Exit(1)
        profiles = data.get("profiles", {})
        if not isinstance(profiles, dict):
            err_console.print(f"[red]Error: 'profiles' in config is not a table ({CONFIG_FILE})[/red]")
            err_console.print("[dim]Expected [profiles.name] sections. Fix or delete the file.[/dim]")
            raise typer.Exit(1)
        data["profiles"] = profiles
        return data
    except tomllib.TOMLDecodeError as e:
        err_console.print(f"[red]Error: Config file is corrupt ({CONFIG_FILE}): {e}[/red]")
        err_console.print("[dim]Fix the file manually or delete it to start fresh.[/dim]")
        raise typer.Exit(1)
    except OSError as e:
        err_console.print(f"[red]Error: Cannot read config file ({CONFIG_FILE}): {e}[/red]")
        raise typer.Exit(1)


def save_profiles(data: dict) -> None:
    """Save profiles to TOML config file."""
    ensure_dirs()
    # TOML doesn't support None values — filter them out before writing
    clean = {k: v for k, v in data.items() if v is not None}
    _atomic_write(CONFIG_FILE, tomli_w.dumps(clean), restricted=True)


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively replace {env:VAR_NAME} placeholders with environment values."""
    if isinstance(obj, str):
        for match in re.finditer(r"\{env:([^}]+)\}", obj):
            env_name = match.group(1)
            env_val = os.environ.get(env_name)
            if env_val is None:
                _verbose(f"Environment variable {env_name} is not set (referenced as {{env:{env_name}}})")
                env_val = ""
            obj = obj.replace(match.group(0), env_val)
        return obj
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def get_active_profile() -> tuple[str, dict]:
    """Return (name, profile_dict) for the active profile.

    Priority: --profile flag > OAC_PROFILE env var > config file active_profile.
    Resolves {env:VAR_NAME} placeholders throughout the profile.
    """
    data = load_profiles()
    profiles = data.get("profiles", {})

    if not profiles:
        err_console.print("[red]No profiles configured. Run 'openapi-cli4ai init' to set one up.[/red]")
        raise typer.Exit(1)

    # Priority: --profile flag > OAC_PROFILE env var > config active_profile
    env_profile = os.environ.get(f"{ENV_PREFIX}PROFILE")
    name = _state._profile_override or env_profile or data.get("active_profile")

    if name and name not in profiles:
        if _state._profile_override:
            origin = " (from --profile)"
        elif env_profile:
            origin = " (from OAC_PROFILE env var)"
        else:
            origin = ""
        err_console.print(f"[red]Profile '{name}' not found{origin}.[/red]")
        available = ", ".join(profiles.keys())
        err_console.print(f"[dim]Available profiles: {available}[/dim]")
        raise typer.Exit(1)
    elif not name:
        name = next(iter(profiles))

    profile = _resolve_env_vars(profiles[name])
    if not isinstance(profile, dict):
        err_console.print(f"[red]Error: Profile '{name}' is not a valid table in config.[/red]")
        err_console.print("[dim]Expected [profiles.name] with base_url, auth, etc.[/dim]")
        raise typer.Exit(1)
    profile["_name"] = name  # Inject name for internal use
    return name, profile


def _spec_cache_paths(spec_url: str) -> tuple[Path, Path]:
    """Return (cache_file, meta_file) paths for a spec URL."""
    url_hash = hashlib.sha256(spec_url.encode()).hexdigest()[:12]
    return CACHE_DIR / f"spec_{url_hash}.json", CACHE_DIR / f"spec_{url_hash}.meta"


def _resolve_spec_url(profile: dict) -> str:
    """Determine the full URL for fetching the OpenAPI spec."""
    if profile.get("openapi_url"):
        return profile["openapi_url"]
    base = profile["base_url"].rstrip("/")
    path = profile.get("openapi_path", "/openapi.json").lstrip("/")
    return f"{base}/{path}"
