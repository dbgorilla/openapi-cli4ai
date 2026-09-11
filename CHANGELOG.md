# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.7.0] - 2026-09-11

### Added

- `--profile` is accepted after the subcommand (`openapi-cli4ai endpoints --profile x`); long form only, since `-p` is reused by subcommands (#52)
- `catalog install --dry-run` prints the exact profile file that would be written and changes nothing (#53)
- `catalog uninstall <name>` removes an installed catalog profile plus its cached spec and token, with confirm prompt and `--force` (#54)
- Drop-in profiles: one file per profile under `~/.openapi-cli4ai/profiles.d/<name>.toml`, read alongside `~/.openapi-cli4ai.toml` (a drop-in overrides a same-named entry). `catalog install` writes there. No migration needed (#56)
- Remote catalog index: `catalog list`/`search`/`show`/`install` merge `profiles/index.json` from `main` over the bundled catalog (3 s timeout, cached one hour, silent fallback to cache then bundled; every remote entry is re-validated). `OAC_CATALOG_OFFLINE=1` disables it. New `catalog index` command generates the committed index; CI checks it is current (#57)
- DNS TXT domain verification: a profile may set `domain_verified = true`; `catalog validate` resolves `_openapi-cli4ai.<domain>` and requires `github=<maintainer>`. Shown as a `✓ dns` badge, separate from the tier (#58)

### Changed

- `cli.py` split into `config.py`, `catalog.py`, `validator.py` plus small state/UI modules; behavior unchanged (#55)
- Adopted ruff 0.16's default rule set; the rule selection is no longer implicit (#50, #51)
- Dependency updates: `typer` 0.27.2, `click` 8.5, `python-dotenv`, `mypy`, `types-PyYAML`, and the GitHub Actions group (#49, #50)

### Fixed

- Rich swallowed TOML table headers such as `[profiles.x]` in `profile show` output; now rendered as plain text (#53)
- File paths in install output are never wrapped across lines (#56)

## [0.6.0] - 2026-07-31

### Added

- **Profile catalog** — browse and install ready-made API profiles: `catalog search` / `show` / `list` / `install` / `validate`. Bundled into the wheel, so it works offline, with two trust tiers (`verified` / `community`) (#25)
- Global `--profile` / `-p` flag to select a profile for a single invocation (precedence: flag > `OAC_PROFILE` > active profile) (#25)
- Community-contributed profile: Xquik (#23)

### Changed

- Dependency updates: `typer` 0.27, `mypy` 2.3, `types-PyYAML`, and the GitHub Actions group (#38, #37)
- Pin `ruff` and run the locked version in CI so lint is deterministic (no more `uvx`-latest drift) (#39)

### Security

- Catalog validation hardening: SSRF guard on spec fetches (rejects cloud-metadata/private/loopback hosts, pre- and post-redirect), Public Suffix List domain-ownership check, spec size/redirect/timeout limits, prompt-injection scan of descriptions, and an explicit confirmation before installing unverified community profiles (#25)

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
