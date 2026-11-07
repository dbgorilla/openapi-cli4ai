"""MCP server profiles, OAuth discovery + dynamic registration, read-only mode.

A fake server (httpx.MockTransport) plays the protected resource, the
authorization server and the MCP endpoint.
"""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest
from typer.testing import CliRunner

from openapi_cli4ai import cli as cli_mod
from openapi_cli4ai import mcp as mcp_mod
from openapi_cli4ai import oauth_discovery

runner = CliRunner()
HOST = "https://api.example.test"
MCP_URL = f"{HOST}/mcp/readonly"

TOOLS = [
    {
        "name": "list_things",
        "description": "List things.\nMore detail.",
        "inputSchema": {
            "type": "object",
            "properties": {"owner": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["owner"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {"name": "delete_thing", "description": "Delete a thing.", "inputSchema": {"type": "object"}},
]


class FakeServer:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.registrations = 0
        self.revoked: list[str] = []
        self.tool_calls: list[dict] = []
        self.refreshes: list[dict] = []
        self.valid_tokens = {"tok-1", "tok-2", "api-key-xyz"}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if url == f"{HOST}/.well-known/oauth-protected-resource/mcp/readonly":
            return httpx.Response(200, json={"resource": MCP_URL, "authorization_servers": [f"{HOST}/mcp"]})
        if url == f"{HOST}/.well-known/oauth-authorization-server/mcp":
            return httpx.Response(
                200,
                json={
                    "issuer": f"{HOST}/mcp",
                    "authorization_endpoint": f"{HOST}/mcp/authorize",
                    "token_endpoint": f"{HOST}/mcp/token",
                    "registration_endpoint": f"{HOST}/mcp/register",
                    "revocation_endpoint": f"{HOST}/mcp/revoke",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url == f"{HOST}/mcp/register":
            self.registrations += 1
            body = json.loads(request.content)
            assert body["token_endpoint_auth_method"] == "none"
            return httpx.Response(201, json={"client_id": "dyn-client", **body})
        if url == f"{HOST}/mcp/token":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert form["client_id"] == "dyn-client"
            assert form["resource"] == MCP_URL
            if form["grant_type"] == "authorization_code":
                assert form["code"] == "the-code" and form["code_verifier"]
                return httpx.Response(200, json={"access_token": "tok-1", "refresh_token": "r-1", "expires_in": 3600})
            self.refreshes.append(form)
            return httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600})
        if url == f"{HOST}/mcp/revoke":
            self.revoked.append(dict(urllib.parse.parse_qsl(request.content.decode()))["token"])
            return httpx.Response(200)
        if url == MCP_URL:
            auth = request.headers.get("authorization", "")
            if auth.removeprefix("Bearer ") not in self.valid_tokens:
                return httpx.Response(
                    401,
                    headers={
                        "www-authenticate": f'Bearer resource_metadata="{HOST}/.well-known/oauth-protected-resource/mcp/readonly"'
                    },
                )
            if request.method == "DELETE":
                return httpx.Response(200)
            msg = json.loads(request.content)
            if "id" not in msg:
                return httpx.Response(202)
            if msg["method"] == "initialize":
                result = {"serverInfo": {"name": "Example", "version": "1"}, "capabilities": {}}
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": msg["id"], "result": result}, headers={"mcp-session-id": "s1"}
                )
            assert request.headers.get("mcp-session-id") == "s1"
            if msg["method"] == "tools/list":
                result = {"tools": TOOLS}
            else:
                self.tool_calls.append(msg["params"])
                result = {"content": [{"type": "text", "text": f"called {msg['params']['name']}"}]}
            # Answer as server-sent events to cover that path.
            sse = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result})}\n\n"
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(404)


@pytest.fixture
def server(monkeypatch, tmp_config):
    fake = FakeServer()

    def make_client(verify: bool = True, follow_redirects: bool = True) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(fake.handle), follow_redirects=follow_redirects)

    monkeypatch.setattr(cli_mod, "_make_client", make_client)
    monkeypatch.delenv("EX_API_KEY", raising=False)
    monkeypatch.delenv("OAC_READ_ONLY", raising=False)
    cli_mod._state._read_only = False
    result = runner.invoke(
        cli_mod.app, ["profile", "add", "ex", "--mcp-url", MCP_URL, "--token-env", "EX_API_KEY", "--read-only"]
    )
    assert result.exit_code == 0, result.output
    return fake


