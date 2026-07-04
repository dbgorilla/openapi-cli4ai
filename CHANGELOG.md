# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.5.1] - 2026-07-04

### Fixed

- Ingest YAML OpenAPI specs served as `application/vnd.oai.openapi` (e.g. Codecov's schema endpoint), with a YAML fallback when a body advertised as JSON fails to decode (#22)
- `--version` now derives from installed package metadata instead of a hardcoded literal that had drifted from the real version (#22)

### Changed

- Commit `uv.lock` and use `uv sync --locked` in CI for reproducible builds; Dependabot now updates the lockfile via the `uv` ecosystem (#20)
- Dependency updates: `typer` 0.26 (with test adjustments for its vendored `click`), plus the python-deps and GitHub Actions groups (#21, #19, #16)

## [0.5.0] - 2026-04-21

### Added

- `--force-login` flag to bypass cached tokens (#9)
- Non-interactive `init` flags for scripted profile creation (#9)
- Security hardening across CLI surfaces (#10)
- OpenAPI spec composition support (#10)
- `py.typed` marker for downstream type checkers (#13)

### Changed

- SHA-pinned publish workflow and simplified CI (#13)
- Dependency updates: `rich` (#12), GitHub Actions group (#11)

## [0.4.0] - 2026-04-15

### Added

- OIDC Device Flow auth type for headless/SSH environments
- Token exchange and auto-discovery of OIDC endpoints
- Token injection from environment for CI use cases

## [0.3.0] - 2026-03-28

### Added

- OIDC Authorization Code + PKCE auth type (`auth.type = "oidc"`)
- Browser-based login with localhost callback
- `--no-browser` flag for headless/SSH environments
- CSRF protection via state parameter validation
- Tested with Auth0 and Keycloak

## [0.2.0] - 2026-03-26

### Added

- `run` command — call API operations by name with auto-routed inputs
- Case-insensitive operationId matching
- Fuzzy suggestions when an operationId isn't found
- Auto-generated release notes via `.github/release.yml`

### Fixed

- `typer>=0.12` crashed with click 8.2+ — bumped to `>=0.24` (#3)
- `--format json` appended non-JSON summary line breaking machine parsing
- `init` auto-detect ignored `--insecure` flag

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
