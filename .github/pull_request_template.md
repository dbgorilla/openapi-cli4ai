<!-- Thanks for contributing! Describe your change below. -->

## What & why



---

<!-- ▼ Delete this whole section if you are NOT adding a catalog profile ▼ -->

### Adding a catalog profile — please confirm

- [ ] The profile is for a **publicly accessible** API with a stable OpenAPI spec.
- [ ] I am the API owner, or have the owner's permission.
- [ ] `source` is the API's **own** developer/docs URL (same registrable domain as `base_url`).
- [ ] No secrets are inlined — credentials are referenced via `*_env_var` fields only.
- [ ] `description` is factual (no marketing copy).
- [ ] I ran `uv run openapi-cli4ai catalog validate profiles/community/<slug>.toml` locally and it passed.

> **Reviewer:** independently verify that `base_url` resolves to the claimed
> service (typosquats and look-alike domains are the main risk), and that the
> auth shape can't leak a credential to an unexpected host.
