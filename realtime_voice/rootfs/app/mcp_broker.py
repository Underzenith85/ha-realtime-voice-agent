"""MCP discovery, namespacing, and invocation."""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft7Validator
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from app.config import McpServerConfig


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value).strip("_").lower()


def _validated_schema(schema: Any, server_name: str, tool_name: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"invalid input schema for MCP tool {server_name}.{tool_name}")
    try:
        Draft7Validator.check_schema(schema)
    except Exception as err:
        raise ValueError(f"invalid input schema for MCP tool {server_name}.{tool_name}") from err
    return schema


@dataclass(frozen=True, slots=True)
class ToolBinding:
    public_name: str
    server_name: str
    remote_name: str
    description: str
    schema: dict[str, Any]

    def realtime_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.public_name,
            "description": self.description,
            "parameters": self.schema,
        }


class McpConnection:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def connect(self) -> None:
        stack = AsyncExitStack()
        try:
            if self.config.transport == "sse":
                streams = await stack.enter_async_context(
                    sse_client(self.config.url, headers=self.config.headers)
                )
            else:
                streams = await stack.enter_async_context(
                    streamablehttp_client(self.config.url, headers=self.config.headers)
                )
            # streamablehttp_client returns a third session-id callback in newer MCP
            # releases. ClientSession's third positional argument is instead the read
            # timeout, so only pass the read and write streams here.
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self.session = session

    async def close(self) -> None:
        if self._stack:
            await self._stack.aclose()
        self._stack = None
        self.session = None


class McpBroker:
    def __init__(self, configs: tuple[McpServerConfig, ...], output_limit: int = 16_384) -> None:
        self.connections = {config.name: McpConnection(config) for config in configs}
        self.bindings: dict[str, ToolBinding] = {}
        self.output_limit = output_limit

    async def start(self) -> None:
        for connection in self.connections.values():
            await connection.connect()
        await self.refresh()

    async def close(self) -> None:
        # MCP transports own AnyIO cancellation scopes. Unwind them in the
        # reverse order they were entered, just like an AsyncExitStack.
        for connection in reversed(self.connections.values()):
            await connection.close()

    async def refresh(self) -> None:
        bindings: dict[str, ToolBinding] = {}
        for name, connection in self.connections.items():
            assert connection.session
            result = await connection.session.list_tools()
            for tool in result.tools:
                allowed = connection.config.allowed_tools
                if name != "homeassistant" and not allowed:
                    continue
                if allowed and tool.name not in allowed:
                    continue
                public_name = f"mcp_{_slug(name)}_{_slug(tool.name)}"[:64]
                if public_name in bindings:
                    raise ValueError(f"duplicate MCP tool name: {public_name}")
                bindings[public_name] = ToolBinding(
                    public_name=public_name,
                    server_name=name,
                    remote_name=tool.name,
                    description=tool.description or f"Tool from {name}",
                    schema=_validated_schema(
                        tool.inputSchema or {"type": "object", "properties": {}},
                        name,
                        tool.name,
                    ),
                )
        self.bindings = bindings

    async def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        binding = self.bindings.get(public_name)
        if binding is None:
            return json.dumps({"error": "tool_not_available"})
        connection = self.connections[binding.server_name]
        assert connection.session
        result = await connection.session.call_tool(binding.remote_name, arguments)
        payload = json.dumps(result.model_dump(mode="json"), separators=(",", ":"))
        if len(payload.encode()) > self.output_limit:
            payload = payload.encode()[: self.output_limit].decode(errors="ignore") + "…"
        return payload

    def realtime_tools(self) -> list[dict[str, Any]]:
        return [binding.realtime_definition() for binding in self.bindings.values()]
