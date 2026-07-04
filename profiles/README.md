# Profile catalog

Ready-made **profiles** for public APIs. A profile is the small bit of config
`openapi-cli4ai` needs to talk to an API: a base URL, where its OpenAPI spec
lives, and how it authenticates. The catalog is bundled into the package, so
these commands work offline:

```bash
openapi-cli4ai catalog search cov            # find profiles
openapi-cli4ai catalog show codecov          # preview one
openapi-cli4ai catalog install codecov       # add it to your config, with next steps
openapi-cli4ai --profile codecov endpoints   # use it (no activation needed)
```

`install` maps the catalog entry into your `~/.openapi-cli4ai.toml`, tells you
exactly which environment variable to set for auth, and hands you the next
command — no docs required.

## Tiers

Listing a profile here is not an endorsement of the product behind it. Trust is
a **tier**, shown next to every entry, not a gate on being listed.

| Tier | Directory | What it means |
| --- | --- | --- |
| **Verified** | `verified/` | A maintainer confirmed the spec loads and the auth flow works. |
| **Community** | `community/` | Contributed via PR and passed automated validation. Not manually vetted. |

New submissions go to `community/`; a maintainer may promote a profile to
`verified/` after checking it end to end.

## Profile format

One profile per file, named `<slug>.toml`:

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

1. Create `community/<slug>.toml` in the format above.
2. Reference secrets only via `*_env_var` fields — **never commit a token**.
3. Keep `description` factual. `source` must be the API's own developer/docs
   URL (same registrable domain as `base_url`).

Validate before opening the PR — the CLI is the single source of truth:

```bash
uv run openapi-cli4ai catalog validate profiles/community/<slug>.toml
```

CI runs `catalog validate --all` on every PR touching `profiles/`, checking the
fields, a live OpenAPI spec fetch, the ownership heuristic, and that no secrets
are inlined. Errors are posted inline on the PR.
