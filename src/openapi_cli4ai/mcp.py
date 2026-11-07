"""Minimal MCP (Model Context Protocol) client over Streamable HTTP.

Enough to list and call tools: initialize, tools/list, tools/call. Lets a
profile point at an MCP server instead of an OpenAPI spec; the tools are
presented as operations so ``endpoints`` and ``run`` work unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Self

import httpx

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    """The server returned an error, or a reply we cannot read."""


class MCPAuthError(MCPError):
    """The server rejected our credentials (HTTP 401/403)."""


def _parse_sse(text: str) -> list[dict]:
    messages: list[dict] = []
    data_lines: list[str] = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            try:
                msg = json.loads("\n".join(data_lines))
                if isinstance(msg, dict):
                    messages.append(msg)
            except json.JSONDecodeError:
                pass
            data_lines = []
    return messages


class MCPSession:
    """One MCP session. Use as a context manager so the session is closed."""

    def __init__(self, client: httpx.Client, url: str, headers: dict, client_name: str, client_version: str):
        self.client = client
        self.url = url
        self.headers = dict(headers)
        self.client_info = {"name": client_name, "version": client_version}
        self.session_id: str | None = None
        self.server_info: dict = {}
        self._next_id = 1

    def __enter__(self) -> Self:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.session_id:
            try:
                self.client.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass

    def _headers(self) -> dict:
        h = dict(self.headers)
        h["Accept"] = "application/json, text/event-stream"
        h["Content-Type"] = "application/json"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return h

    def _send(self, method: str, params: dict | None = None, notify: bool = False) -> Any:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        req_id = None
        if not notify:
            req_id = self._next_id
            self._next_id += 1
            payload["id"] = req_id
        resp = self.client.post(self.url, json=payload, headers=self._headers())
        if resp.status_code in (401, 403):
            raise MCPAuthError(f"HTTP {resp.status_code} from {self.url}")
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code} from {self.url}: {resp.text[:300]}")
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if notify:
            return None
        if "text/event-stream" in resp.headers.get("content-type", ""):
            messages = _parse_sse(resp.text)
        else:
            try:
                body = resp.json()
            except ValueError as e:
                raise MCPError(f"Unreadable reply to {method}") from e
            messages = body if isinstance(body, list) else [body]
        for msg in messages:
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"{method}: {err.get('message', err)}")
                return msg.get("result", {})
        raise MCPError(f"No reply to {method}")

    def initialize(self) -> dict:
        result = self._send(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": self.client_info},
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._send("notifications/initialized", notify=True)
        return result

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(100):  # bounded: a server that never ends pagination cannot hang us
            result = self._send("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._send("tools/call", {"name": name, "arguments": arguments})


def tools_to_spec(tools: list[dict], server_info: dict) -> dict:
    """Present MCP tools as an OpenAPI document: one POST operation per tool.

    ``x-mcp-tool`` marks the operation so ``run`` calls the tool instead of
    sending HTTP. ``x-read-only`` carries the tool's readOnlyHint.
    """
    paths: dict = {}
    for tool in tools:
        name = tool["name"]
        annotations = tool.get("annotations") or {}
        description = tool.get("description") or ""
        paths[f"/{name}"] = {
            "post": {
                "operationId": name,
                "summary": description.strip().split("\n")[0],
                "description": description,
                "tags": ["mcp"],
                "requestBody": {"content": {"application/json": {"schema": tool.get("inputSchema") or {}}}},
                "responses": {"200": {"description": "Tool result"}},
                "x-mcp-tool": name,
                "x-read-only": annotations.get("readOnlyHint") is True,
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": server_info.get("name", "MCP server"), "version": server_info.get("version", "")},
        "x-mcp": True,
        "paths": paths,
    }


def tool_signature(name: str, schema: dict) -> str:
    """Render a tool like ``name(a: string, b?: integer)``."""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = []
    for key, prop in props.items():
        typ = prop.get("type", "any") if isinstance(prop, dict) else "any"
        if isinstance(typ, list):
            typ = "|".join(str(t) for t in typ)
        args.append(f"{key}{'' if key in required else '?'}: {typ}")
    return f"{name}({', '.join(args)})"


def _pretty_if_json(text: str) -> str:
    """Indent text that is a JSON object or array; leave everything else as is."""
    if not text.lstrip().startswith(("{", "[")):
        return text
    try:
        return json.dumps(json.loads(text), indent=2)
    except json.JSONDecodeError:
        return text


def result_text(result: dict) -> str:
    """Join the text parts of a tool result; describe the non-text parts."""
    parts = []
    for item in result.get("content") or []:
        if item.get("type") == "text":
            parts.append(_pretty_if_json(item.get("text", "")))
        elif item.get("type") == "resource" and isinstance(item.get("resource"), dict):
            parts.append(item["resource"].get("text") or f"[resource {item['resource'].get('uri', '')}]")
        else:
            parts.append(f"[{item.get('type', 'unknown')} content]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], indent=2))
    return "\n".join(parts)
