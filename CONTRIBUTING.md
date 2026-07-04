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
  cli.py            # All CLI code lives here
openapi-cli4ai      # Standalone shim (imports from package)
tests/              # pytest tests
```

The core code lives in `src/openapi_cli4ai/cli.py`. The standalone `openapi-cli4ai` script is a thin shim that imports from the package.

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

CI runs `catalog validate --all` on every PR touching `profiles/`, checking the
fields, a live spec fetch, ownership, and absence of inline secrets — errors
appear inline on the PR. Profiles that are primarily promotional will be closed
without review.

## Reporting Issues

Open an issue on GitHub. Include:
- What you tried
- What happened
- What you expected
- The API spec you were using (if relevant)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
