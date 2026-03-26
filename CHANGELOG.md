# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] - 2026-03-25

### Added

- Installable PyPI package (`uv pip install openapi-cli4ai` / `uvx openapi-cli4ai`)
- Profile management with TOML config at `~/.openapi-cli4ai.toml`
- Endpoint discovery with search and tag filtering (`endpoints`)
- API calling with query params, headers, JSON body, and file body (`call`)
- Auth support: bearer token, OAuth token endpoint, API key, basic auth
- Token caching with automatic refresh
- Auto-fetch spec after login for auth-gated APIs
- SSE streaming support
- OpenAPI spec caching with TTL
- JSON and compact output formats
- `python -m openapi_cli4ai` support
- Dependabot for automated dependency and GitHub Actions updates
- CodeQL code scanning workflow
- Trusted Publisher release workflow with Sigstore attestation
- All GitHub Actions pinned to commit SHAs
