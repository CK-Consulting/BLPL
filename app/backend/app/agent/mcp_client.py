"""A minimal MCP client, spoken over streamable HTTP.

Hand-rolled rather than pulled from an SDK because BLPL needs exactly three of
MCP's verbs — initialize, tools/list, tools/call — and an SDK would bring a
transport stack, a session model, and a release cadence for the privilege. The
protocol below is small enough to read in one sitting, which is the property
that matters for something standing between an LLM and a board file.

The server it talks to is kicad-ai-assistant (kcaa) running headless: the KiCad
side of the workbench, exposing routing, placement, zone and netlist tools that
BLPL's emitter deliberately does not implement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    """The server refused, or could not be reached. Carries a message meant to
    be shown, because "the KiCad bridge is down" is something the user must be
    told rather than something to retry silently."""


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict


class MCPClient:
    """One connection to one MCP server.

    Not a long-lived session: each call opens a request. Streamable HTTP servers
    accept that, and it means a bridge that was down a minute ago starts working
    again without anything to reset.
    """

    def __init__(self, url: str, *, timeout: float = 120.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._session_id: str | None = None
        self._next_id = 0

    # -- transport -----------------------------------------------------------

    def _rpc(self, method: str, params: dict | None = None) -> Any:
        import httpx

        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        headers = {
            "content-type": "application/json",
            # Streamable HTTP servers may answer either way; accepting both is
            # what lets one client talk to both flavours.
            "accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(self.url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 — any transport failure reads the same
            raise MCPError(f"cannot reach the MCP server at {self.url}: {exc}") from exc

        if response.status_code >= 400:
            raise MCPError(f"MCP server returned {response.status_code}: {response.text[:300]}")
        sid = response.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        body = _parse_body(response.text, response.headers.get("content-type", ""))
        if body is None:
            raise MCPError(f"MCP server sent no parseable response to {method}")
        if "error" in body:
            err = body["error"]
            raise MCPError(f"{method} failed: {err.get('message', err)}")
        return body.get("result")

    # -- verbs ---------------------------------------------------------------

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "blpl", "version": "0.5.0"},
            },
        )
        # The spec wants an initialized notification; servers that don't care
        # ignore it, and one that does would refuse everything after this.
        try:
            self._rpc("notifications/initialized")
        except MCPError:
            pass
        return result or {}

    def list_tools(self) -> list[MCPTool]:
        result = self._rpc("tools/list") or {}
        return [
            MCPTool(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
            )
            for t in result.get("tools", [])
            if t.get("name")
        ]

    def call_tool(self, name: str, arguments: dict) -> str:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        parts: list[str] = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block))
        text = "\n".join(p for p in parts if p) or json.dumps(result)
        if result.get("isError"):
            raise MCPError(text[:1000])
        return text


def _parse_body(text: str, content_type: str) -> dict | None:
    """JSON, or the first JSON payload in an SSE stream."""
    text = text.strip()
    if not text:
        return None
    if "text/event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