def _browser_login(monkeypatch, server: FakeServer) -> None:
    """Stand in for the browser: check the authorize URL, hand back a code."""

    def fake_browser(full_auth_url: str, callback_port: int, state: str, timeout: int = 120) -> str:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(full_auth_url).query))
        assert full_auth_url.startswith(f"{HOST}/mcp/authorize?")
        assert q["client_id"] == "dyn-client"
        assert q["code_challenge_method"] == "S256"
        assert q["resource"] == MCP_URL
        assert q["redirect_uri"] == "http://127.0.0.1:8484/callback"
        assert "scope" not in q
        return "the-code"

    monkeypatch.setattr(cli_mod, "_oidc_login_browser", fake_browser)
    result = runner.invoke(cli_mod.app, ["login"])
    assert result.exit_code == 0, result.output
    assert "2 tools available" in result.output


def test_login_discovers_registers_and_caches(monkeypatch, server, tmp_config):
    _browser_login(monkeypatch, server)
    _, _, cache_dir = tmp_config
    assert json.loads((cache_dir / "ex_token.json").read_text())["access_token"] == "tok-1"
    reg = cache_dir / "ex_oauth_client.json"
    assert reg.stat().st_mode & 0o077 == 0
    # A second login reuses the registration.
    _browser_login(monkeypatch, server)
    assert server.registrations == 1


def test_expired_token_refreshes_with_registered_client(monkeypatch, server, tmp_config):
    _browser_login(monkeypatch, server)
    _, _, cache_dir = tmp_config
    token_file = cache_dir / "ex_token.json"
    data = json.loads(token_file.read_text())
    data["expires_at"] = time.time() - 10
    token_file.write_text(json.dumps(data))

    result = runner.invoke(cli_mod.app, ["run", "list_things", "owner=me", "limit=3"])
    assert result.exit_code == 0, result.output
    assert "called list_things" in result.output
    assert server.refreshes and server.refreshes[0]["refresh_token"] == "r-1"
    saved = json.loads(token_file.read_text())
    assert saved["access_token"] == "tok-2" and saved["refresh_token"] == "r-1"


def test_run_types_key_value_args_from_schema(monkeypatch, server):
    _browser_login(monkeypatch, server)
    result = runner.invoke(cli_mod.app, ["run", "list_things", "owner=007", "limit=3"])
    assert result.exit_code == 0, result.output
    assert server.tool_calls[-1] == {"name": "list_things", "arguments": {"owner": "007", "limit": 3}}


def test_read_only_hides_and_refuses_unmarked_tools(monkeypatch, server):
    _browser_login(monkeypatch, server)
    listed = runner.invoke(cli_mod.app, ["endpoints", "--format", "compact"])
    assert "list_things(owner: string, limit?: integer)" in listed.output
    assert "delete_thing" not in listed.output
    refused = runner.invoke(cli_mod.app, ["run", "delete_thing"])
    assert refused.exit_code == 1
    assert "Read-only mode" in refused.output
    assert not any(c["name"] == "delete_thing" for c in server.tool_calls)


def test_api_key_env_var_skips_login(monkeypatch, server):
    monkeypatch.setenv("EX_API_KEY", "api-key-xyz")
    result = runner.invoke(cli_mod.app, ["run", "list_things", "owner=me"])
    assert result.exit_code == 0, result.output
    assert server.requests[-2].headers["authorization"] == "Bearer api-key-xyz"


def test_not_signed_in_says_how(server):
    result = runner.invoke(cli_mod.app, ["endpoints"])
    assert result.exit_code == 1
    assert "openapi-cli4ai login" in result.output


def test_logout_revokes_and_clears(monkeypatch, server, tmp_config):
    _browser_login(monkeypatch, server)
    _, _, cache_dir = tmp_config
    result = runner.invoke(cli_mod.app, ["logout", "--forget-client"])
    assert result.exit_code == 0, result.output
    assert "revoked" in result.output
    assert server.revoked == ["r-1"]
    assert not (cache_dir / "ex_token.json").exists()
    assert not (cache_dir / "ex_oauth_client.json").exists()


def test_call_refuses_unsafe_method_in_read_only(tmp_config, sample_profiles, monkeypatch):
    mod, _, _ = tmp_config
    mod.save_profiles(sample_profiles)
    monkeypatch.setenv("OAC_READ_ONLY", "1")
    result = runner.invoke(cli_mod.app, ["call", "DELETE", "/pet/1"])
    assert result.exit_code == 1
    assert "Read-only mode" in result.output


def test_discovery_refuses_server_without_s256():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"authorization_endpoint": "a", "token_endpoint": "t", "code_challenge_methods_supported": ["plain"]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(oauth_discovery.DiscoveryError):
        oauth_discovery.find_authorization_server_metadata(client, "https://idp.test")


def test_tools_to_spec_marks_read_only():
    spec = mcp_mod.tools_to_spec(TOOLS, {"name": "X"})
    ops = {p["post"]["operationId"]: p["post"] for p in spec["paths"].values()}
    assert ops["list_things"]["x-read-only"] is True
    assert ops["delete_thing"]["x-read-only"] is False
