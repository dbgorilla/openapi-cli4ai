# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |

## Reporting a Vulnerability

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, use [GitHub's private vulnerability reporting](https://github.com/dbgorilla/openapi-cli4ai/security/advisories/new) to report the issue. We will acknowledge receipt within 48 hours and provide a detailed response within 7 days.

## Security Considerations

This tool handles API credentials (tokens, passwords, API keys). Keep in mind:

- **Credentials stay local** — profiles reference env vars by name, not by value
- **Token cache files** (`~/.cache/openapi-cli4ai/`) contain short-lived access tokens and are protected with restricted file permissions (0600)
- **`verify_ssl: false`** disables certificate verification. Only use this for local development

## Supply Chain Protections

- **Trusted Publishers** — PyPI releases use OIDC-based trusted publishing (no stored API tokens)
- **Sigstore attestation** — every release has verifiable build provenance via `actions/attest-build-provenance`
- **SHA-pinned GitHub Actions** — all CI/CD actions are pinned to commit SHAs to prevent tag hijacking
- **Dependabot** — automated weekly checks for dependency and GitHub Actions updates
- **CodeQL** — automated code scanning on every push and PR
- **Minimal CI permissions** — each workflow job declares only the permissions it needs
