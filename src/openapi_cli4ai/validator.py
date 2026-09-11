"""Catalog profile validator, shared by ``catalog validate`` and CI.

Checks fields, ownership (base_url vs source domain), inline secrets,
prompt-injection markers, and optionally fetches the live OpenAPI spec
through an SSRF guard.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse
from typing import Any

import httpx
import yaml
from publicsuffix2 import get_sld

from openapi_cli4ai.catalog import _catalog_to_profile
from openapi_cli4ai.config import _resolve_spec_url

_CATALOG_AUTH_TYPES = ("none", "bearer", "oidc", "device", "api-key", "basic")
_CATALOG_PROMO_TERMS = (
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
_CATALOG_SECRET_KEYS = ("token", "password", "secret", "api_key", "apikey", "client_secret")
# Stored prompt-injection markers: catalog text ships in the wheel and feeds the AI router.
_CATALOG_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the",
    "system prompt",
    "you are now",
    "new instructions",
    "</system>",
    "<|im_start|>",
)
_CATALOG_MAX_SPEC_BYTES = 5 * 1024 * 1024
# Domain verification: the API owner publishes a TXT record at
# _openapi-cli4ai.<registrable domain of base_url> containing github=<maintainer>.
DOMAIN_TXT_PREFIX = "_openapi-cli4ai"
_DNS_TIMEOUT = 5.0


class DnsUnavailable(RuntimeError):
    """dnspython is not installed, so a domain_verified claim cannot be checked."""


def _registrable_domain(host: str) -> str:
    """Registrable domain via the Public Suffix List (handles co.uk, shared hosts)."""
    return (get_sld(host.lower()) or host.lower()) if host else ""


def _assert_public_url(url: str) -> None:
    """Reject non-https or non-public hosts. SSRF guard for validation fetches.

    Catalog profiles describe public APIs; the CI validator must never be
    coaxed into fetching cloud-metadata (169.254.169.254) or internal hosts.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"must be an https:// URL (got '{parsed.scheme or 'no scheme'}')")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"cannot resolve host '{host}': {exc}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError(f"host '{host}' resolves to non-public address {ip}")


def _resolve_txt(name: str) -> list[str]:
    """TXT record strings for `name` ([] when the record does not exist).

    Raises DnsUnavailable without dnspython; other resolver failures raise
    ValueError so the caller can report them as a validation error.
    """
    try:
        import dns.exception
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise DnsUnavailable("dnspython is not installed") from exc
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=_DNS_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except dns.exception.DNSException as exc:
        raise ValueError(f"DNS lookup for {name} failed: {exc.__class__.__name__}") from exc
    return [b"".join(rdata.strings).decode("utf-8", errors="replace") for rdata in answer]


def _check_domain_verified(entry: dict) -> str | None:
    """Return an error message if the entry's domain_verified claim does not hold.

    The claim holds when _openapi-cli4ai.<registrable domain> has a TXT record
    `github=<maintainer>` (maintainer compared case-insensitively).
    """
    host = urllib.parse.urlparse(str(entry.get("base_url", ""))).hostname or ""
    domain = _registrable_domain(host)
    name = f"{DOMAIN_TXT_PREFIX}.{domain}"
    expected = f"github={str(entry.get('maintainer', '')).lower()}"
    try:
        records = _resolve_txt(name)
    except ValueError as exc:
        return f"domain_verified: {exc}"
    found = [r for r in records if r.strip().lower() == expected]
    if found:
        return None
    if records:
        return f"domain_verified: TXT {name} exists but none equals '{expected}' (got {records})"
    return f"domain_verified: no TXT record at {name}; publish '{expected}' or remove the claim"


def _fetch_public_spec(url: str) -> tuple[str, str]:
    """SSRF-guarded, size-capped, timeout-bounded fetch of a catalog spec URL."""
    _assert_public_url(url)
    with (
        httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True, max_redirects=3) as client,
        client.stream("GET", url) as resp,
    ):
        resp.raise_for_status()
        _assert_public_url(str(resp.url))  # re-check the post-redirect host
        content_type = resp.headers.get("content-type", "")
        body = bytearray()
        for chunk in resp.iter_bytes():
            body += chunk
            if len(body) > _CATALOG_MAX_SPEC_BYTES:
                raise ValueError(f"spec exceeds {_CATALOG_MAX_SPEC_BYTES // (1024 * 1024)}MB limit")
    return content_type, bytes(body).decode("utf-8", errors="replace")


