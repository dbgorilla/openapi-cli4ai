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
import hashlib
import json
import os
import re
import secrets
import sys
import time
import tomllib
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Annotated, Optional

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
VERSION = "0.2.0"
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


def set_insecure_mode(insecure: bool) -> None:
    global _insecure_mode
    _insecure_mode = insecure


def get_verify_ssl() -> bool:
    return not _insecure_mode


# ── Directory Helpers ──────────────────────────────────────────────────────────
def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)


# ── Profile Management ────────────────────────────────────────────────────────
def load_profiles() -> dict:
    """Load profiles from TOML config file."""
    if not CONFIG_FILE.exists():
        return {"active_profile": None, "profiles": {}}
    try:
        data = tomllib.loads(CONFIG_FILE.read_text())
        if not isinstance(data, dict):
            return {"active_profile": None, "profiles": {}}
        if "profiles" not in data:
            data["profiles"] = {}
        return data
    except (tomllib.TOMLDecodeError, OSError):
        return {"active_profile": None, "profiles": {}}


def save_profiles(data: dict) -> None:
    """Save profiles to TOML config file."""
    ensure_dirs()
    # TOML doesn't support None values — filter them out before writing
    clean = {k: v for k, v in data.items() if v is not None}
    CONFIG_FILE.write_text(tomli_w.dumps(clean))


