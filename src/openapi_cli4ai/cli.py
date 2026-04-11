"""openapi-cli4ai — Turn any REST API with an OpenAPI spec into an AI-ready CLI.

A CLI tool that gives any AI agent (Claude, GPT, Copilot, etc.)
the ability to discover and call any REST API. Point it at a URL, and the
agent can explore endpoints and make API calls — no MCP server, no custom
integration.

Quick start:
    openapi-cli4ai init petstore --url https://petstore3.swagger.io/api/v3
    openapi-cli4ai endpoints
    openapi-cli4ai call GET /pet/findByStatus --query status=available

Commands:
    endpoints   List and search available API endpoints
    call        Call any API endpoint directly
    init        Initialize a new API profile with guided setup
    profile     Manage API profiles (add, list, use, remove, show)

Environment variables:
    OAC_PROFILE          Override the active profile
"""

from __future__ import annotations

import base64
import binascii
from email.utils import parsedate_to_datetime
import hashlib
import html as html_module  # noqa: F401 (used by F3: OIDC callback HTML escaping)
import json
import os
import random
import re
import secrets
import sys
import tempfile
import time
import tomllib
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated, Any, Optional  # noqa: F401 (Any used by F2: resolve_refs/resolve_env_vars)

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402
import tomli_w  # noqa: E402
import typer  # noqa: E402
import yaml  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.json import JSON as RichJSON  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────
VERSION = "0.4.0"
APP_NAME = "openapi-cli4ai"
CONFIG_FILE = Path.home() / ".openapi-cli4ai.toml"
CACHE_DIR = Path.home() / ".cache" / APP_NAME
CACHE_TTL = 3600  # 1 hour
ENV_PREFIX = "OAC_"

# HTTP methods we care about from OpenAPI specs
VALID_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Colors for HTTP methods in table output
METHOD_COLORS = {
    "GET": "green",
    "POST": "yellow",
    "PUT": "blue",
    "PATCH": "magenta",
    "DELETE": "red",
    "HEAD": "cyan",
    "OPTIONS": "white",
}

# Common OpenAPI spec paths to try during init
COMMON_SPEC_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/v3/api-docs",
    "/api/openapi.json",
    "/api/v1/openapi.json",
    "/api-docs/openapi.json",
    "/docs/openapi.json",
]

console = Console()

# ── Typer App Setup ────────────────────────────────────────────────────────────
app = typer.Typer(
    name=APP_NAME,
    help="Turn any REST API with an OpenAPI spec into an AI-ready CLI.",
    no_args_is_help=True,
)
profile_app = typer.Typer(help="Manage API profiles.")
app.add_typer(profile_app, name="profile")

# Global state
_insecure_mode = False
err_console = Console(stderr=True)

_verbose_mode = False
_timeout_seconds = 60.0
_max_retries = 0


def set_insecure_mode(insecure: bool) -> None:
    global _insecure_mode
    _insecure_mode = insecure


def get_verify_ssl() -> bool:
    return not _insecure_mode


def _redact_headers(headers: dict) -> dict:
    """Return a copy of headers with sensitive values redacted for verbose output."""
    sensitive_value_prefixes = ("bearer ", "basic ", "token ")
    sensitive_exact_keys = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
    # Also redact any header whose name contains these substrings (catches custom auth headers)
    sensitive_key_patterns = ("key", "token", "secret", "password", "auth")
    redacted = {}
    for k, v in headers.items():
        k_lower = k.lower()
        v_str = str(v).lower()
        if (
            k_lower in sensitive_exact_keys
            or any(p in k_lower for p in sensitive_key_patterns)
            or any(v_str.startswith(p) for p in sensitive_value_prefixes)
        ):
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted


def _verbose(msg: str) -> None:
    """Print a verbose message to stderr if verbose mode is enabled."""
    if _verbose_mode:
        err_console.print(f"[dim]> {msg}[/dim]")


def _make_client(verify: bool = True, follow_redirects: bool = True) -> httpx.Client:
    """Create a configured httpx.Client with the global timeout.

    Callers are responsible for retry logic when _max_retries > 0.
    Set follow_redirects=False for auth requests that send credentials
    to prevent replay of secrets on 307/308 redirects.
    """
    return httpx.Client(
        verify=verify,
        timeout=_timeout_seconds,
        follow_redirects=follow_redirects,
    )


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with optional retry on 429/503.

    Respects Retry-After header. Uses exponential backoff with jitter.
    Total retry time is capped at 600s (10 minutes) across all attempts.
    Only retries idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE) to
    avoid duplicating side effects on POST/PATCH.
    """
    idempotent_methods = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
    can_retry = method.upper() in idempotent_methods
    max_attempts = max(1, _max_retries + 1) if can_retry else 1
    max_total_wait = 600.0  # 10 minute aggregate cap
    last_response = None
    total_waited = 0.0

    for attempt in range(max_attempts):
        _verbose(f"{method} {url}" + (f" (attempt {attempt + 1}/{max_attempts})" if attempt > 0 else ""))
        response = client.request(method=method, url=url, **kwargs)
        last_response = response

        if response.status_code not in (429, 503) or attempt >= max_attempts - 1:
            return response

        # Determine wait time (capped at 300s per attempt to prevent server-controlled DoS)
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait = min(float(retry_after), 300.0)
            except ValueError:
                # Retry-After may be an HTTP-date (RFC 7231 §7.1.3)
                try:
                    retry_dt = parsedate_to_datetime(retry_after)
                    wait = min((retry_dt.timestamp() - time.time()), 300.0)
                except (ValueError, TypeError):
                    wait = 2**attempt
        else:
            wait = 2**attempt

        # Floor at 0 (servers may send negative Retry-After), add jitter, cap at 300s
        wait = max(0.0, wait)
        wait = min(wait + random.uniform(0, wait * 0.25), 300.0)

        # Enforce aggregate cap
        if total_waited + wait > max_total_wait:
            _verbose(f"Aggregate retry cap ({max_total_wait:.0f}s) reached, returning last response")
            return response

        _verbose(f"Got {response.status_code}, retrying in {wait:.1f}s...")
        time.sleep(wait)
        total_waited += wait

    return last_response  # type: ignore[return-value]


# ── Directory Helpers ──────────────────────────────────────────────────────────
def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)


def _resolve_file_path(file_path: str | Path, purpose: str = "file") -> Path:
    """Resolve a user-supplied file path, following symlinks.

    Warns if the resolved path is outside the current working directory.
    Does NOT block access — this tool is for power users and AI agents
    who may legitimately read files from anywhere.
    """
    resolved = Path(file_path).resolve()
    try:
        cwd = Path.cwd().resolve()
        resolved.relative_to(cwd)
    except ValueError:
        err_console.print(f"[yellow]Warning: {purpose} path resolves outside working directory: {resolved}[/yellow]")
    except OSError:
        pass  # CWD may not exist in some edge cases
    return resolved


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


def _save_token(profile_name: str, token_data: dict) -> Path:
    """Cache an OAuth/OIDC token with restricted permissions.

    Returns the path to the cached token file.
    """
    token_cache = CACHE_DIR / f"{_safe_profile_name(profile_name)}_token.json"
    ensure_dirs()
    _atomic_write(token_cache, json.dumps(token_data), restricted=True)
    return token_cache


def _require_env_var(env_var: str, label: str, quiet: bool = False) -> str:
    """Get a required value from an environment variable.

    Raises typer.Exit(1) with a helpful message if not set.
    """
    value = os.environ.get(env_var, "")
    if not value:
        if not quiet:
            err_console.print(f"[red]Set the {env_var} environment variable with your {label}.[/red]")
        raise typer.Exit(1)
    return value


# ── Profile Management ────────────────────────────────────────────────────────
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

    Priority: OAC_PROFILE env var > config file active_profile.
    Resolves {env:VAR_NAME} placeholders throughout the profile.
    """
    data = load_profiles()
    profiles = data.get("profiles", {})

    if not profiles:
        err_console.print("[red]No profiles configured. Run 'openapi-cli4ai init' to set one up.[/red]")
        raise typer.Exit(1)

    # Check env var override
    env_profile = os.environ.get(f"{ENV_PREFIX}PROFILE")
    name = env_profile or data.get("active_profile")

    if name and name not in profiles:
        err_console.print(
            f"[red]Profile '{name}' not found{' (from OAC_PROFILE env var)' if env_profile else ''}.[/red]"
        )
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


# ── Spec Fetching & Caching ───────────────────────────────────────────────────
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


