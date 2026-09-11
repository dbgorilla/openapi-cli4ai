# Contributing to openapi-cli4ai

Thanks for your interest in contributing!

## Getting Started

1. Fork the repository
2. Clone your fork
3. Install [uv](https://docs.astral.sh/uv/) if you don't have it
4. Install in editable mode: `uv pip install -e .`
5. Run `openapi-cli4ai --help` to verify everything works

## Project Structure

```
src/openapi_cli4ai/
  __init__.py       # Package exports
  __main__.py       # python -m support
  cli.py            # Typer commands, HTTP/auth flows, spec fetching
  config.py         # Config file, cache dir, active-profile precedence
  catalog.py        # Bundled profile catalog: load, find, map to a profile
  validator.py      # Catalog profile validator (shared by `catalog validate` and CI)
  _state.py         # Runtime flags set by the root command (--verbose, --profile, ...)
  _ui.py            # Shared Rich consoles and the verbose logger
openapi-cli4ai      # Standalone shim (imports from package)
tests/              # pytest tests
```

Commands live in `src/openapi_cli4ai/cli.py`; it imports the helpers it needs from the service modules, so tests can patch either the service module or the `cli` binding. The standalone `openapi-cli4ai` script is a thin shim that imports from the package.

## Testing

```bash
pytest tests/ -m "not integration" -v
```

## Submitting a Pull Request

1. Create a feature branch (`git checkout -b my-feature`)
2. Make your changes
3. Run the tests
4. Commit with a clear message
5. Push and open a PR

## Contributing a Profile to the Catalog

`openapi-cli4ai` ships a community catalog of ready-made API profiles under
[`profiles/`](profiles/README.md). To add one:

1. Create `profiles/community/<slug>.toml` following the format in
   [`profiles/README.md`](profiles/README.md).
2. Reference secrets only via `*_env_var` fields — never commit a token.
3. Keep the `description` factual. `source` must be the API's own developer or
   docs URL (same domain as `base_url`).
4. Validate locally: `uv run openapi-cli4ai catalog validate profiles/community/<slug>.toml`
5. Regenerate the index and commit it: `uv run openapi-cli4ai catalog index`
6. Optional: claim the domain-verified badge by setting `domain_verified = true`
   and publishing a `_openapi-cli4ai.<domain>` TXT record naming you; see
   "Domain verification" in [`profiles/README.md`](profiles/README.md).

CI runs `catalog validate --all` and `catalog index --check` on every PR touching
`profiles/`, checking the fields, a live spec fetch, ownership, absence of inline
secrets, any `domain_verified` claim against DNS, and that `profiles/index.json`
is current — errors appear inline on the PR. Once merged, the profile is visible to every installed CLI within an hour
via the remote index (see `profiles/README.md`, "Freshness"). Profiles that are primarily promotional will be closed
without review.

## Reporting Issues

Open an issue on GitHub. Include:
- What you tried
- What happened
- What you expected
- The API spec you were using (if relevant)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