def _resolve_env_vars(obj):
    """Recursively replace {env:VAR_NAME} placeholders with environment values."""
    if isinstance(obj, str):
        for match in re.finditer(r"\{env:([^}]+)\}", obj):
            env_val = os.environ.get(match.group(1), "")
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
        console.print("[red]No profiles configured. Run 'openapi-cli4ai init' to set one up.[/red]")
        raise typer.Exit(1)

    # Check env var override
    name = os.environ.get(f"{ENV_PREFIX}PROFILE") or data.get("active_profile")

    if not name or name not in profiles:
        # Fall back to first profile
        name = next(iter(profiles))

    profile = _resolve_env_vars(profiles[name])
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
            age = time.time() - meta.get("fetched_at", 0)
            if age < CACHE_TTL:
                return json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Fetch fresh spec
    try:
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        headers = dict(profile.get("headers", {}))
        # Add auth if available
        try:
            auth_headers = get_auth_headers(profile, quiet=True)
            headers.update(auth_headers)
        except (typer.Exit, Exception):
            pass  # Auth not required for spec fetching

        with httpx.Client(verify=verify, follow_redirects=True, timeout=30.0) as client:
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

        # Write cache
        ensure_dirs()
        cache_file.write_text(json.dumps(spec))
        cache_meta.write_text(json.dumps({"fetched_at": time.time(), "url": spec_url}))

        return spec

    except Exception as e:
        # Fallback to stale cache
        if cache_file.exists():
            console.print(f"[yellow]Warning: Using stale cached spec ({e})[/yellow]")
            try:
                return json.loads(cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        console.print(f"[red]Failed to fetch spec from {spec_url}: {e}[/red]")
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
def resolve_refs(schema, spec_root: dict, max_depth: int = 10):
    """Recursively resolve $ref pointers in an OpenAPI schema."""
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
            return resolve_refs(resolved, spec_root, max_depth - 1)
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
                return {
                    "method": method.upper(),
                    "path": path,
                    "operationId": operation_id,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                    "parameters": resolved.get("parameters", []),
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
    elif auth_type == "oidc":
        return _oidc_auth(profile, auth_config, quiet)
    elif auth_type == "api-key":
        return _api_key_auth(auth_config, quiet)
    elif auth_type == "basic":
        return _basic_auth(auth_config, quiet)
    else:
        if not quiet:
            console.print(f"[red]Unknown auth type: {auth_type}[/red]")
        raise typer.Exit(1)


def _bearer_auth(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle bearer token auth — static from env or OAuth flow."""
    # Static token from env var
    env_var = auth_config.get("token_env_var")
    if env_var:
        token = os.environ.get(env_var)
        if not token:
            if not quiet:
                console.print(f"[red]Set the {env_var} environment variable with your token.[/red]")
            raise typer.Exit(1)
        prefix = auth_config.get("prefix", "Bearer ")
        header = auth_config.get("header", "Authorization")
        return {header: f"{prefix}{token}"}

    # OAuth token-endpoint flow
    token_endpoint = auth_config.get("token_endpoint")
    if token_endpoint:
        return _oauth_bearer(profile, auth_config, quiet)

    if not quiet:
        console.print("[red]Bearer auth requires either token_env_var or token_endpoint in profile.[/red]")
    raise typer.Exit(1)


def _oauth_bearer(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle OAuth password-grant or similar token endpoint flows."""
    profile_name = profile.get("_name", "default")
    token_cache = CACHE_DIR / f"{profile_name}_token.json"

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
        console.print("[yellow]Token expired or missing. Run 'openapi-cli4ai login' to authenticate.[/yellow]")
    raise typer.Exit(1)


def _try_refresh_token(profile: dict, auth_config: dict, cached: dict) -> dict | None:
    """Attempt to refresh an OAuth token."""
    refresh_endpoint = auth_config.get("refresh_endpoint")
    if not refresh_endpoint:
        return None
    try:
        base_url = profile["base_url"].rstrip("/")
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        with httpx.Client(verify=verify, timeout=30.0) as client:
            resp = client.post(
                f"{base_url}{refresh_endpoint}",
                headers={"Authorization": f"Bearer {cached['refresh_token']}"},
            )
            if resp.status_code == 200:
                new_data = resp.json()
                if "expires_in" in new_data:
                    new_data["expires_at"] = time.time() + new_data["expires_in"]
                elif "expires_at" not in new_data:
                    new_data["expires_at"] = time.time() + 86400  # 24h default
                profile_name = profile.get("_name", "default")
                token_cache = CACHE_DIR / f"{profile_name}_token.json"
                ensure_dirs()
                token_cache.write_text(json.dumps(new_data))
                token_cache.chmod(0o600)
                return new_data
    except Exception:
        pass
    return None


# ── OIDC (Authorization Code + PKCE) ─────────────────────────────────────────


def _oidc_auth(profile: dict, auth_config: dict, quiet: bool = False) -> dict:
    """Handle OIDC auth -- cached token with form-encoded refresh."""
    profile_name = profile.get("_name", "default")
    token_cache = CACHE_DIR / f"{profile_name}_token.json"

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
                    refreshed["expires_at"] = time.time() + refreshed.get("expires_in", 300)
                    ensure_dirs()
                    token_cache.write_text(json.dumps(refreshed))
                    token_cache.chmod(0o600)
                    return {"Authorization": f"Bearer {refreshed['access_token']}"}
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    if not quiet:
        console.print("[yellow]Token expired or missing. Run 'openapi-cli4ai login' to authenticate.[/yellow]")
    raise typer.Exit(1)


def _oidc_refresh(auth_config: dict, cached: dict, verify: bool = True) -> dict | None:
    """Refresh an OIDC token using form-encoded POST (standard OIDC spec)."""
    token_url = auth_config.get("token_url", "")
    client_id = auth_config.get("client_id", "")
    if not token_url or not client_id:
        return None
    try:
        with httpx.Client(verify=verify, timeout=30.0) as client:
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
    except Exception:
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
            self.wfile.write(
                f"<html><body><h2>Login failed</h2><p>{_OIDCCallbackHandler.error}</p></body></html>".encode()
            )

    def log_message(self, format: str, *args: object) -> None:
        pass  # Suppress HTTP server logging


def _oidc_login(auth_config: dict, profile_name: str, no_browser: bool = False, verify: bool = True) -> None:
    """Run OIDC Authorization Code + PKCE flow.

    With browser (default): opens browser, listens on localhost for callback.
    Without browser (--no-browser): prints URL, user pastes redirect URL back.
    """
    authorize_url = auth_config.get("authorize_url", "")
    token_url = auth_config.get("token_url", "")
    client_id = auth_config.get("client_id", "")
    scopes = auth_config.get("scopes", "openid")
    callback_port = auth_config.get("callback_port", 8484)
    redirect_uri = auth_config.get("redirect_uri", f"http://localhost:{callback_port}/callback")

    if not authorize_url or not token_url or not client_id:
        console.print("[red]OIDC auth requires authorize_url, token_url, and client_id.[/red]")
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
        auth_code = _oidc_login_no_browser(full_auth_url)
    else:
        auth_code = _oidc_login_browser(full_auth_url, callback_port, state)

    # Exchange code for tokens
    _oidc_exchange_code(
        token_url=token_url,
        client_id=client_id,
        auth_code=auth_code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        profile_name=profile_name,
        verify=verify,
    )


def _oidc_login_browser(full_auth_url: str, callback_port: int, state: str) -> str:
    """Open browser and listen for the OIDC callback on localhost."""
    _OIDCCallbackHandler.auth_code = None
    _OIDCCallbackHandler.error = None
    _OIDCCallbackHandler.expected_state = state
    server = HTTPServer(("127.0.0.1", callback_port), _OIDCCallbackHandler)
    server.timeout = 120

    console.print(f"[dim]Listening on http://localhost:{callback_port}/callback[/dim]")
    console.print("[bold]Opening browser for login...[/bold]")
    webbrowser.open(full_auth_url)
    console.print("[dim]Waiting for callback (120s timeout)...[/dim]")

    server.handle_request()
    server.server_close()

    if _OIDCCallbackHandler.error:
        console.print(f"[red]OIDC error: {_OIDCCallbackHandler.error}[/red]")
        raise typer.Exit(1)
    if not _OIDCCallbackHandler.auth_code:
        console.print("[red]No authorization code received.[/red]")
        raise typer.Exit(1)

    return _OIDCCallbackHandler.auth_code


def _oidc_login_no_browser(full_auth_url: str) -> str:
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
        console.print(f"[red]OIDC error: {params['error'][0]}[/red]")
        raise typer.Exit(1)

    if "code" not in params:
        console.print("[red]No authorization code found in the URL.[/red]")
        console.print("[dim]Expected a URL like: http://localhost:.../callback?code=...&state=...[/dim]")
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
) -> None:
    """Exchange an authorization code for tokens and cache them."""
    try:
        with httpx.Client(verify=verify, timeout=30.0) as client:
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
            console.print(f"[red]Token exchange failed ({resp.status_code}):[/red]")
            console.print(resp.text)
            raise typer.Exit(1)

        token_data = resp.json()
        if "expires_in" in token_data:
            token_data["expires_at"] = time.time() + token_data["expires_in"]
        elif "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + 86400

        token_cache = CACHE_DIR / f"{profile_name}_token.json"
        ensure_dirs()
        token_cache.write_text(json.dumps(token_data))
        token_cache.chmod(0o600)

        console.print("[green]Logged in successfully![/green]")
        console.print(f"[dim]Token cached at {token_cache}[/dim]")

    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {token_url}[/red]")
        raise typer.Exit(1)


def _api_key_auth(auth_config: dict, quiet: bool = False) -> dict:
    """Handle API key auth via custom header."""
    env_var = auth_config.get("env_var", "")
    key = os.environ.get(env_var, "") if env_var else ""
    if not key:
        if not quiet:
            console.print(f"[red]Set the {env_var} environment variable with your API key.[/red]")
        raise typer.Exit(1)
    header = auth_config.get("header", "X-API-Key")
    prefix = auth_config.get("prefix", "")
    return {header: f"{prefix}{key}"}


def _basic_auth(auth_config: dict, quiet: bool = False) -> dict:
    """Handle HTTP basic auth."""
    user_var = auth_config.get("username_env_var", "")
    pass_var = auth_config.get("password_env_var", "")
    username = os.environ.get(user_var, "") if user_var else ""
    password = os.environ.get(pass_var, "") if pass_var else ""
    if not username or not password:
        if not quiet:
            missing = []
            if not username:
                missing.append(user_var)
            if not password:
                missing.append(pass_var)
            console.print(f"[red]Set environment variable(s): {', '.join(missing)}[/red]")
        raise typer.Exit(1)
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
    stream: bool = False,
) -> httpx.Response:
    """Make an authenticated HTTP request."""
    base_url = profile["base_url"].rstrip("/")
    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    if extra_headers:
        headers.update(extra_headers)

    url = f"{base_url}{path}" if path.startswith("/") else f"{base_url}/{path}"

    with httpx.Client(verify=verify, timeout=60.0, follow_redirects=True) as client:
        if stream:
            headers["Accept"] = "text/event-stream"
            # For streaming, we need to return within the context manager
            # so we handle this differently in the call command
            raise NotImplementedError("Use stream_request() for streaming")

        return client.request(
            method=method.upper(),
            url=url,
            json=json_body,
            params=params,
            headers=headers,
        )


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

    if raw:
        print(response.text)
        return

    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = response.json()
            if status >= 400:
                _display_error(data, status)
            elif json_output:
                print(json.dumps(data, indent=2, default=str))
            else:
                console.print(RichJSON(json.dumps(data, default=str)))
        except json.JSONDecodeError:
            console.print(response.text)
    else:
        console.print(response.text)

    console.print(
        f"[{status_style}]{status}[/{status_style}] [{status_style}]{response.reason_phrase}[/{status_style}]",
        style="dim",
    )


def _display_error(data, status: int) -> None:
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
                    console.print(f"[red]Error: {data['error']}[/red]")
                    return content_buffer

                # Handle status updates
                elif "status" in data:
                    status = data.get("status", "")
                    tool_name = data.get("tool_name", "")
                    if tool_name:
                        console.print(f"[yellow][{status}] {tool_name}[/yellow]")
                    elif status not in ("running", "complete"):
                        console.print(f"[dim]{status}[/dim]")

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
            console.print("[yellow]Tip: Try a different search term or use --tag to filter by category.[/yellow]")
        return

    # Sort
    eps.sort(key=lambda x: (x["path"], x["method"]))

    if output_format == "json":
        print(json.dumps(eps, indent=2))
    elif output_format == "compact":
        for ep in eps:
            color = METHOD_COLORS.get(ep["method"], "white")
            console.print(f"[{color}]{ep['method']:7s}[/{color}] {ep['path']}  [dim]{ep.get('summary', '')}[/dim]")
        console.print(f"[dim]{len(eps)} endpoint(s)[/dim]")
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
        console.print(f"[dim]{len(eps)} endpoint(s)[/dim]")


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
        console.print(f"[red]Invalid HTTP method: {method}[/red]")
        raise typer.Exit(1)

    profile_name, profile = get_active_profile()

    # Parse body
    json_body = None
    if body:
        if body.startswith("@"):
            file_path = Path(body[1:])
            if not file_path.exists():
                console.print(f"[red]Body file not found: {file_path}[/red]")
                raise typer.Exit(1)
            try:
                json_body = json.loads(file_path.read_text())
                console.print(f"[dim]Body loaded from {file_path}[/dim]")
            except json.JSONDecodeError as e:
                console.print(f"[red]Invalid JSON in {file_path}: {e}[/red]")
                raise typer.Exit(1)
        else:
            try:
                json_body = json.loads(body)
            except json.JSONDecodeError as e:
                console.print(f"[red]Invalid JSON body: {e}[/red]")
                raise typer.Exit(1)

    # Parse query params (key=value format)
    params = {}
    if query:
        for q in query:
            if "=" in q:
                k, v = q.split("=", 1)
                params[k] = v
            else:
                console.print(f"[red]Invalid query param (expected key=value): {q}[/red]")
                raise typer.Exit(1)

    # Parse extra headers
    extra_headers = {}
    if header:
        for h in header:
            if ":" in h:
                k, v = h.split(":", 1)
                extra_headers[k.strip()] = v.strip()
            else:
                console.print(f"[red]Invalid header (expected Key:Value): {h}[/red]")
                raise typer.Exit(1)

    # Build URL
    base_url = profile["base_url"].rstrip("/")
    full_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{full_path}"

    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    headers.update(extra_headers)

    start_time = time.perf_counter()

    with httpx.Client(verify=verify, timeout=60.0, follow_redirects=True) as client:
        if stream:
            console.print(f"[dim]{method} {full_path} (streaming)...[/dim]")
            headers["Accept"] = "text/event-stream"
            with client.stream(
                method,
                url,
                json=json_body,
                params=params or None,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    _display_error(
                        response.json() if "json" in response.headers.get("content-type", "") else response.text,
                        response.status_code,
                    )
                    raise typer.Exit(1)
                stream_sse(response)
            elapsed = time.perf_counter() - start_time
            console.print(f"[dim]Completed in {elapsed:.2f}s[/dim]")
        else:
            console.print(f"[dim]{method} {full_path}...[/dim]")
            response = client.request(
                method,
                url,
                json=json_body,
                params=params or None,
                headers=headers,
            )
            elapsed = time.perf_counter() - start_time
            handle_response(response, raw=raw, json_output=output_json_flag)
            console.print(f"[dim]{elapsed:.2f}s[/dim]")


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
        elif location:
            # cookie or other — treat as query
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
        console.print(f"[red]Operation '{operation}' not found in spec.[/red]")
        # Suggest similar operations
        all_eps = extract_endpoint_summaries(spec)
        op_lower = operation.lower()
        suggestions = [e["operationId"] for e in all_eps if op_lower in e["operationId"].lower()][:5]
        if suggestions:
            console.print("[dim]Did you mean:[/dim]")
            for s in suggestions:
                console.print(f"  [cyan]{s}[/cyan]")
        raise typer.Exit(1)

    # Parse input
    parsed_input = {}
    if input_file:
        fp = Path(input_file)
        if not fp.exists():
            console.print(f"[red]Input file not found: {input_file}[/red]")
            raise typer.Exit(1)
        try:
            parsed_input = json.loads(fp.read_text())
        except json.JSONDecodeError as e:
            console.print(f"[red]Invalid JSON in {input_file}: {e}[/red]")
            raise typer.Exit(1)
    elif input_data:
        try:
            parsed_input = json.loads(input_data)
        except json.JSONDecodeError as e:
            console.print(f"[red]Invalid JSON input: {e}[/red]")
            raise typer.Exit(1)

    # Route inputs to the right places
    method = endpoint["method"]
    path_template = endpoint["path"]
    parameters = endpoint.get("parameters", [])
    has_request_body = endpoint.get("requestBody") is not None

    path_params, query_params, header_params, json_body = _route_inputs(parsed_input, parameters, has_request_body)

    # Substitute path parameters
    full_path = path_template
    for key, value in path_params.items():
        full_path = full_path.replace(f"{{{key}}}", str(value))

    # Check for unresolved path params
    if "{" in full_path:
        import re as _re

        missing = _re.findall(r"\{(\w+)\}", full_path)
        console.print(f"[red]Missing required path parameter(s): {', '.join(missing)}[/red]")
        console.print(f'[dim]Provide them in --input, e.g. --input \'{{"{missing[0]}": "value"}}\'[/dim]')
        raise typer.Exit(1)

    # Build URL and make request
    base_url = profile["base_url"].rstrip("/")
    url = f"{base_url}{full_path}"

    verify = profile.get("verify_ssl", True) and get_verify_ssl()
    headers = dict(profile.get("headers", {}))
    headers.update(get_auth_headers(profile))
    headers.update(header_params)

    start_time = time.perf_counter()
    console.print(f"[dim]{method} {full_path}...[/dim]")

    with httpx.Client(verify=verify, timeout=60.0, follow_redirects=True) as client:
        if stream:
            headers["Accept"] = "text/event-stream"
            with client.stream(
                method,
                url,
                json=json_body,
                params=query_params or None,
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    _display_error(
                        response.json() if "json" in response.headers.get("content-type", "") else response.text,
                        response.status_code,
                    )
                    raise typer.Exit(1)
                stream_sse(response)
            elapsed = time.perf_counter() - start_time
            console.print(f"[dim]Completed in {elapsed:.2f}s[/dim]")
        else:
            response = client.request(
                method,
                url,
                json=json_body,
                params=query_params or None,
                headers=headers,
            )
            elapsed = time.perf_counter() - start_time
            handle_response(response, raw=raw, json_output=output_json_flag)
            console.print(f"[dim]{elapsed:.2f}s[/dim]")


# ── Commands: init ─────────────────────────────────────────────────────────────
@app.command("init")
def cmd_init(
    name: Annotated[str, typer.Argument(help="Profile name (e.g., petstore, myapp)")],
    url: Annotated[str, typer.Option("--url", "-u", help="Base URL of the API")] = "",
    spec_path: Annotated[
        Optional[str], typer.Option("--spec", "-s", help="Path to OpenAPI spec (auto-detected if omitted)")
    ] = None,
    spec_url: Annotated[Optional[str], typer.Option("--spec-url", help="Full URL to OpenAPI spec file")] = None,
    auth_type: Annotated[str, typer.Option("--auth", help="Auth type: bearer, oidc, api-key, basic, none")] = "none",
) -> None:
    """Initialize a new API profile with guided setup.

    Fetches the OpenAPI spec, validates it, and creates a profile.

    Examples:
        openapi-cli4ai init petstore --url https://petstore3.swagger.io/api/v3
        openapi-cli4ai init myapp --url http://localhost:8000 --auth bearer
        openapi-cli4ai init github --url https://api.github.com --spec-url https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json --auth bearer
    """
    if not url:
        url = typer.prompt("Base URL of the API")

    url = url.rstrip("/")

    # Check if profile already exists
    data = load_profiles()
    if name in data.get("profiles", {}):
        if not typer.confirm(f"Profile '{name}' already exists. Overwrite?"):
            console.print("[yellow]Cancelled.[/yellow]")
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
        with httpx.Client(verify=get_verify_ssl(), timeout=10.0, follow_redirects=True) as client:
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
                except Exception:
                    continue

        if resolved_spec_path:
            profile["openapi_path"] = resolved_spec_path
        else:
            console.print("[yellow]Could not auto-detect spec location.[/yellow]")
            manual_path = typer.prompt("OpenAPI spec path", default="/openapi.json")
            profile["openapi_path"] = manual_path

    # Auth setup
    if auth_type == "bearer":
        bearer_mode = typer.prompt("Static token or login endpoint?", type=str, default="login")
        if bearer_mode.lower().startswith("s"):
            # Static token from env var
            env_var = typer.prompt("Environment variable for token", default=f"{name.upper()}_TOKEN")
            profile["auth"]["token_env_var"] = env_var
            console.print(f"[dim]Set {env_var} in your environment or add it to a .env file.[/dim]")
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
        authorize_url = typer.prompt("Authorization URL (full URL)")
        token_url = typer.prompt("Token URL (full URL)")
        client_id_val = typer.prompt("Client ID")
        scopes = typer.prompt("Scopes", default="openid")
        redirect_uri_val = typer.prompt("Redirect URI (or leave blank for localhost callback)", default="")
        if redirect_uri_val:
            profile["auth"]["redirect_uri"] = redirect_uri_val
        else:
            cb_port = typer.prompt("Local callback port", default="8484")
            profile["auth"]["callback_port"] = int(cb_port)
        profile["auth"]["authorize_url"] = authorize_url
        profile["auth"]["token_url"] = token_url
        profile["auth"]["client_id"] = client_id_val
        profile["auth"]["scopes"] = scopes
        console.print("[dim]Run 'openapi-cli4ai login' to authenticate via browser.[/dim]")
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
    except (typer.Exit, Exception) as e:
        console.print(f"[yellow]Warning: Could not validate spec ({e}). Profile saved anyway.[/yellow]")

    # Save profile
    del profile["_name"]
    data.setdefault("profiles", {})[name] = profile
    data["active_profile"] = name
    save_profiles(data)

    console.print(f"\n[green]Profile '{name}' created and set as active.[/green]")
    console.print(f"[dim]Config saved to {CONFIG_FILE}[/dim]")
    console.print("\n[bold]Next steps:[/bold]")
    console.print("  openapi-cli4ai endpoints           [dim]# List available endpoints[/dim]")
    console.print("  openapi-cli4ai endpoints -s keyword [dim]# Search endpoints[/dim]")
    console.print("  openapi-cli4ai call GET /path       [dim]# Call an endpoint[/dim]")


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
        typer.Option("--no-browser", help="OIDC: print login URL instead of opening browser (for headless/SSH)"),
    ] = False,
) -> None:
    """Login to an API that uses OAuth/token-endpoint authentication.

    Supports two auth modes:
        - auth.type=bearer with token_endpoint: username/password grant
        - auth.type=oidc: Authorization Code + PKCE flow (browser or --no-browser)

    For OIDC, use --no-browser on headless machines: prints the login URL,
    then prompts you to paste back the redirect URL after authenticating.

    Password input methods (bearer mode, in priority order):
        1. --password-file /path/to/file
        2. --password-stdin (piped input)
        3. --password flag (avoid for special characters)
        4. Interactive prompt (most secure)
    """
    profile_name, profile = get_active_profile()
    auth_config = profile.get("auth", {})

    # OIDC flow
    if auth_config.get("type") == "oidc":
        verify = profile.get("verify_ssl", True) and get_verify_ssl()
        _oidc_login(auth_config, profile_name, no_browser=no_browser, verify=verify)
        # Try fetching the spec now that we're authenticated
        try:
            spec = fetch_spec(profile, refresh=True)
            endpoints = extract_endpoint_summaries(spec)
            spec_title = spec.get("info", {}).get("title", "Unknown")
            console.print(f"[green]Fetched spec: {spec_title} ({len(endpoints)} endpoints)[/green]")
        except (typer.Exit, Exception):
            pass
        return

    if auth_config.get("type") != "bearer" or not auth_config.get("token_endpoint"):
        console.print("[yellow]Login is for profiles with bearer auth + token_endpoint, or oidc.[/yellow]")
        console.print("[dim]If your API uses a static token or API key, set the environment variable instead.[/dim]")
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
            pf = Path(password_file)
            if not pf.exists():
                console.print(f"[red]Password file not found: {password_file}[/red]")
                raise typer.Exit(1)
            resolved_password = pf.read_text().strip()
        elif password_stdin:
            if sys.stdin.isatty():
                console.print("[red]--password-stdin requires piped input.[/red]")
                raise typer.Exit(1)
            resolved_password = sys.stdin.read().strip()
        elif password:
            resolved_password = password
        else:
            resolved_password = typer.prompt("Password", hide_input=True)

    if needs_username and not username:
        username = typer.prompt("Username")

    payload = {}
    for k, v in payload_template.items():
        if isinstance(v, str):
            v = v.replace("{username}", username).replace("{password}", resolved_password)
        payload[k] = v

    # Make token request
    base_url = profile["base_url"].rstrip("/")
    token_endpoint = auth_config["token_endpoint"]
    token_url = f"{base_url}{token_endpoint}"
    verify = profile.get("verify_ssl", True) and get_verify_ssl()

    try:
        with httpx.Client(verify=verify, timeout=30.0) as client:
            resp = client.post(token_url, json=payload)

        if resp.status_code != 200:
            _display_error(
                resp.json() if "json" in resp.headers.get("content-type", "") else resp.text,
                resp.status_code,
            )
            raise typer.Exit(1)

        token_data = resp.json()
        if "expires_in" in token_data:
            token_data["expires_at"] = time.time() + token_data["expires_in"]
        elif "expires_at" not in token_data:
            token_data["expires_at"] = time.time() + 86400  # 24h default

        # Cache token
        token_cache = CACHE_DIR / f"{profile_name}_token.json"
        ensure_dirs()
        token_cache.write_text(json.dumps(token_data))
        token_cache.chmod(0o600)

        console.print("[green]Logged in successfully![/green]")
        console.print(f"[dim]Token cached at {token_cache}[/dim]")

        # Try fetching the spec now that we're authenticated
        spec_url = _resolve_spec_url(profile)
        cache_file, _ = _spec_cache_paths(spec_url)
        if not cache_file.exists():
            try:
                spec = fetch_spec(profile, refresh=True)
                endpoints = extract_endpoint_summaries(spec)
                spec_title = spec.get("info", {}).get("title", "Unknown")
                console.print(f"[green]Fetched spec: {spec_title} ({len(endpoints)} endpoints)[/green]")
            except (typer.Exit, Exception):
                pass  # Spec fetch is best-effort after login

    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {base_url}[/red]")
        raise typer.Exit(1)


@app.command("logout")
def cmd_logout() -> None:
    """Clear cached authentication tokens for the active profile."""
    profile_name, profile = get_active_profile()
    token_cache = CACHE_DIR / f"{profile_name}_token.json"
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
        console.print(f"[red]Profile '{name}' not found.[/red]")
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
        console.print(f"[red]Profile '{name}' not found.[/red]")
        raise typer.Exit(1)

    if not force and not typer.confirm(f"Remove profile '{name}'?"):
        raise typer.Exit(0)

    del data["profiles"][name]
    if data.get("active_profile") == name:
        data["active_profile"] = next(iter(data["profiles"]), None)
    save_profiles(data)

    # Clean up cached spec and token
    for f in CACHE_DIR.glob(f"{name}_*"):
        f.unlink(missing_ok=True)

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
        console.print(f"[red]Profile '{name}' not found.[/red]")
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
) -> None:
    """openapi-cli4ai — Interact with any REST API using natural language.

    Point it at an OpenAPI spec. Discover endpoints. Call them directly or let
    an LLM figure out the right one from your natural language query.
    """
    set_insecure_mode(insecure)

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
