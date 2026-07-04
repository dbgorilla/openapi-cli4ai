# Profile catalog

A community catalog of ready-made **profiles** for public APIs. A profile is
just the small bit of config `openapi-cli4ai` needs to talk to an API: a base
URL, where to find its OpenAPI spec, and how it authenticates.

> **Status: Phase 1 (scaffolding).** This directory establishes the format,
> validation, and governance. A CLI command to browse and install these
> profiles (`openapi-cli4ai profile install <name>`) is planned but not built
> yet — for now these files are a reviewed, validated reference.

## Tiers

Listing a profile here is not an endorsement of the product behind it. Trust is
a **tier**, shown next to every entry, not a gate on being listed.

| Tier | Directory | What it means |
| --- | --- | --- |
| **Verified** | `verified/` | A maintainer has confirmed the spec loads and the auth flow works. |
| **Community** | `community/` | Contributed via PR and passed automated validation. Not manually vetted. |

New submissions go to `community/`. A maintainer may promote a profile to
`verified/` after checking it end to end.

## Profile format

One profile per file, named `<slug>.toml`, matching this shape (see
[`profile.schema.json`](profile.schema.json) for the full contract):

```toml
name = "example"                        # must match the file name
description = "Example REST API"         # one factual line, no marketing
maintainer = "your-github-username"
source = "https://example.com/docs"      # the API's official docs (same domain as base_url)

base_url = "https://api.example.com"
openapi_url = "https://api.example.com/openapi.json"   # or: openapi_path = "/openapi.json"

[auth]
type = "api-key"                         # none | bearer | oidc | device | api-key | basic
env_var = "EXAMPLE_API_KEY"              # reference secrets by env var — never inline them
header = "x-api-key"
```

Auth field names match the CLI's runtime config; see
[`../examples/profiles.toml.example`](../examples/profiles.toml.example) for
each auth type.

## Contributing a profile

1. Copy the format above into `community/<slug>.toml`.
2. Reference secrets only via `*_env_var` fields — **never commit a token**.
3. Keep `description` factual. Promotional copy will be flagged and the PR
   closed without review.
4. `source` must be the API's own developer/docs URL (same registrable domain
   as `base_url`) — this is a lightweight ownership check.

Validate locally before opening the PR:

```bash
uv run --with jsonschema python scripts/validate_profiles.py
```

CI runs the same check on every PR touching `profiles/**`. It verifies the
schema, that the OpenAPI spec is reachable and parseable, the ownership
heuristic, and that no secrets are inlined.