def fetch_spec(profile: dict, refresh: bool = False) -> dict:
    """Fetch OpenAPI spec with caching and stale fallback."""
    spec_url = _resolve_spec_url(profile)
    cache_file, cache_meta = _spec_cache_paths(spec_url)

    # Check cache freshness
    if not refresh and cache_meta.exists() and cache_file.exists():
        try:
            meta = json.loads(cache_meta.read_text())
            if not isinstance(meta, dict):
                raise ValueError("cache meta is not a JSON object")
            age = time.time() - meta.get("fetched_at", 0)
            if age < CACHE_TTL:
                cached_spec = json.loads(cache_file.read_text())
                if isinstance(cached_spec, dict):
                    return cached_spec
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
            pass

    # Fetch fresh spec
    try:
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        headers = dict(profile.get("headers", {}))
        # Add auth if available
        try:
            auth_headers = get_auth_headers(profile, quiet=True)
            headers.update(auth_headers)
        except (typer.Exit, httpx.HTTPError, OSError, KeyError, TypeError, ValueError, AttributeError) as e:
            _verbose(f"Auth headers unavailable for spec fetch: {e}")

        with _make_client(verify=verify) as client:
            resp = client.get(spec_url, headers=headers)
            resp.raise_for_status()

        # Validate response is actually a spec, not HTML from a frontend SPA
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            raise ValueError(
                f"Got HTML instead of OpenAPI spec from {spec_url} — the server may have a frontend catch-all route"
            )

        # Handle JSON or YAML
        if "yaml" in content_type or spec_url.endswith((".yaml", ".yml")):
            spec = yaml.safe_load(resp.text)
        else:
            spec = resp.json()

        # Write cache atomically to prevent partial writes
        ensure_dirs()
        _atomic_write(cache_file, json.dumps(spec))
        _atomic_write(cache_meta, json.dumps({"fetched_at": time.time(), "url": spec_url}))

        return spec

    except (httpx.HTTPError, json.JSONDecodeError, yaml.YAMLError, ValueError, OSError) as e:
        # Fallback to stale cache on network, parse, or I/O errors
        if cache_file.exists():
            err_console.print(f"[yellow]Warning: Using stale cached spec ({e})[/yellow]")
            try:
                return json.loads(cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        err_console.print(f"[red]Failed to fetch spec from {spec_url}: {e}[/red]")
        raise typer.Exit(1)


# ── Endpoint Extraction ───────────────────────────────────────────────────────
def extract_endpoint_summaries(spec: dict) -> list[dict]:
    """Extract compact endpoint summaries from an OpenAPI spec."""
    endpoints = []
    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if method.lower() not in VALID_METHODS or not isinstance(operation, dict):
                continue
            summary = operation.get("summary", "") or operation.get("description", "") or ""
            # Truncate to first sentence, max 120 chars
            if summary:
                first_sentence = summary.split(". ")[0]
                summary = first_sentence[:120]
            endpoints.append(
                {
                    "operationId": operation.get("operationId", f"{method}_{path}"),
                    "method": method.upper(),
                    "path": path,
                    "summary": summary,
                    "tags": operation.get("tags", []),
                    "deprecated": operation.get("deprecated", False),
                }
            )
    return endpoints


# ── $ref Resolution ────────────────────────────────────────────────────────────
def _merge_allof(schemas: list[dict]) -> dict:
    """Merge a list of schemas from an allOf into a single schema.

    Combines all keys from sub-schemas. For 'properties' and 'required',
    values are merged (combined). For other keys, later schemas win.
    """
    merged: dict = {}
    required: list[str] = []
    properties: dict = {}
    for s in schemas:
        if not isinstance(s, dict):
            continue
        for k, v in s.items():
            if k == "properties":
                properties.update(v)
            elif k == "required":
                required.extend(v)
            else:
                merged[k] = v  # Later schemas win for all non-merge keys
    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = sorted(set(required))
    return merged


def resolve_refs(schema: Any, spec_root: dict, max_depth: int = 10) -> Any:
    """Recursively resolve $ref pointers and composition keywords in an OpenAPI schema.

    Handles:
      - $ref: follows JSON pointer and resolves the referenced schema
      - allOf: merges all sub-schemas into a single object (combined properties)
      - oneOf/anyOf: resolves each variant and presents as a list
    """
    if max_depth <= 0:
        return schema
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref_path = schema["$ref"]  # e.g., "#/components/schemas/User"
            if not ref_path.startswith("#/"):
                return schema  # External ref, can't resolve
            parts = ref_path.lstrip("#/").split("/")
            resolved = spec_root
            for part in parts:
                if isinstance(resolved, dict):
                    resolved = resolved.get(part, {})
                else:
                    return schema
            resolved_schema = resolve_refs(resolved, spec_root, max_depth - 1)
            # Preserve sibling keys alongside $ref (e.g., description, nullable)
            # Local siblings override scalars but merge object keywords (properties, required)
            siblings = {k: resolve_refs(v, spec_root, max_depth - 1) for k, v in schema.items() if k != "$ref"}
            if siblings and isinstance(resolved_schema, dict):
                merged = dict(resolved_schema)
                for k, v in siblings.items():
                    if k == "properties" and isinstance(v, dict) and isinstance(merged.get("properties"), dict):
                        merged["properties"] = {**merged["properties"], **v}
                    elif k == "required" and isinstance(v, list) and isinstance(merged.get("required"), list):
                        merged["required"] = sorted(set(merged["required"] + v))
                    else:
                        merged[k] = v
                return merged
            return resolved_schema

        # allOf: merge all sub-schemas into one
        if "allOf" in schema:
            resolved_schemas = [resolve_refs(s, spec_root, max_depth - 1) for s in schema["allOf"]]
            merged = _merge_allof(resolved_schemas)
            # Parent wrapper metadata: merge properties/required, override scalars
            for k, v in schema.items():
                if k == "allOf":
                    continue
                resolved_v = resolve_refs(v, spec_root, max_depth - 1)
                if k == "properties" and isinstance(resolved_v, dict) and isinstance(merged.get("properties"), dict):
                    merged["properties"] = {**merged["properties"], **resolved_v}
                elif k == "required" and isinstance(resolved_v, list) and isinstance(merged.get("required"), list):
                    merged["required"] = sorted(set(merged["required"] + resolved_v))
                else:
                    merged[k] = resolved_v
            return merged

        # oneOf / anyOf: resolve each variant, present as list
        for keyword in ("oneOf", "anyOf"):
            if keyword in schema:
                resolved_variants = [resolve_refs(s, spec_root, max_depth - 1) for s in schema[keyword]]
                result = {keyword: resolved_variants}
                # Preserve sibling keys (discriminator, description, etc.)
                for k, v in schema.items():
                    if k != keyword:
                        result[k] = resolve_refs(v, spec_root, max_depth - 1)
                return result

        return {k: resolve_refs(v, spec_root, max_depth - 1) for k, v in schema.items()}
    elif isinstance(schema, list):
        return [resolve_refs(item, spec_root, max_depth - 1) for item in schema]
    return schema


def extract_full_endpoint_schema(spec: dict, operation_id: str) -> dict | None:
    """Extract the full schema for a specific endpoint, resolving $ref pointers."""
    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            op_id = operation.get("operationId", f"{method}_{path}")
            if op_id == operation_id:
                resolved = resolve_refs(operation, spec)
                # Merge path-level parameters with operation-level parameters.
                # Operation-level params override path-level params with the same name+in.
                path_params = resolve_refs(methods.get("parameters", []), spec)
                op_params = resolved.get("parameters", [])
                # Build a lookup of operation params by (name, in) for dedup
                op_param_keys = set()
                for p in op_params:
                    if isinstance(p, dict) and "name" in p:
                        op_param_keys.add((p["name"], p.get("in", "")))
                # Include path params not overridden by operation params
                merged_params = list(op_params)
                for p in path_params:
                    if isinstance(p, dict) and "name" in p:
                        if (p["name"], p.get("in", "")) not in op_param_keys:
                            merged_params.append(p)
                return {
                    "method": method.upper(),
                    "path": path,
                    "operationId": operation_id,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                    "parameters": merged_params,
                    "requestBody": resolved.get("requestBody"),
                    "responses": _summarize_responses(resolved.get("responses", {})),
                }
    return None


def _summarize_responses(responses: dict) -> dict:
    """Create a compact summary of response schemas."""
    summary = {}
    if not isinstance(responses, dict):
        return summary
    for status, resp in responses.items():
        if not isinstance(resp, dict):
            continue
        content = resp.get("content", {})
        json_schema = {}
        if isinstance(content, dict):
            json_content = content.get("application/json", {})
            if isinstance(json_content, dict):
                json_schema = json_content.get("schema", {})
        summary[status] = {
            "description": resp.get("description", ""),
            "schema": _compact_schema(json_schema) if json_schema else None,
        }
    return summary


def _compact_schema(schema: dict, max_props: int = 15) -> dict:
    """Create a compact version of a schema, limiting property count."""
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "object" and "properties" in schema:
        props = dict(list(schema["properties"].items())[:max_props])
        total = len(schema["properties"])
        result = {"type": "object", "properties": props}
        if "required" in schema:
            result["required"] = schema["required"]
        if total > max_props:
            result["_note"] = f"{total - max_props} more properties omitted"
        return result
    if schema.get("type") == "array" and "items" in schema:
        return {"type": "array", "items": _compact_schema(schema["items"], max_props)}
    return schema


# ── Auth Management ────────────────────────────────────────────────────────────
def get_auth_headers(profile: dict, quiet: bool = False) -> dict:
    """Build auth headers based on profile's auth config."""
    auth_config = profile.get("auth", {})
    auth_type = auth_config.get("type", "none")

    if auth_type == "none":
        return {}
    elif auth_type == "bearer":
        return _bearer_auth(profile, auth_config, quiet)
    elif auth_type in ("oidc", "device", "auto"):
        return _oidc_auth(profile, auth_config, quiet)
    elif auth_type == "api-key":
        return _api_key_auth(auth_config, quiet)
    elif auth_type == "basic":
        return _basic_auth(auth_config, quiet)
    else:
        if not quiet:
            err_console.print(f"[red]Unknown auth type: {auth_type}[/red]")
        raise typer.Exit(1)


def _bearer_auth(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle bearer token auth — static from env or OAuth flow."""
    # Static token from env var
    env_var = auth_config.get("token_env_var")
    if env_var:
        token = _require_env_var(env_var, "token", quiet=quiet)
        prefix = auth_config.get("prefix", "Bearer ")
        header = auth_config.get("header", "Authorization")
        return {header: f"{prefix}{token}"}

    # OAuth token-endpoint flow
    token_endpoint = auth_config.get("token_endpoint")
    if token_endpoint:
        return _oauth_bearer(profile, auth_config, quiet)

    if not quiet:
        err_console.print("[red]Bearer auth requires either token_env_var or token_endpoint in profile.[/red]")
    raise typer.Exit(1)


def _oauth_bearer(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle OAuth password-grant or similar token endpoint flows."""
    profile_name = profile.get("_name", "default")
    token_cache = CACHE_DIR / f"{_safe_profile_name(profile_name)}_token.json"

    # Check cached token
    if token_cache.exists():
        try:
            cached = json.loads(token_cache.read_text())
            expires_at = cached.get("expires_at", 0)
            # 5-minute buffer before expiry
            if time.time() < (expires_at - 300):
                return {"Authorization": f"Bearer {cached['access_token']}"}

            # Try refresh
            refresh_endpoint = auth_config.get("refresh_endpoint")
            if refresh_endpoint and "refresh_token" in cached:
                refreshed = _try_refresh_token(profile, auth_config, cached)
                if refreshed:
                    return {"Authorization": f"Bearer {refreshed['access_token']}"}
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Need fresh login
    if not quiet:
        err_console.print("[yellow]Token expired or missing. Run 'openapi-cli4ai login' to authenticate.[/yellow]")
    raise typer.Exit(1)


def _try_refresh_token(profile: dict, auth_config: dict, cached: dict) -> dict | None:
    """Attempt to refresh an OAuth token."""
    refresh_endpoint = auth_config.get("refresh_endpoint")
    if not refresh_endpoint:
        return None
    try:
        base_url = profile["base_url"].rstrip("/")
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(
                f"{base_url}{refresh_endpoint}",
                headers={"Authorization": f"Bearer {cached['refresh_token']}"},
            )
            if resp.status_code == 200:
                new_data = resp.json()
                if "expires_in" in new_data:
                    new_data["expires_at"] = time.time() + float(new_data["expires_in"])
                elif "expires_at" not in new_data:
                    new_data["expires_at"] = time.time() + 86400  # 24h default
                # Preserve existing refresh_token if server omits a new one
                if "refresh_token" not in new_data and "refresh_token" in cached:
                    new_data["refresh_token"] = cached["refresh_token"]
                _save_token(profile.get("_name", "default"), new_data)
                return new_data
    except (httpx.HTTPError, json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        pass
    return None


# ── OIDC (Authorization Code + PKCE) ─────────────────────────────────────────


def _oidc_auth(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle OIDC auth -- cached token with form-encoded refresh."""
    profile_name = profile.get("_name", "default")
    token_cache = CACHE_DIR / f"{_safe_profile_name(profile_name)}_token.json"

    if token_cache.exists():
        try:
            cached = json.loads(token_cache.read_text())
            expires_at = cached.get("expires_at", 0)

            # Valid token -- use it
            if time.time() < (expires_at - 300):
                return {"Authorization": f"Bearer {cached['access_token']}"}

            # Try refresh (form-encoded, standard OIDC)
            if "refresh_token" in cached:
                verify = profile.get("verify_ssl", True) and get_verify_ssl()
                refreshed = _oidc_refresh(auth_config, cached, verify=verify)
                if refreshed:
                    refreshed["expires_at"] = time.time() + float(refreshed.get("expires_in", 300))
                    # Preserve existing refresh_token if server omits a new one
                    if "refresh_token" not in refreshed and "refresh_token" in cached:
                        refreshed["refresh_token"] = cached["refresh_token"]
                    _save_token(profile_name, refreshed)
                    return {"Authorization": f"Bearer {refreshed['access_token']}"}
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
            pass

    if not quiet:
        err_console.print("[yellow]Token expired or missing. Run 'openapi-cli4ai login' to authenticate.[/yellow]")
    raise typer.Exit(1)


def _oidc_refresh(auth_config: dict, cached: dict, verify: bool = True) -> dict | None:
    """Refresh an OIDC token using form-encoded POST (standard OIDC spec)."""
    token_url = auth_config.get("token_url", "")
    client_id = auth_config.get("client_id", "")
    if not token_url or not client_id:
        return None
    try:
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(
                token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": cached["refresh_token"],
                },
            )
            if resp.status_code == 200:
                return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        pass
    return None


class _OIDCCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OIDC authorization code callback."""

    auth_code: str | None = None
    error: str | None = None
    expected_state: str | None = None

    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        # Validate state to prevent CSRF
        received_state = params.get("state", [None])[0]
        if _OIDCCallbackHandler.expected_state and received_state != _OIDCCallbackHandler.expected_state:
            _OIDCCallbackHandler.error = "state_mismatch"
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login failed</h2><p>State mismatch - possible CSRF attack.</p></body></html>"
            )
            return

        if "code" in params:
            _OIDCCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Login successful</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        else:
            _OIDCCallbackHandler.error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            safe_error = html_module.escape(str(_OIDCCallbackHandler.error))
            self.wfile.write(f"<html><body><h2>Login failed</h2><p>{safe_error}</p></body></html>".encode())

    def log_message(self, format: str, *args: object) -> None:
        pass  # Suppress HTTP server logging


def _oidc_login(
    auth_config: dict,
    profile_name: str,
    no_browser: bool = False,
    verify: bool = True,
    base_url: str = "",
) -> None:
    """Run OIDC Authorization Code + PKCE flow.

    With browser (default): opens browser, listens on localhost for callback.
    Without browser (--no-browser): prints URL, user pastes redirect URL back.
    """
    authorize_url = auth_config.get("authorize_url", "")
    token_url = auth_config.get("token_url", "")
    client_id = auth_config.get("client_id", "")
    scopes = auth_config.get("scopes", "openid")
    callback_port = auth_config.get("callback_port", 8484)
    callback_timeout = auth_config.get("callback_timeout", 120)
    redirect_uri = auth_config.get("redirect_uri", f"http://localhost:{callback_port}/callback")

    if not authorize_url or not token_url or not client_id:
        err_console.print("[red]OIDC auth requires authorize_url, token_url, and client_id.[/red]")
        raise typer.Exit(1)

    # PKCE: generate code verifier + challenge
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)

    # Build authorization URL
    auth_params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    full_auth_url = f"{authorize_url}?{auth_params}"

    if no_browser:
        auth_code = _oidc_login_no_browser(full_auth_url, expected_state=state)
    else:
        auth_code = _oidc_login_browser(full_auth_url, callback_port, state, timeout=callback_timeout)

    # Exchange code for tokens
    _oidc_exchange_code(
        token_url=token_url,
        client_id=client_id,
        auth_code=auth_code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        profile_name=profile_name,
        verify=verify,
        auth_config=auth_config,
        base_url=base_url,
    )


def _oidc_login_browser(full_auth_url: str, callback_port: int, state: str, timeout: int = 120) -> str:
    """Open browser and listen for the OIDC callback on localhost."""
    _OIDCCallbackHandler.auth_code = None
    _OIDCCallbackHandler.error = None
    _OIDCCallbackHandler.expected_state = state

    try:
        server = HTTPServer(("127.0.0.1", callback_port), _OIDCCallbackHandler)
    except OSError as e:
        err_console.print(f"[red]Cannot start OIDC callback server on port {callback_port}: {e}[/red]")
        err_console.print(
            "[dim]Another process may be using this port. Try a different callback_port in your profile.[/dim]"
        )
        raise typer.Exit(1)

    server.timeout = timeout

    err_console.print(f"[dim]Listening on http://localhost:{callback_port}/callback[/dim]")
    console.print("[bold]Opening browser for login...[/bold]")
    webbrowser.open(full_auth_url)
    console.print(f"[dim]Waiting for callback ({timeout}s timeout)...[/dim]")

    try:
        server.handle_request()
    finally:
        server.server_close()

    if _OIDCCallbackHandler.error:
        err_console.print(f"[red]OIDC error: {_OIDCCallbackHandler.error}[/red]")
        raise typer.Exit(1)
    if not _OIDCCallbackHandler.auth_code:
        err_console.print("[red]No authorization code received.[/red]")
        raise typer.Exit(1)

    return _OIDCCallbackHandler.auth_code


def _oidc_login_no_browser(full_auth_url: str, expected_state: str | None = None) -> str:
    """Print the auth URL, user pastes back the redirect URL containing the code."""
    console.print("\n[bold]Open this URL in any browser to log in:[/bold]\n")
    console.print(f"  {full_auth_url}\n")
    console.print(
        "[dim]After login, your browser will redirect to a localhost URL.\n"
        "Copy the full URL from your browser's address bar and paste it below.[/dim]\n"
    )
    redirect_input = typer.prompt("Paste the redirect URL")

    # Extract the authorization code from the pasted URL
    parsed = urllib.parse.urlparse(redirect_input.strip())
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        err_console.print(f"[red]OIDC error: {params['error'][0]}[/red]")
        raise typer.Exit(1)

    # Validate state to prevent CSRF
    if expected_state:
        received_state = params.get("state", [None])[0]
        if received_state != expected_state:
            err_console.print("[red]OIDC error: state mismatch — possible CSRF attack.[/red]")
            err_console.print("[dim]The state parameter in the redirect URL does not match the expected value.[/dim]")
            raise typer.Exit(1)

    if "code" not in params:
        err_console.print("[red]No authorization code found in the URL.[/red]")
        err_console.print("[dim]Expected a URL like: http://localhost:.../callback?code=...&state=...[/dim]")
        raise typer.Exit(1)

    return params["code"][0]


def _oidc_exchange_code(
    *,
    token_url: str,
    client_id: str,
    auth_code: str,
    redirect_uri: str,
    code_verifier: str,
    profile_name: str,
    verify: bool = True,
    auth_config: dict | None = None,
    base_url: str = "",
) -> None:
    """Exchange an authorization code for tokens and cache them."""
    try:
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": auth_code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )

        if resp.status_code != 200:
            err_console.print(f"[red]Token exchange failed ({resp.status_code}):[/red]")
            err_console.print(resp.text)
            raise typer.Exit(1)

        token_data = resp.json()

        # Two-phase auth: exchange IdP tokens for local API tokens
        if auth_config and auth_config.get("token_exchange_endpoint") and base_url:
            token_data = _token_exchange(token_data, auth_config, base_url, verify=verify)

        if "expires_in" in token_data:
            try:
                token_data["expires_at"] = time.time() + float(token_data["expires_in"])
            except (TypeError, ValueError):
                token_data["expires_at"] = time.time() + 86400
        elif "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + 86400

        token_cache = _save_token(profile_name, token_data)

        console.print("[green]Logged in successfully![/green]")
        err_console.print(f"[dim]Token cached at {token_cache}[/dim]")

    except httpx.HTTPError as e:
        err_console.print(f"[red]Token exchange failed: {e}[/red]")
        raise typer.Exit(1)


# ── Token Exchange (two-phase auth) ───────────────────────────────────────────


def _token_exchange(
    token_data: dict,
    auth_config: dict,
    base_url: str,
    verify: bool = True,
) -> dict:
    """Exchange IdP tokens for local API tokens via a token exchange endpoint.

    If token_exchange_endpoint is configured, POST the IdP tokens to it and
    return the exchange response. Otherwise, return the original token_data.
    """
    exchange_endpoint = auth_config.get("token_exchange_endpoint")
    if not exchange_endpoint:
        return token_data

    # Build exchange body safely via dict to prevent JSON injection from token values
    body_template = auth_config.get("token_exchange_body")
    if body_template:
        # Escape token values for safe JSON substitution (handles quotes, backslashes, control chars)
        safe_access = json.dumps(token_data.get("access_token", ""))[1:-1]  # strip surrounding quotes
        safe_refresh = json.dumps(token_data.get("refresh_token", ""))[1:-1]
        body_str = body_template.replace("{access_token}", safe_access).replace("{refresh_token}", safe_refresh)
        try:
            exchange_body = json.loads(body_str)
        except json.JSONDecodeError:
            err_console.print("[red]Token exchange body template produced invalid JSON[/red]")
            raise typer.Exit(1)
    else:
        # Default: safe dict construction, backward-compatible payload shape
        exchange_body = {"access_token": token_data.get("access_token", "")}

    exchange_url = f"{base_url.rstrip('/')}{exchange_endpoint}"
    try:
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(
                exchange_url,
                json=exchange_body,
            )
        if resp.status_code != 200:
            err_console.print(f"[red]Token exchange failed ({resp.status_code}):[/red]")
            err_console.print(resp.text)
            raise typer.Exit(1)
        return resp.json()
    except httpx.HTTPError as e:
        err_console.print(f"[red]Token exchange failed: {e}[/red]")
        raise typer.Exit(1)


# ── OAuth 2.0 Device Authorization Flow (RFC 8628) ───────────────────────────


def _device_discover_endpoints(auth_config: dict, verify: bool = True) -> dict:
    """Discover device authorization and token endpoints.

    Priority: device_config_url > issuer_url (well-known) > explicit endpoints.
    Returns dict with device_authorization_endpoint, token_endpoint, client_id.
    """
    device_config_url = auth_config.get("device_config_url")
    issuer_url = auth_config.get("issuer_url")

    if device_config_url:
        try:
            with _make_client(verify=verify) as client:
                resp = client.get(device_config_url)
            if resp.status_code == 200:
                config = resp.json()
                return {
                    "device_authorization_endpoint": config["device_authorization_endpoint"],
                    "token_endpoint": config["token_endpoint"],
                    "client_id": config.get("client_id", auth_config.get("client_id", "")),
                }
        except (httpx.HTTPError, KeyError, json.JSONDecodeError) as e:
            err_console.print(f"[red]Failed to fetch device config from {device_config_url}: {e}[/red]")
            raise typer.Exit(1)

    if issuer_url:
        well_known_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
        try:
            with _make_client(verify=verify) as client:
                resp = client.get(well_known_url)
            if resp.status_code == 200:
                oidc_config = resp.json()
                device_ep = oidc_config.get("device_authorization_endpoint")
                token_ep = oidc_config.get("token_endpoint")
                if not device_ep:
                    err_console.print(
                        f"[red]Issuer {issuer_url} does not advertise device_authorization_endpoint.[/red]"
                    )
                    raise typer.Exit(1)
                return {
                    "device_authorization_endpoint": device_ep,
                    "token_endpoint": token_ep,
                    "client_id": auth_config.get("client_id", ""),
                }
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            err_console.print(f"[red]Failed to fetch OIDC discovery from {well_known_url}: {e}[/red]")
            raise typer.Exit(1)

    # Explicit endpoints
    device_ep = auth_config.get("device_authorization_endpoint")
    token_ep = auth_config.get("token_endpoint")
    client_id = auth_config.get("client_id", "")
    if not device_ep or not token_ep:
        err_console.print(
            "[red]Device auth requires device_config_url, issuer_url, or explicit "
            "device_authorization_endpoint + token_endpoint.[/red]"
        )
        raise typer.Exit(1)
    return {
        "device_authorization_endpoint": device_ep,
        "token_endpoint": token_ep,
        "client_id": client_id,
    }


def _device_login(
    auth_config: dict, profile_name: str, profile: dict, no_browser: bool = False, verify: bool = True
) -> None:
    """Run OAuth 2.0 Device Authorization Grant (RFC 8628) flow."""
    endpoints = _device_discover_endpoints(auth_config, verify=verify)
    device_ep = endpoints["device_authorization_endpoint"]
    token_ep = endpoints["token_endpoint"]
    client_id = endpoints.get("client_id") or auth_config.get("client_id", "")
    scopes = auth_config.get("scopes", "openid")

    if not client_id:
        err_console.print("[red]Device auth requires client_id.[/red]")
        raise typer.Exit(1)

    # Step 1: Request device code
    try:
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(
                device_ep,
                data={"client_id": client_id, "scope": scopes},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            err_console.print(f"[red]Device authorization request failed ({resp.status_code}):[/red]")
            err_console.print(resp.text)
            raise typer.Exit(1)
    except httpx.HTTPError as e:
        err_console.print(f"[red]Device authorization failed: {e}[/red]")
        raise typer.Exit(1)

    try:
        device_data = resp.json()
        device_code = device_data["device_code"]
        user_code = device_data["user_code"]
        verification_uri = device_data["verification_uri"]
    except (json.JSONDecodeError, KeyError) as e:
        err_console.print(f"[red]Device authorization response missing required fields: {e}[/red]")
        raise typer.Exit(1)
    verification_uri_complete = device_data.get("verification_uri_complete")
    try:
        expires_in = float(device_data.get("expires_in", 600))
    except (TypeError, ValueError):
        expires_in = 600.0
    interval = device_data.get("interval", 5)

    # Step 2: Display instructions
    display_uri = verification_uri_complete or verification_uri
    err_console.print(f"\n[bold]Open this URL in your browser:[/bold]\n  {display_uri}\n")
    err_console.print(f"[bold]Code:[/bold] {user_code}")
    err_console.print("[dim]Waiting for authorization...[/dim]\n")

    if not no_browser:
        webbrowser.open(display_uri)

    # Step 3: Poll for token
    deadline = time.time() + expires_in
    with _make_client(verify=verify, follow_redirects=False) as client:
        while time.time() < deadline:
            time.sleep(interval)
            try:
                poll_resp = client.post(
                    token_ep,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "client_id": client_id,
                        "device_code": device_code,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError:
                continue

            if poll_resp.status_code == 200:
                token_data = poll_resp.json()
                break
            else:
                try:
                    error_data = poll_resp.json()
                    error_code = error_data.get("error", "")
                except (json.JSONDecodeError, ValueError):
                    continue

                if error_code == "authorization_pending":
                    continue
                elif error_code == "slow_down":
                    interval += 5
                    continue
                elif error_code == "expired_token":
                    err_console.print("[red]Device code expired. Please try again.[/red]")
                    raise typer.Exit(1)
                elif error_code == "access_denied":
                    err_console.print("[red]Authorization was denied.[/red]")
                    raise typer.Exit(1)
                else:
                    err_console.print(f"[red]Device flow error: {error_code}[/red]")
                    raise typer.Exit(1)
        else:
            err_console.print("[red]Device code expired (timeout). Please try again.[/red]")
            raise typer.Exit(1)

    # Step 4: Token exchange (two-phase auth) if configured
    base_url = profile.get("base_url", "")
    if auth_config.get("token_exchange_endpoint") and base_url:
        token_data = _token_exchange(token_data, auth_config, base_url, verify=verify)

    # Step 5: Cache token
    if "expires_in" in token_data:
        token_data["expires_at"] = time.time() + float(token_data["expires_in"])
    elif "expires_at" not in token_data:
        token_data["expires_at"] = time.time() + 86400

    token_cache = _save_token(profile_name, token_data)

    console.print("[green]Logged in successfully![/green]")
    err_console.print(f"[dim]Token cached at {token_cache}[/dim]")


def _api_key_auth(auth_config: dict, quiet: bool = False) -> dict:
    """Handle API key auth via custom header."""
    env_var = auth_config.get("env_var", "")
    if not env_var:
        if not quiet:
            err_console.print("[red]API key auth requires 'env_var' in profile config.[/red]")
        raise typer.Exit(1)
    key = _require_env_var(env_var, "API key", quiet=quiet)
    header = auth_config.get("header", "X-API-Key")
    prefix = auth_config.get("prefix", "")
    return {header: f"{prefix}{key}"}


def _basic_auth(auth_config: dict, quiet: bool = False) -> dict:
    """Handle HTTP basic auth."""
    user_var = auth_config.get("username_env_var", "")
    pass_var = auth_config.get("password_env_var", "")
    if not user_var or not pass_var:
        if not quiet:
            err_console.print("[red]Basic auth requires 'username_env_var' and 'password_env_var' in profile.[/red]")
        raise typer.Exit(1)
    username = _require_env_var(user_var, "username", quiet=quiet)
    password = _require_env_var(pass_var, "password", quiet=quiet)
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _get_password(auth_config: dict) -> str:
    """Get password via multiple methods in priority order."""
    # 1. Env var
    env_var = auth_config.get("password_env_var")
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]

    # 2. Password file
    password_file = auth_config.get("password_file")
    if password_file:
        pf = Path(password_file)
        if pf.exists():
            return pf.read_text().strip()

    # 3. Stdin (if piped)
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()

    # 4. Interactive prompt
    return typer.prompt("Password", hide_input=True)


# ── HTTP & Response Handling ───────────────────────────────────────────────────
def make_request(
    profile: dict,
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict | None = None,
    extra_headers: dict | None = None,
) -> httpx.Response:
    """Make an authenticated HTTP request."""
    base_url = profile["base_url"].rstrip("/")
    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    if extra_headers:
        headers.update(extra_headers)

    url = f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}"

    with _make_client(verify=verify) as client:
        return client.request(
            method=method.upper(),
            url=url,
            json=json_body,
            params=params,
            headers=headers,
        )


def _safe_json_or_text(response: httpx.Response) -> dict | list | str:
    """Try to parse response as JSON, fall back to text if it fails.

    Servers sometimes claim application/json content-type but return
    HTML error pages or malformed bodies.
    """
    if "json" in response.headers.get("content-type", ""):
        try:
            return response.json()
        except json.JSONDecodeError:
            pass
    return response.text


def handle_response(response: httpx.Response, raw: bool = False, json_output: bool = False) -> None:
    """Parse and display an API response."""
    status = response.status_code
    if 200 <= status < 300:
        status_style = "green"
    elif 300 <= status < 400:
        status_style = "yellow"
    elif 400 <= status < 500:
        status_style = "red"
    else:
        status_style = "bold red"

    # Use stderr for status line when output needs to be machine-parseable
    status_console = err_console if (raw or json_output) else console

    if raw:
        print(response.text)
        status_console.print(
            f"[{status_style}]{status}[/{status_style}] [{status_style}]{response.reason_phrase}[/{status_style}]",
            style="dim",
        )
        return

    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = response.json()
        except json.JSONDecodeError:
            # Content-Type says JSON but body isn't valid JSON — show as-is
            if json_output:
                print(json.dumps({"body": response.text, "content_type": content_type}, indent=2))
            else:
                console.print(response.text)
            data = None

        if data is not None:
            if json_output:
                # Always output valid JSON to stdout, even for errors
                print(json.dumps(data, indent=2, default=str))
            elif status >= 400:
                _display_error(data, status)
            else:
                console.print(RichJSON(json.dumps(data, default=str)))
    else:
        if json_output:
            # Wrap non-JSON text in a JSON envelope for machine consumers
            print(json.dumps({"text": response.text}, indent=2))
        else:
            console.print(response.text)

    status_console.print(
        f"[{status_style}]{status}[/{status_style}] [{status_style}]{response.reason_phrase}[/{status_style}]",
        style="dim",
    )


def _display_error(data: Any, status: int) -> None:
    """Display error response with helpful formatting."""
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or data.get("detail") or str(data)
    else:
        message = str(data)
    console.print(
        Panel(
            f"[red]{message}[/red]",
            title=f"Error {status}",
            border_style="red",
        )
    )
    if isinstance(data, dict):
        if "errors" in data and isinstance(data["errors"], list):
            for err in data["errors"]:
                console.print(f"  [dim]- {err}[/dim]")
        if "documentation_url" in data:
            console.print(f"  [dim]Docs: {data['documentation_url']}[/dim]")


# ── SSE Streaming ──────────────────────────────────────────────────────────────
def stream_sse(response: httpx.Response) -> str:
    """Stream SSE response, printing data in real-time. Returns accumulated text."""
    content_buffer = ""

    for line in response.iter_lines():
        if not line:
            continue

        if line.startswith("data: "):
            data_str = line[6:]

            # Handle OpenAI-style [DONE] signal
            if data_str.strip() == "[DONE]":
                if content_buffer:
                    print()
                return content_buffer

            try:
                data = json.loads(data_str)

                # Handle content delta (streaming tokens)
                if "delta" in data:
                    chunk = data["delta"] if isinstance(data["delta"], str) else data["delta"].get("content", "")
                    if chunk:
                        print(chunk, end="", flush=True)
                        content_buffer += chunk

                # Handle OpenAI chat completion chunks
                elif "choices" in data:
                    for choice in data["choices"]:
                        delta = choice.get("delta", {})
                        chunk = delta.get("content", "")
                        if chunk:
                            print(chunk, end="", flush=True)
                            content_buffer += chunk

                # Handle completion signal
                elif data.get("done"):
                    if content_buffer:
                        print()
                    return content_buffer

                # Handle errors
                elif "error" in data:
                    if content_buffer:
                        print()
                    err_console.print(f"[red]Error: {data['error']}[/red]")
                    return content_buffer

                # Handle status updates
                elif "status" in data:
                    status = data.get("status", "")
                    tool_name = data.get("tool_name", "")
                    if tool_name:
                        err_console.print(f"[yellow][{status}] {tool_name}[/yellow]")
                    elif status not in ("running", "complete"):
                        err_console.print(f"[dim]{status}[/dim]")

            except json.JSONDecodeError:
                continue

    if content_buffer:
        print()
    return content_buffer


# ── Commands: endpoints ────────────────────────────────────────────────────────
@app.command("endpoints")
def cmd_endpoints(
    tag: Annotated[Optional[str], typer.Option("--tag", "-t", help="Filter by tag")] = None,
    search: Annotated[
        Optional[str], typer.Option("--search", "-s", help="Search paths, summaries, and operationIds")
    ] = None,
    show_deprecated: Annotated[bool, typer.Option("--deprecated", help="Include deprecated endpoints")] = False,
    output_format: Annotated[str, typer.Option("--format", "-f", help="Output format: table, json, compact")] = "table",
    refresh: Annotated[bool, typer.Option("--refresh", "-r", help="Force refresh the cached spec")] = False,
) -> None:
    """List available API endpoints from the OpenAPI spec."""
    profile_name, profile = get_active_profile()
    spec = fetch_spec(profile, refresh=refresh)
    eps = extract_endpoint_summaries(spec)

    # Filters
    if not show_deprecated:
        eps = [e for e in eps if not e.get("deprecated")]
    if tag:
        tag_lower = tag.lower()
        eps = [e for e in eps if any(tag_lower in t.lower() for t in e.get("tags", []))]
    if search:
        search_lower = search.lower()
        eps = [
            e
            for e in eps
            if (
                search_lower in e["path"].lower()
                or search_lower in e.get("summary", "").lower()
                or search_lower in e.get("operationId", "").lower()
            )
        ]

    if not eps:
        console.print("[dim]No endpoints found.[/dim]")
        if search:
            err_console.print("[yellow]Tip: Try a different search term or use --tag to filter by category.[/yellow]")
        return

    # Sort
    eps.sort(key=lambda x: (x["path"], x["method"]))

    if output_format == "json":
        print(json.dumps(eps, indent=2))
    elif output_format == "compact":
        for ep in eps:
            color = METHOD_COLORS.get(ep["method"], "white")
            console.print(f"[{color}]{ep['method']:7s}[/{color}] {ep['path']}  [dim]{ep.get('summary', '')}[/dim]")
        err_console.print(f"[dim]{len(eps)} endpoint(s)[/dim]")
    else:
        # Table format
        table = Table(title=f"Endpoints ({profile_name}) — {len(eps)} found")
        table.add_column("Method", style="bold", width=8)
        table.add_column("Path", style="cyan")
        table.add_column("Summary", max_width=50)
        table.add_column("Tags", style="dim")

        for ep in eps:
            color = METHOD_COLORS.get(ep["method"], "white")
            tags = ", ".join(ep.get("tags", []))
            table.add_row(
                f"[{color}]{ep['method']}[/{color}]",
                ep["path"],
                ep.get("summary", ""),
                tags,
            )
        console.print(table)
        err_console.print(f"[dim]{len(eps)} endpoint(s)[/dim]")


# ── Commands: call ─────────────────────────────────────────────────────────────
@app.command("call")
def cmd_call(
    method: Annotated[str, typer.Argument(help="HTTP method (GET, POST, PUT, PATCH, DELETE)")],
    path: Annotated[str, typer.Argument(help="API path (e.g., /pet/findByStatus)")],
    body: Annotated[
        Optional[str], typer.Option("--body", "-b", help="Request body (JSON string or @file.json)")
    ] = None,
    query: Annotated[
        Optional[list[str]], typer.Option("--query", "-q", help="Query params as key=value (repeatable)")
    ] = None,
    header: Annotated[
        Optional[list[str]], typer.Option("--header", "-H", help="Extra headers as Key:Value (repeatable)")
    ] = None,
    stream: Annotated[bool, typer.Option("--stream", help="Stream SSE response")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Print raw response without formatting")] = False,
    output_json_flag: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
) -> None:
    """Call any API endpoint directly.

    Examples:
        openapi-cli4ai call GET /pet/findByStatus --query status=available
        openapi-cli4ai call POST /pet --body '{"name": "Rex", "status": "available"}'
        openapi-cli4ai call POST /pet --body @payload.json
        openapi-cli4ai call GET /pet/1
    """
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        err_console.print(f"[red]Invalid HTTP method: {method}[/red]")
        raise typer.Exit(1)

    profile_name, profile = get_active_profile()

    # Parse body
    json_body = None
    if body:
        if body.startswith("@"):
            file_path = _resolve_file_path(body[1:], purpose="body")
            if not file_path.exists():
                err_console.print(f"[red]Body file not found: {file_path}[/red]")
                raise typer.Exit(1)
            try:
                json_body = json.loads(file_path.read_text())
                err_console.print(f"[dim]Body loaded from {file_path}[/dim]")
            except json.JSONDecodeError as e:
                err_console.print(f"[red]Invalid JSON in {file_path}: {e}[/red]")
                raise typer.Exit(1)
        else:
            try:
                json_body = json.loads(body)
            except json.JSONDecodeError as e:
                err_console.print(f"[red]Invalid JSON body: {e}[/red]")
                raise typer.Exit(1)

    # Parse query params (key=value format)
    params = {}
    if query:
        for q in query:
            if "=" in q:
                k, v = q.split("=", 1)
                params[k] = v
            else:
                err_console.print(f"[red]Invalid query param (expected key=value): {q}[/red]")
                raise typer.Exit(1)

    # Parse extra headers
    extra_headers = {}
    if header:
        for h in header:
            if ":" in h:
                k, v = h.split(":", 1)
                extra_headers[k.strip()] = v.strip()
            else:
                err_console.print(f"[red]Invalid header (expected Key:Value): {h}[/red]")
                raise typer.Exit(1)

    # Build URL
    base_url = profile["base_url"].rstrip("/")
    full_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{full_path}"

    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    headers.update(extra_headers)

    _verbose(f"Headers: {_redact_headers(headers)}")
    if json_body:
        _verbose("Body: [present, redacted for safety]")

    start_time = time.perf_counter()

    try:
        with _make_client(verify=verify) as client:
            if stream:
                err_console.print(f"[dim]{method} {full_path} (streaming)...[/dim]")
                headers["Accept"] = "text/event-stream"
                with client.stream(
                    method,
                    url,
                    json=json_body,
                    params=params or None,
                    headers=headers,
                ) as response:
                    _verbose(f"Response: {response.status_code}")
                    if response.status_code >= 400:
                        response.read()
                        error_data = _safe_json_or_text(response)
                        if raw or output_json_flag:
                            if isinstance(error_data, (dict, list)):
                                print(json.dumps(error_data, indent=2))
                            elif output_json_flag:
                                print(json.dumps({"error": str(error_data)}, indent=2))
                            else:
                                print(str(error_data))
                            err_console.print(f"[red]HTTP {response.status_code}[/red]")
                        else:
                            _display_error(error_data, response.status_code)
                        raise typer.Exit(1)
                    stream_sse(response)
                elapsed = time.perf_counter() - start_time
                err_console.print(f"[dim]Completed in {elapsed:.2f}s[/dim]")
            else:
                err_console.print(f"[dim]{method} {full_path}...[/dim]")
                response = _request_with_retry(
                    client,
                    method,
                    url,
                    json=json_body,
                    params=params or None,
                    headers=headers,
                )
                elapsed = time.perf_counter() - start_time
                _verbose(f"Response: {response.status_code} {response.reason_phrase}")
                _verbose(f"Response headers: {_redact_headers(dict(response.headers))}")
                handle_response(response, raw=raw, json_output=output_json_flag)
                err_console.print(f"[dim]{elapsed:.2f}s[/dim]")
                if response.status_code >= 400:
                    raise typer.Exit(1)
    except httpx.HTTPError as e:
        err_console.print(f"[red]Request failed: {e}[/red]")
        raise typer.Exit(1)


# ── Commands: run ──────────────────────────────────────────────────────────────
def _route_inputs(input_data: dict, parameters: list, has_request_body: bool) -> tuple[dict, dict, dict, dict | None]:
    """Route flat input keys to path params, query params, headers, and body.

    Returns (path_params, query_params, header_params, body).
    """
    path_params = {}
    query_params = {}
    header_params = {}
    body_keys = {}

    # Build a lookup of parameter names to their location
    param_map: dict[str, str] = {}
    for p in parameters:
        if isinstance(p, dict) and "name" in p:
            param_map[p["name"]] = p.get("in", "query")

    for key, value in input_data.items():
        location = param_map.get(key)
        if location == "path":
            path_params[key] = value
        elif location == "query":
            query_params[key] = value
        elif location == "header":
            header_params[key] = value
        elif location == "cookie":
            # Cookie params are set via Cookie header
            existing = header_params.get("Cookie", "")
            header_params["Cookie"] = f"{existing}; {key}={value}".lstrip("; ")
        elif location:
            # Other parameter locations — treat as query
            query_params[key] = value
        else:
            # Not a declared parameter — goes into request body
            body_keys[key] = value

    body = body_keys if body_keys else None
    # If there's a requestBody defined but no body keys collected,
    # and the entire input looks like it could be the body, send it all
    if has_request_body and not body and not param_map:
        body = input_data

    return path_params, query_params, header_params, body


@app.command("run")
def cmd_run(
    operation: Annotated[
        str, typer.Argument(help="Operation ID from the OpenAPI spec (e.g., findPetsByStatus, addPet)")
    ],
    input_data: Annotated[
        Optional[str], typer.Option("--input", "-i", help="Input as JSON (keys auto-routed to path/query/body)")
    ] = None,
    input_file: Annotated[Optional[str], typer.Option("--input-file", "-f", help="Read input from a JSON file")] = None,
    stream: Annotated[bool, typer.Option("--stream", help="Stream SSE response")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Print raw response without formatting")] = False,
    output_json_flag: Annotated[bool, typer.Option("--json", help="Output raw JSON")] = False,
) -> None:
    """Run an API operation by name. Inputs are auto-routed to the right place.

    The spec defines where each parameter goes (path, query, header, body).
    You just pass a flat JSON object and the tool figures it out.

    Examples:
        openapi-cli4ai run findPetsByStatus --input '{"status": "available"}'
        openapi-cli4ai run getPetById --input '{"petId": 123}'
        openapi-cli4ai run addPet --input '{"name": "Rex", "status": "available"}'
        openapi-cli4ai run addPet --input-file pet.json
    """
    profile_name, profile = get_active_profile()
    spec = fetch_spec(profile)

    # Look up the operation in the spec
    endpoint = extract_full_endpoint_schema(spec, operation)
    if not endpoint:
        # Try case-insensitive fuzzy match
        all_eps = extract_endpoint_summaries(spec)
        matches = [e for e in all_eps if e["operationId"].lower() == operation.lower()]
        if matches:
            endpoint = extract_full_endpoint_schema(spec, matches[0]["operationId"])

    if not endpoint:
        err_console.print(f"[red]Operation '{operation}' not found in spec.[/red]")
        # Suggest similar operations
        all_eps = extract_endpoint_summaries(spec)
        op_lower = operation.lower()
        suggestions = [e["operationId"] for e in all_eps if op_lower in e["operationId"].lower()][:5]
        if suggestions:
            err_console.print("[dim]Did you mean:[/dim]")
            for s in suggestions:
                err_console.print(f"  [cyan]{s}[/cyan]")
        raise typer.Exit(1)

    # Parse input
    parsed_input: dict = {}
    json_body = None  # Set directly for array inputs, otherwise set by _route_inputs
    if input_file:
        fp = _resolve_file_path(input_file, purpose="input")
        if not fp.exists():
            err_console.print(f"[red]Input file not found: {input_file}[/red]")
            raise typer.Exit(1)
        try:
            raw_input = json.loads(fp.read_text())
        except json.JSONDecodeError as e:
            err_console.print(f"[red]Invalid JSON in {input_file}: {e}[/red]")
            raise typer.Exit(1)
        if isinstance(raw_input, dict):
            parsed_input = raw_input
        elif isinstance(raw_input, list):
            parsed_input = {}
            json_body = raw_input
        else:
            err_console.print("[red]Input must be a JSON object or array, not a scalar.[/red]")
            raise typer.Exit(1)
    elif input_data:
        try:
            raw_input = json.loads(input_data)
        except json.JSONDecodeError as e:
            err_console.print(f"[red]Invalid JSON input: {e}[/red]")
            raise typer.Exit(1)
        if isinstance(raw_input, dict):
            parsed_input = raw_input
        elif isinstance(raw_input, list):
            parsed_input = {}
            json_body = raw_input
        else:
            err_console.print("[red]Input must be a JSON object or array, not a scalar.[/red]")
            raise typer.Exit(1)

    # Route inputs to the right places
    method = endpoint["method"]
    path_template = endpoint["path"]
    parameters = endpoint.get("parameters", [])
    has_request_body = endpoint.get("requestBody") is not None

    # If json_body was already set (array input), validate and skip parameter routing
    if json_body is not None and not has_request_body:
        err_console.print("[red]This operation does not accept a request body. Array input is not valid here.[/red]")
        raise typer.Exit(1)
    if json_body is not None:
        path_params, query_params, header_params = {}, {}, {}
        unsupplied = [
            f"{p['name']} ({p.get('in', '?')})"
            for p in parameters
            if isinstance(p, dict) and p.get("in") in ("path", "query", "header", "cookie")
        ]
        if unsupplied:
            err_console.print(
                f"[yellow]Warning: Array body input cannot supply parameters: {', '.join(unsupplied)}[/yellow]"
            )
    else:
        path_params, query_params, header_params, json_body = _route_inputs(parsed_input, parameters, has_request_body)

    # Substitute path parameters (URL-encode values for safety)
    full_path = path_template
    for key, value in path_params.items():
        full_path = full_path.replace(f"{{{key}}}", urllib.parse.quote(str(value), safe=","))

    # Check for unresolved path params
    if "{" in full_path:
        missing = re.findall(r"\{(\w+)\}", full_path)
        err_console.print(f"[red]Missing required path parameter(s): {', '.join(missing)}[/red]")
        err_console.print(f'[dim]Provide them in --input, e.g. --input \'{{"{missing[0]}": "value"}}\'[/dim]')
        raise typer.Exit(1)

    # Build URL and make request
    base_url = profile["base_url"].rstrip("/")
    url = f"{base_url}{full_path}"

    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    # Merge header_params, appending Cookie values instead of overwriting
    # Check case-insensitively since profiles may use "cookie" or "Cookie"
    for hk, hv in header_params.items():
        if hk.lower() == "cookie":
            existing_key = next((k for k in headers if k.lower() == "cookie"), None)
            if existing_key:
                headers[existing_key] = f"{headers[existing_key]}; {hv}"
            else:
                headers[hk] = hv
        else:
            headers[hk] = hv

    _verbose(f"Headers: {_redact_headers(headers)}")

    start_time = time.perf_counter()
    err_console.print(f"[dim]{method} {full_path}...[/dim]")

    try:
        with _make_client(verify=verify) as client:
            if stream:
                headers["Accept"] = "text/event-stream"
                with client.stream(
                    method,
                    url,
                    json=json_body,
                    params=query_params or None,
                    headers=headers,
                ) as response:
                    _verbose(f"Response: {response.status_code}")
                    if response.status_code >= 400:
                        response.read()
                        error_data = _safe_json_or_text(response)
                        if raw or output_json_flag:
                            if isinstance(error_data, (dict, list)):
                                print(json.dumps(error_data, indent=2))
                            elif output_json_flag:
                                print(json.dumps({"error": str(error_data)}, indent=2))
                            else:
                                print(str(error_data))
                            err_console.print(f"[red]HTTP {response.status_code}[/red]")
                        else:
                            _display_error(error_data, response.status_code)
                        raise typer.Exit(1)
                    stream_sse(response)
                elapsed = time.perf_counter() - start_time
                err_console.print(f"[dim]Completed in {elapsed:.2f}s[/dim]")
            else:
                response = _request_with_retry(
                    client,
                    method,
                    url,
                    json=json_body,
                    params=query_params or None,
                    headers=headers,
                )
                elapsed = time.perf_counter() - start_time
                handle_response(response, raw=raw, json_output=output_json_flag)
                err_console.print(f"[dim]{elapsed:.2f}s[/dim]")
                if response.status_code >= 400:
                    raise typer.Exit(1)
    except httpx.HTTPError as e:
        err_console.print(f"[red]Request failed: {e}[/red]")
        raise typer.Exit(1)


# ── Commands: init ─────────────────────────────────────────────────────────────
@app.command("init")
def cmd_init(
    name: Annotated[str, typer.Argument(help="Profile name (e.g., petstore, myapp)")],
    url: Annotated[str, typer.Option("--url", "-u", help="Base URL of the API")] = "",
    spec_path: Annotated[
        Optional[str], typer.Option("--spec", "-s", help="Path to OpenAPI spec (auto-detected if omitted)")
    ] = None,
    spec_url: Annotated[Optional[str], typer.Option("--spec-url", help="Full URL to OpenAPI spec file")] = None,
    auth_type: Annotated[
        str, typer.Option("--auth", help="Auth type: bearer, oidc, device, auto, api-key, basic, none")
    ] = "none",
    # Non-interactive auth flags
    issuer_url: Annotated[Optional[str], typer.Option("--issuer-url", help="OIDC issuer URL for discovery")] = None,
    client_id: Annotated[Optional[str], typer.Option("--client-id", help="OAuth client ID")] = None,
    scopes: Annotated[Optional[str], typer.Option("--scopes", help="OAuth scopes")] = None,
    device_config_url: Annotated[
        Optional[str], typer.Option("--device-config-url", help="Device flow config discovery URL")
    ] = None,
    authorize_url: Annotated[
        Optional[str], typer.Option("--authorize-url", help="OIDC authorization endpoint URL")
    ] = None,
    token_url: Annotated[Optional[str], typer.Option("--token-url", help="OIDC token endpoint URL")] = None,
    token_exchange_endpoint: Annotated[
        Optional[str], typer.Option("--token-exchange-endpoint", help="Token exchange endpoint path for two-phase auth")
    ] = None,
) -> None:
    """Initialize a new API profile with guided setup.

    Fetches the OpenAPI spec, validates it, and creates a profile.

    Examples:
        openapi-cli4ai init petstore --url https://petstore3.swagger.io/api/v3
        openapi-cli4ai init myapp --url http://localhost:8000 --auth bearer
        openapi-cli4ai init myapp --url http://localhost:8000 --auth device --issuer-url https://auth.example.com/realms/myrealm --client-id my-cli
        openapi-cli4ai init myapp --url http://localhost:8000 --auth oidc --authorize-url https://auth.example.com/authorize --token-url https://auth.example.com/token --client-id my-client
    """
    if not url:
        url = typer.prompt("Base URL of the API")

    # Auto-prepend http:// if no scheme provided
    if not url.startswith(("http://", "https://")):
        # urlparse misparses "host:port" as scheme="host", so check for
        # real URL schemes (contain "://") vs bare host:port patterns
        if "://" in url:
            scheme = url.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                err_console.print(
                    f"[red]Unsupported URL scheme '{scheme}://'. Only http:// and https:// are supported.[/red]"
                )
                raise typer.Exit(1)
        else:
            url = f"http://{url}"
            err_console.print(f"[dim]No scheme provided — using {url}[/dim]")

    url = url.rstrip("/")

    # Check if profile already exists
    data = load_profiles()
    if name in data.get("profiles", {}):
        if not typer.confirm(f"Profile '{name}' already exists. Overwrite?"):
            err_console.print("[yellow]Cancelled.[/yellow]")
            raise typer.Exit(0)

    # Build profile
    profile: dict = {
        "base_url": url,
        "auth": {"type": auth_type},
        "verify_ssl": True,
    }

    if spec_url:
        profile["openapi_url"] = spec_url
    elif spec_path:
        profile["openapi_path"] = spec_path

    # Auto-detect spec if no path/url given
    resolved_spec_path = None
    if not spec_url and not spec_path:
        console.print("[dim]Auto-detecting OpenAPI spec location...[/dim]")
        with _make_client(verify=get_verify_ssl()) as client:
            for try_path in COMMON_SPEC_PATHS:
                try:
                    resp = client.get(f"{url}{try_path}")
                    if resp.status_code == 200:
                        # Verify it's actually a spec
                        content_type = resp.headers.get("content-type", "")
                        if "json" in content_type or "yaml" in content_type:
                            try:
                                spec_data = resp.json() if "json" in content_type else yaml.safe_load(resp.text)
                                if isinstance(spec_data, dict) and ("openapi" in spec_data or "swagger" in spec_data):
                                    resolved_spec_path = try_path
                                    console.print(f"[green]Found spec at {try_path}[/green]")
                                    break
                            except (json.JSONDecodeError, yaml.YAMLError):
                                continue
                except (httpx.HTTPError, OSError):
                    continue

        if resolved_spec_path:
            profile["openapi_path"] = resolved_spec_path
        else:
            err_console.print("[yellow]Could not auto-detect spec location.[/yellow]")
            manual_path = typer.prompt("OpenAPI spec path", default="/openapi.json")
            profile["openapi_path"] = manual_path

    # Auth setup
    if auth_type == "bearer":
        bearer_mode = typer.prompt("Static token or login endpoint?", type=str, default="login")
        if bearer_mode.lower().startswith("s"):
            # Static token from env var
            env_var = typer.prompt("Environment variable for token", default=f"{name.upper()}_TOKEN")
            profile["auth"]["token_env_var"] = env_var
            err_console.print(f"[dim]Set {env_var} in your environment or add it to a .env file.[/dim]")
        else:
            # Token endpoint (username/password login)
            token_ep = typer.prompt("Token endpoint path", default="/api/auth/token")
            profile["auth"]["token_endpoint"] = token_ep
            refresh_ep = typer.prompt("Refresh endpoint path (leave blank to skip)", default="")
            if refresh_ep:
                profile["auth"]["refresh_endpoint"] = refresh_ep
            profile["auth"]["payload"] = {
                "username": "{username}",
                "password": "{password}",
            }
            console.print("[dim]Run 'openapi-cli4ai login --username <user>' to authenticate.[/dim]")
    elif auth_type == "oidc":
        _init_oidc_auth(profile, authorize_url, token_url, client_id, scopes, issuer_url, token_exchange_endpoint)
        console.print("[dim]Run 'openapi-cli4ai login' to authenticate via browser.[/dim]")
    elif auth_type == "device":
        _init_device_auth(profile, device_config_url, issuer_url, client_id, scopes, token_exchange_endpoint)
        console.print("[dim]Run 'openapi-cli4ai login' to authenticate.[/dim]")
    elif auth_type == "auto":
        _init_auto_auth(profile, issuer_url, client_id, scopes, token_exchange_endpoint)
        console.print("[dim]Run 'openapi-cli4ai login' to authenticate (flow auto-detected).[/dim]")
    elif auth_type == "api-key":
        env_var = typer.prompt("Environment variable for API key", default=f"{name.upper()}_API_KEY")
        header_name = typer.prompt("Header name", default="Authorization")
        prefix = typer.prompt("Header value prefix", default="Bearer ")
        profile["auth"]["env_var"] = env_var
        profile["auth"]["header"] = header_name
        profile["auth"]["prefix"] = prefix
        console.print(f"[dim]Set {env_var} in your environment or add it to a .env file.[/dim]")
    elif auth_type == "basic":
        user_var = typer.prompt("Environment variable for username", default=f"{name.upper()}_USER")
        pass_var = typer.prompt("Environment variable for password", default=f"{name.upper()}_PASS")
        profile["auth"]["username_env_var"] = user_var
        profile["auth"]["password_env_var"] = pass_var

    # Try fetching and validating spec
    console.print("[dim]Fetching spec...[/dim]")
    profile["_name"] = name  # Needed for fetch_spec
    try:
        spec = fetch_spec(profile)
        endpoints = extract_endpoint_summaries(spec)
        spec_title = spec.get("info", {}).get("title", "Unknown")
        spec_version = spec.get("info", {}).get("version", "?")
        openapi_version = spec.get("openapi", spec.get("swagger", "?"))
        console.print(
            Panel(
                f"[cyan]Title:[/cyan] {spec_title}\n"
                f"[cyan]Version:[/cyan] {spec_version}\n"
                f"[cyan]OpenAPI:[/cyan] {openapi_version}\n"
                f"[cyan]Endpoints:[/cyan] {len(endpoints)}",
                title="Spec Validated",
                border_style="green",
            )
        )
    except (typer.Exit, httpx.HTTPError, json.JSONDecodeError, yaml.YAMLError, OSError, ValueError, TypeError, AttributeError) as e:
        err_console.print(f"[yellow]Warning: Could not validate spec ({e}). Profile saved anyway.[/yellow]")

    # Save profile
    del profile["_name"]
    data.setdefault("profiles", {})[name] = profile
    data["active_profile"] = name
    save_profiles(data)

    console.print(f"\n[green]Profile '{name}' created and set as active.[/green]")
    err_console.print(f"[dim]Config saved to {CONFIG_FILE}[/dim]")
    console.print("\n[bold]Next steps:[/bold]")
    console.print("  openapi-cli4ai endpoints           [dim]# List available endpoints[/dim]")
    console.print("  openapi-cli4ai endpoints -s keyword [dim]# Search endpoints[/dim]")
    console.print("  openapi-cli4ai call GET /path       [dim]# Call an endpoint[/dim]")


def _init_oidc_auth(
    profile: dict,
    authorize_url: str | None,
    token_url: str | None,
    client_id: str | None,
    scopes: str | None,
    issuer_url: str | None,
    token_exchange_endpoint: str | None,
) -> None:
    """Set up OIDC auth config in profile, prompting only for missing values."""
    if issuer_url:
        profile["auth"]["issuer_url"] = issuer_url
    if not authorize_url:
        authorize_url = typer.prompt("Authorization URL (full URL)")
    if not token_url:
        token_url = typer.prompt("Token URL (full URL)")
    if not client_id:
        client_id = typer.prompt("Client ID")
    if not scopes:
        scopes = typer.prompt("Scopes", default="openid")
    redirect_uri_val = typer.prompt("Redirect URI (or leave blank for localhost callback)", default="")
    if redirect_uri_val:
        profile["auth"]["redirect_uri"] = redirect_uri_val
    else:
        cb_port = typer.prompt("Local callback port", default="8484")
        profile["auth"]["callback_port"] = int(cb_port)
    profile["auth"]["authorize_url"] = authorize_url
    profile["auth"]["token_url"] = token_url
    profile["auth"]["client_id"] = client_id
    profile["auth"]["scopes"] = scopes
    if token_exchange_endpoint:
        profile["auth"]["token_exchange_endpoint"] = token_exchange_endpoint


def _init_device_auth(
    profile: dict,
    device_config_url: str | None,
    issuer_url: str | None,
    client_id: str | None,
    scopes: str | None,
    token_exchange_endpoint: str | None,
) -> None:
    """Set up device flow auth config in profile, prompting only for missing values."""
    if device_config_url:
        profile["auth"]["device_config_url"] = device_config_url
    elif issuer_url:
        profile["auth"]["issuer_url"] = issuer_url
    else:
        use_discovery = typer.confirm("Use a device-config discovery URL?", default=False)
        if use_discovery:
            profile["auth"]["device_config_url"] = typer.prompt("Device config URL")
        else:
            use_issuer = typer.confirm("Use issuer URL for OIDC discovery?", default=True)
            if use_issuer:
                profile["auth"]["issuer_url"] = typer.prompt("Issuer URL (e.g., https://accounts.google.com)")
            else:
                if not client_id:
                    client_id = typer.prompt("OAuth Client ID")
                device_ep = typer.prompt("Device authorization endpoint URL")
                token_ep = typer.prompt("Token endpoint URL")
                profile["auth"]["device_authorization_endpoint"] = device_ep
                profile["auth"]["token_endpoint"] = token_ep

    if not client_id and "client_id" not in profile["auth"]:
        client_id = typer.prompt("OAuth Client ID")
    if client_id:
        profile["auth"]["client_id"] = client_id
    if scopes:
        profile["auth"]["scopes"] = scopes
    if token_exchange_endpoint:
        profile["auth"]["token_exchange_endpoint"] = token_exchange_endpoint


def _init_auto_auth(
    profile: dict,
    issuer_url: str | None,
    client_id: str | None,
    scopes: str | None,
    token_exchange_endpoint: str | None,
) -> None:
    """Set up auto-detect auth config in profile, prompting only for missing values."""
    if not issuer_url:
        issuer_url = typer.prompt("Issuer URL (e.g., https://accounts.google.com)")
    profile["auth"]["issuer_url"] = issuer_url
    if not client_id:
        client_id = typer.prompt("OAuth Client ID")
    profile["auth"]["client_id"] = client_id
    if scopes:
        profile["auth"]["scopes"] = scopes
    if token_exchange_endpoint:
        profile["auth"]["token_exchange_endpoint"] = token_exchange_endpoint


# ── Commands: login ────────────────────────────────────────────────────────────
@app.command("login")
def cmd_login(
    username: Annotated[str, typer.Option("--username", "-u", help="Username or email")] = "",
    password: Annotated[
        str, typer.Option("--password", "-p", help="Password (avoid for special chars — use interactive prompt)")
    ] = "",
    password_file: Annotated[Optional[str], typer.Option("--password-file", help="Read password from file")] = None,
    password_stdin: Annotated[bool, typer.Option("--password-stdin", help="Read password from stdin")] = False,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="OIDC/device: print login URL instead of opening browser (for headless/SSH)"),
    ] = False,
    access_token: Annotated[str, typer.Option("--access-token", help="Inject a pre-obtained access token")] = "",
    refresh_token: Annotated[str, typer.Option("--refresh-token", help="Inject a pre-obtained refresh token")] = "",
    access_token_stdin: Annotated[
        bool, typer.Option("--access-token-stdin", help="Read access token from stdin")
    ] = False,
) -> None:
    """Login to an API that uses OAuth/token-endpoint authentication.

    Supports auth modes:
        - auth.type=bearer with token_endpoint: username/password grant
        - auth.type=oidc: Authorization Code + PKCE flow (browser or --no-browser)
        - auth.type=device: OAuth 2.0 Device Authorization Grant (RFC 8628)
        - auth.type=auto: auto-detect best flow from OIDC discovery
        - --access-token / --access-token-stdin: inject a pre-obtained token

    For OIDC/device, use --no-browser on headless machines.

    Password input methods (bearer mode, in priority order):
        1. --password-file /path/to/file
        2. --password-stdin (piped input)
        3. --password flag (avoid for special characters)
        4. Interactive prompt (most secure)
    """
    profile_name, profile = get_active_profile()
    auth_config = profile.get("auth", {})
    auth_type = auth_config.get("type", "none")

    # Token injection — works with any auth type
    if access_token or access_token_stdin:
        _inject_token(profile_name, access_token, refresh_token, access_token_stdin)
        return

    # Auto-detect flow from OIDC discovery
    if auth_type == "auto":
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        resolved_type = _auto_detect_flow(auth_config, verify=verify)
        auth_type = resolved_type

    # OIDC flow
    if auth_type == "oidc":
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        base_url = profile.get("base_url", "").rstrip("/")
        _oidc_login(auth_config, profile_name, no_browser=no_browser, verify=verify, base_url=base_url)
        _try_post_login_spec_fetch(profile)
        return

    # Device flow
    if auth_type == "device":
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        _device_login(auth_config, profile_name, profile, no_browser=no_browser, verify=verify)
        _try_post_login_spec_fetch(profile)
        return

    if auth_type != "bearer" or not auth_config.get("token_endpoint"):
        err_console.print(
            "[yellow]Login is for profiles with bearer auth + token_endpoint, oidc, device, or auto.[/yellow]"
        )
        err_console.print("[dim]If your API uses a static token or API key, set the environment variable instead.[/dim]")
        raise typer.Exit(1)

    # Build payload from config ({env:VAR} already resolved at profile load)
    payload_template = dict(auth_config.get("payload", {"username": "{username}", "password": "{password}"}))

    # Check if payload still needs {username} or {password} substitution
    raw_payload = json.dumps(payload_template)
    needs_username = "{username}" in raw_payload
    needs_password = "{password}" in raw_payload

    # Only prompt/resolve credentials if the payload actually needs them
    resolved_password = ""
    if needs_password:
        if password_file:
            pf = _resolve_file_path(password_file, purpose="password")
            if not pf.exists():
                err_console.print(f"[red]Password file not found: {password_file}[/red]")
                raise typer.Exit(1)
            resolved_password = pf.read_text().rstrip("\n\r")
        elif password_stdin:
            if sys.stdin.isatty():
                err_console.print("[red]--password-stdin requires piped input.[/red]")
                raise typer.Exit(1)
            resolved_password = sys.stdin.read().rstrip("\n\r")
        elif password:
            resolved_password = password
        else:
            resolved_password = typer.prompt("Password", hide_input=True)

    if needs_username and not username:
        username = typer.prompt("Username")

    # Build payload safely — substitute placeholders recursively,
    # using value assignment (not string interpolation) to prevent JSON injection
    def _substitute_placeholders(obj: Any) -> Any:
        if isinstance(obj, str):
            if obj == "{username}":
                return username
            if obj == "{password}":
                return resolved_password
            return obj.replace("{username}", username).replace("{password}", resolved_password)
        if isinstance(obj, dict):
            return {k: _substitute_placeholders(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_substitute_placeholders(item) for item in obj]
        return obj

    payload = _substitute_placeholders(payload_template)

    # Make token request
    base_url = profile["base_url"].rstrip("/")
    token_endpoint = auth_config["token_endpoint"]
    token_url = f"{base_url}{token_endpoint}"
    verify = profile.get("verify_ssl", True) and get_verify_ssl()

    try:
        with _make_client(verify=verify, follow_redirects=False) as client:
            resp = client.post(token_url, json=payload)

        if resp.status_code != 200:
            _display_error(
                _safe_json_or_text(resp),
                resp.status_code,
            )
            raise typer.Exit(1)

        token_data = resp.json()
        if "expires_in" in token_data:
            try:
                token_data["expires_at"] = time.time() + float(token_data["expires_in"])
            except (TypeError, ValueError):
                token_data["expires_at"] = time.time() + 86400
        elif "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + 86400  # 24h default

        # Cache token
        token_cache = _save_token(profile_name, token_data)

        console.print("[green]Logged in successfully![/green]")
        err_console.print(f"[dim]Token cached at {token_cache}[/dim]")

        _try_post_login_spec_fetch(profile)

    except httpx.HTTPError as e:
        err_console.print(f"[red]Login failed: {e}[/red]")
        raise typer.Exit(1)


def _inject_token(profile_name: str, access_token: str, refresh_token: str, from_stdin: bool) -> None:
    """Inject a pre-obtained token into the token cache."""
    if from_stdin:
        if sys.stdin.isatty():
            err_console.print("[red]--access-token-stdin requires piped input.[/red]")
            raise typer.Exit(1)
        access_token = sys.stdin.read().strip()

    if not access_token:
        err_console.print("[red]No access token provided.[/red]")
        raise typer.Exit(1)

    token_data: dict = {"access_token": access_token}
    if refresh_token:
        token_data["refresh_token"] = refresh_token

    # Try to extract exp from JWT payload (without validation)
    try:
        payload_b64 = access_token.split(".")[1]
        # Add padding
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if "exp" in payload:
            token_data["expires_at"] = payload["exp"]
    except (IndexError, ValueError, json.JSONDecodeError, binascii.Error):
        pass

    if "expires_at" not in token_data:
        token_data["expires_at"] = time.time() + 3600

    token_cache = _save_token(profile_name, token_data)

    console.print("[green]Token injected successfully![/green]")
    err_console.print(f"[dim]Token cached at {token_cache}[/dim]")


def _auto_detect_flow(auth_config: dict, verify: bool = True) -> str:
    """Auto-detect the best OAuth flow from OIDC discovery.

    Returns 'device' if device_authorization_endpoint is available, else 'oidc'.
    """
    issuer_url = auth_config.get("issuer_url")
    if not issuer_url:
        err_console.print("[red]Auto auth type requires issuer_url.[/red]")
        raise typer.Exit(1)

    well_known_url = f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
    try:
        with _make_client(verify=verify) as client:
            resp = client.get(well_known_url)
        if resp.status_code != 200:
            err_console.print(f"[red]Failed to fetch OIDC discovery ({resp.status_code})[/red]")
            raise typer.Exit(1)

        oidc_config = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        err_console.print(f"[red]Failed to fetch OIDC discovery: {e}[/red]")
        raise typer.Exit(1)

    # Check if device flow is supported
    grant_types = oidc_config.get("grant_types_supported", [])
    device_ep = oidc_config.get("device_authorization_endpoint")

    if device_ep and "urn:ietf:params:oauth:grant-type:device_code" in grant_types:
        # Populate auth_config with discovered endpoints for device flow
        auth_config.setdefault("device_authorization_endpoint", device_ep)
        auth_config.setdefault("token_endpoint", oidc_config.get("token_endpoint", ""))
        err_console.print("[dim]Auto-detected: using device authorization flow[/dim]")
        return "device"

    # Fall back to PKCE
    auth_config.setdefault("authorize_url", oidc_config.get("authorization_endpoint", ""))
    auth_config.setdefault("token_url", oidc_config.get("token_endpoint", ""))
    err_console.print("[dim]Auto-detected: using Authorization Code + PKCE flow[/dim]")
    return "oidc"


def _try_post_login_spec_fetch(profile: dict) -> None:
    """Best-effort spec fetch after successful login."""
    try:
        spec = fetch_spec(profile, refresh=True)
        endpoints = extract_endpoint_summaries(spec)
        spec_title = spec.get("info", {}).get("title", "Unknown")
        console.print(f"[green]Fetched spec: {spec_title} ({len(endpoints)} endpoints)[/green]")
    except (typer.Exit, httpx.HTTPError, json.JSONDecodeError, KeyError, OSError, TypeError, ValueError, AttributeError):
        pass


@app.command("logout")
def cmd_logout() -> None:
    """Clear cached authentication tokens for the active profile."""
    profile_name, profile = get_active_profile()
    token_cache = CACHE_DIR / f"{_safe_profile_name(profile_name)}_token.json"
    if token_cache.exists():
        token_cache.unlink()
        console.print(f"[green]Logged out from '{profile_name}'.[/green]")
    else:
        console.print(f"[dim]No cached token for '{profile_name}'.[/dim]")


# ── Commands: profile ──────────────────────────────────────────────────────────
@profile_app.command("add")
def cmd_profile_add(
    name: Annotated[str, typer.Argument(help="Profile name")],
    url: Annotated[str, typer.Option("--url", "-u", help="Base URL of the API")] = "",
    spec_path: Annotated[str, typer.Option("--spec", "-s", help="Path to OpenAPI spec")] = "/openapi.json",
    auth_type: Annotated[str, typer.Option("--auth", help="Auth type: bearer, oidc, api-key, basic, none")] = "none",
) -> None:
    """Add a new API profile."""
    if not url:
        url = typer.prompt("Base URL")

    data = load_profiles()
    if name in data.get("profiles", {}):
        if not typer.confirm(f"Profile '{name}' exists. Overwrite?"):
            raise typer.Exit(0)

    data.setdefault("profiles", {})[name] = {
        "base_url": url.rstrip("/"),
        "openapi_path": spec_path,
        "auth": {"type": auth_type},
        "verify_ssl": True,
    }

    if not data.get("active_profile"):
        data["active_profile"] = name

    save_profiles(data)
    console.print(f"[green]Profile '{name}' added.[/green]")


@profile_app.command("list")
def cmd_profile_list() -> None:
    """List all configured profiles."""
    data = load_profiles()
    profiles = data.get("profiles", {})
    active = data.get("active_profile")

    if not profiles:
        console.print("[dim]No profiles configured. Run 'openapi-cli4ai init' to create one.[/dim]")
        return

    table = Table(title="Profiles")
    table.add_column("", width=2)
    table.add_column("Name", style="cyan")
    table.add_column("Base URL", style="green")
    table.add_column("Auth", style="yellow")

    for name, prof in profiles.items():
        marker = "*" if name == active else ""
        auth_type = prof.get("auth", {}).get("type", "none")
        base_url = _resolve_env_vars(prof.get("base_url", ""))
        table.add_row(marker, name, base_url, auth_type)

    console.print(table)
    console.print("[dim]* = active profile[/dim]")


@profile_app.command("use")
def cmd_profile_use(
    name: Annotated[str, typer.Argument(help="Profile name to activate")],
) -> None:
    """Set the active profile."""
    data = load_profiles()
    if name not in data.get("profiles", {}):
        err_console.print(f"[red]Profile '{name}' not found.[/red]")
        raise typer.Exit(1)
    data["active_profile"] = name
    save_profiles(data)
    console.print(f"[green]Active profile set to '{name}'.[/green]")


@profile_app.command("remove")
def cmd_profile_remove(
    name: Annotated[str, typer.Argument(help="Profile name to remove")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Remove a profile."""
    data = load_profiles()
    if name not in data.get("profiles", {}):
        err_console.print(f"[red]Profile '{name}' not found.[/red]")
        raise typer.Exit(1)

    if not force and not typer.confirm(f"Remove profile '{name}'?"):
        raise typer.Exit(0)

    # Resolve spec cache paths before deleting the profile
    profile = data["profiles"][name]
    try:
        profile["_name"] = name
        spec_url = _resolve_spec_url(profile)
        spec_cache, spec_meta = _spec_cache_paths(spec_url)
    except (KeyError, TypeError):
        spec_cache, spec_meta = None, None

    del data["profiles"][name]
    if data.get("active_profile") == name:
        data["active_profile"] = next(iter(data["profiles"]), None)
    save_profiles(data)

    # Clean up cached token file (exact match by profile name)
    token_cache = CACHE_DIR / f"{_safe_profile_name(name)}_token.json"
    token_cache.unlink(missing_ok=True)

    # Clean up cached spec files (keyed by URL hash)
    if spec_cache and spec_cache.exists():
        spec_cache.unlink(missing_ok=True)
    if spec_meta and spec_meta.exists():
        spec_meta.unlink(missing_ok=True)

    console.print(f"[green]Profile '{name}' removed.[/green]")


@profile_app.command("show")
def cmd_profile_show(
    name: Annotated[Optional[str], typer.Argument(help="Profile name (default: active)")] = None,
) -> None:
    """Show profile configuration details."""
    data = load_profiles()
    if not name:
        name = data.get("active_profile")
    if not name or name not in data.get("profiles", {}):
        err_console.print(f"[red]Profile '{name}' not found.[/red]")
        raise typer.Exit(1)

    profile = data["profiles"][name]
    is_active = name == data.get("active_profile")

    # Display as TOML snippet
    display_data = {name: profile}
    console.print(
        Panel(
            tomli_w.dumps(display_data).strip(),
            title=f"Profile: {name}" + (" (active)" if is_active else ""),
            border_style="cyan",
        )
    )


# ── Main ───────────────────────────────────────────────────────────────────────
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version")] = False,
    insecure: Annotated[bool, typer.Option("--insecure", "-k", help="Disable SSL verification")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show request/response details")] = False,
    timeout: Annotated[float, typer.Option("--timeout", help="HTTP timeout in seconds")] = 60.0,
    retries: Annotated[int, typer.Option("--retries", help="Retry count for 429/503 responses")] = 0,
) -> None:
    """openapi-cli4ai — Interact with any REST API using natural language.

    Point it at an OpenAPI spec. Discover endpoints. Call them directly or let
    an LLM figure out the right one from your natural language query.
    """
    global _verbose_mode, _timeout_seconds, _max_retries
    set_insecure_mode(insecure)
    _verbose_mode = verbose
    _timeout_seconds = timeout
    _max_retries = retries

    if version:
        console.print(f"{APP_NAME} {VERSION}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        console.print(
            Panel(
                f"[bold]{APP_NAME}[/bold] v{VERSION}\n\n"
                "[cyan]init[/cyan]       Point it at any API with an OpenAPI spec\n"
                "[cyan]endpoints[/cyan]  Discover and search API endpoints\n"
                "[cyan]call[/cyan]       Call any endpoint directly\n"
                "[cyan]profile[/cyan]    Manage API profiles\n"
                "[cyan]login[/cyan]      Authenticate (OAuth/token flows)\n"
                "[cyan]logout[/cyan]     Clear cached auth tokens\n\n"
                "[dim]Run any command with --help for details.[/dim]",
                title="Quick Reference",
                border_style="cyan",
            )
        )


if __name__ == "__main__":
    app()
