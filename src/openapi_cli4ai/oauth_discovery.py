"""OAuth endpoint discovery and dynamic client registration.

Lets a profile sign in with only a URL: no authorize_url, token_url or
client_id to copy by hand. Implements the chain MCP clients use, which works
for any OAuth-protected API that publishes the standard metadata:

1. Protected-resource metadata (RFC 9728), found through the
   ``WWW-Authenticate: Bearer resource_metadata=...`` header on a 401, or at
   ``/.well-known/oauth-protected-resource{path}``.
2. Authorization-server metadata (RFC 8414) or OpenID discovery.
3. Dynamic client registration (RFC 7591) when the profile has no client_id.

Functions take an ``httpx.Client`` so the caller keeps control of TLS,
timeouts and redirects.
"""

from __future__ import annotations

import re
import urllib.parse

import httpx


class DiscoveryError(Exception):
    """The server does not publish enough metadata to sign in automatically."""


def _origin_and_path(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}", parts.path.rstrip("/")


def _get_json(client: httpx.Client, url: str) -> dict | None:
    try:
        resp = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _resource_metadata_from_header(header: str) -> str | None:
    match = re.search(r'resource_metadata="([^"]+)"', header or "")
    return match.group(1) if match else None


def find_protected_resource_metadata(client: httpx.Client, resource_url: str) -> dict | None:
    """Return RFC 9728 metadata for ``resource_url``, or None if it publishes none."""
    candidates: list[str] = []
    try:
        probe = client.get(resource_url, headers={"Accept": "application/json, text/event-stream"})
        if probe.status_code == 401:
            hinted = _resource_metadata_from_header(probe.headers.get("www-authenticate", ""))
            if hinted:
                candidates.append(hinted)
    except httpx.HTTPError:
        pass
    origin, path = _origin_and_path(resource_url)
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")
    for url in candidates:
        meta = _get_json(client, url)
        if meta and meta.get("authorization_servers"):
            return meta
    return None


def find_authorization_server_metadata(client: httpx.Client, issuer: str) -> dict:
    """Return RFC 8414 / OpenID metadata for ``issuer``.

    Tries the path-aware well-known locations first, as RFC 8414 section 3.1
    requires for issuers with a path, then the legacy issuer-relative one.
    """
    origin, path = _origin_and_path(issuer)
    candidates = [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
    ]
    if path:
        candidates.append(f"{origin}{path}/.well-known/openid-configuration")
    for url in candidates:
        meta = _get_json(client, url)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            methods = meta.get("code_challenge_methods_supported")
            if methods is not None and "S256" not in methods:
                raise DiscoveryError(f"{issuer} does not support PKCE with S256; refusing to sign in without it.")
            return meta
    raise DiscoveryError(f"No OAuth authorization-server metadata found for {issuer}.")


def discover(client: httpx.Client, resource_url: str, issuer: str | None = None) -> dict:
    """Find the OAuth endpoints that protect ``resource_url``.

    Returns a dict using the profile's own auth keys (authorize_url,
    token_url, ...) so it can be merged straight into an auth config.
    """
    resource: str | None = None
    scopes: list[str] = []
    if not issuer:
        prm = find_protected_resource_metadata(client, resource_url)
        if prm:
            issuer = prm["authorization_servers"][0]
            resource = prm.get("resource") or resource_url
            scopes = prm.get("scopes_supported") or []
        else:
            issuer, _ = _origin_and_path(resource_url)
    as_meta = find_authorization_server_metadata(client, issuer)
    found = {
        "issuer": as_meta.get("issuer", issuer),
        "authorize_url": as_meta["authorization_endpoint"],
        "token_url": as_meta["token_endpoint"],
        "registration_endpoint": as_meta.get("registration_endpoint", ""),
        "revocation_endpoint": as_meta.get("revocation_endpoint", ""),
    }
    if resource:
        found["resource"] = resource
    if scopes:
        found["scopes"] = " ".join(scopes)
    return {k: v for k, v in found.items() if v}


def register_client(client: httpx.Client, registration_endpoint: str, redirect_uri: str, client_name: str) -> dict:
    """Register a public native client (RFC 7591) and return the server's response."""
    body = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    try:
        resp = client.post(registration_endpoint, json=body)
    except httpx.HTTPError as e:
        raise DiscoveryError(f"Client registration failed: {e}") from e
    if resp.status_code not in (200, 201):
        raise DiscoveryError(f"Client registration failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("client_id"):
        raise DiscoveryError("Client registration response has no client_id.")
    return data
