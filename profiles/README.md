# Profile catalog

Ready-made **profiles** for public APIs. A profile is the small bit of config
`openapi-cli4ai` needs to talk to an API: a base URL, where its OpenAPI spec
lives, and how it authenticates. The catalog is bundled into the package, so
these commands work offline; when online they also pick up profiles merged to
`main` since your release (see [Freshness](#freshness)):

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
`verified/` after checking it end to end. Installing a **community** profile
prompts for confirmation (it shows the `base_url` your credentials would be
sent to); **verified** profiles install without a prompt. Use `--yes` to skip
the prompt in scripts.

## Freshness

The bundled catalog is the offline baseline. On top of it, `catalog list`,
`search`, `show` and `install` fetch [`index.json`](index.json) from this
repository's `main` branch (3 s timeout, cached for an hour under
`~/.cache/openapi-cli4ai/`) and merge it in, so a profile is usable as soon as
its PR merges. Any failure falls back to the cached index, then to the bundled
copy, silently. Every remote entry is re-checked with the offline validator
before use; a bad one is skipped. Set `OAC_CATALOG_OFFLINE=1` to never fetch.

`index.json` is generated from the TOML files by `openapi-cli4ai catalog index`
and committed; CI fails a PR whose index is stale.

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

Validate before opening the PR — the CLI is the single source of truth — and
regenerate the index:

```bash
uv run openapi-cli4ai catalog validate profiles/community/<slug>.toml
uv run openapi-cli4ai catalog index
```

CI runs `catalog validate --all` on every PR touching `profiles/`. It checks the
fields and (Public Suffix List-based) domain ownership, fetches the OpenAPI spec
behind SSRF guards with size/redirect/timeout limits, rejects inlined secrets,
and flags prompt-injection markers in the description. Errors are posted inline
on the PR.