def _parse_openapi_text(content_type: str, text: str) -> Any:
    ct = content_type.lower()
    if ("yaml" in ct or "vnd.oai.openapi" in ct) and "json" not in ct:
        return yaml.safe_load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return yaml.safe_load(text)


def _validate_catalog_entry(entry: dict, *, check_spec: bool) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one catalog entry. Errors block a submission."""
    errors: list[str] = []
    warnings: list[str] = []

    for field in ("name", "description", "maintainer", "source", "base_url", "auth"):
        if not entry.get(field):
            errors.append(f"missing required field '{field}'")
    if errors:
        return errors, warnings

    name = str(entry["name"])
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", name):
        errors.append(f"name '{name}' must be lowercase letters, digits, and hyphens")
    if entry.get("_slug") and entry["_slug"] != name:
        errors.append(f"name '{name}' must match the file name '{entry['_slug']}'")

    desc = str(entry["description"])
    if not 3 <= len(desc) <= 100:
        errors.append("description must be 3-100 characters")
    promo = [t for t in _CATALOG_PROMO_TERMS if t in desc.lower()]
    if promo:
        warnings.append(f"description contains promotional terms {promo}; keep it factual")
    injection = [m for m in _CATALOG_INJECTION_MARKERS if m in desc.lower()]
    if injection:
        errors.append(f"description contains prompt-injection markers {injection}")

    for field in ("source", "base_url"):
        if not str(entry[field]).startswith("https://"):
            errors.append(f"{field} must be an https:// URL")
    if entry.get("openapi_url") and not str(entry["openapi_url"]).startswith("https://"):
        errors.append("openapi_url must be an https:// URL")
    if not entry.get("openapi_url") and not entry.get("openapi_path"):
        errors.append("provide either openapi_url or openapi_path")

    auth = entry.get("auth")
    if not isinstance(auth, dict) or auth.get("type") not in _CATALOG_AUTH_TYPES:
        errors.append(f"auth.type must be one of: {', '.join(_CATALOG_AUTH_TYPES)}")
    else:
        if auth.get("type") == "api-key" and not auth.get("env_var"):
            errors.append("auth.env_var is required for api-key profiles")
        for key, value in auth.items():
            if key.lower() in _CATALOG_SECRET_KEYS and isinstance(value, str):
                errors.append(f"auth.{key} looks like an inline secret; reference an *_env_var instead")

    if "domain_verified" in entry and not isinstance(entry.get("domain_verified"), bool):
        errors.append("domain_verified must be true or false")

    if errors:
        return errors, warnings

    base_host = urllib.parse.urlparse(str(entry["base_url"])).hostname or ""
    source_host = urllib.parse.urlparse(str(entry["source"])).hostname or ""
    if _registrable_domain(base_host) != _registrable_domain(source_host):
        errors.append(
            f"ownership: base_url domain '{_registrable_domain(base_host)}' != "
            f"source domain '{_registrable_domain(source_host)}' "
            "(source must be the API's own developer/docs URL)"
        )

    if check_spec and entry.get("domain_verified") is True:
        # Online check, like the spec fetch. Without dnspython the claim cannot
        # be verified: that is an error in CI (the gate) and a warning locally.
        try:
            problem = _check_domain_verified(entry)
        except DnsUnavailable:
            msg = "domain_verified: cannot verify without dnspython (uv sync installs it)"
            if os.environ.get("GITHUB_ACTIONS"):
                errors.append(msg)
            else:
                warnings.append(msg)
        else:
            if problem:
                errors.append(problem)

    if check_spec:
        profile = _catalog_to_profile(entry)
        try:
            spec_url = _resolve_spec_url(profile)
            content_type, text = _fetch_public_spec(spec_url)
            spec = _parse_openapi_text(content_type, text)
        except (httpx.HTTPError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"spec not reachable/valid: {exc}")
            return errors, warnings
        if not isinstance(spec, dict) or not (spec.get("openapi") or spec.get("swagger")):
            errors.append("spec is not a valid OpenAPI/Swagger document")

    return errors, warnings


def _gh_annotate(level: str, file: str, msg: str) -> None:
    """Emit a GitHub Actions annotation so validation errors show on the PR diff."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level} file={file}::{msg}")
