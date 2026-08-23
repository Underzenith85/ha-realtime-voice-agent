import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from app.config import McpServerConfig
from app.mcp_broker import McpBroker, McpConnection, ToolBinding, _validated_schema


def test_realtime_tool_definition() -> None:
    binding = ToolBinding(
        public_name="mcp_homeassistant_turn_on",
        server_name="homeassistant",
        remote_name="HassTurnOn",
        description="Turn on an exposed entity",
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    assert binding.realtime_definition() == {
        "type": "function",
        "name": "mcp_homeassistant_turn_on",
        "description": "Turn on an exposed entity",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    }


@pytest.mark.parametrize(
    "schema",
    [
        None,
        {"type": "string"},
        {"type": "object", "properties": "not-an-object"},
    ],
)
def test_invalid_mcp_tool_schema_is_rejected(schema) -> None:
    with pytest.raises(ValueError, match=r"invalid input schema.*fake\.broken"):
        _validated_schema(schema, "fake", "broken")


class FakeSession:
    async def list_tools(self):
        return SimpleNamespace(tools=[])


class DelayedSession(FakeSession):
    def __init__(self) -> None:
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()

    async def call_tool(self, name, arguments):
        self.called.set()
        await self.release.wait()
        self.returned.set()
        return SimpleNamespace()


async def test_homeassistant_fallback_is_used_only_for_confirmed_404(monkeypatch) -> None:
    config = McpServerConfig(name="homeassistant", url="http://supervisor/core/api/mcp/assist")
    connection = McpConnection(config)
    requested = []

    async def connect(url):
        requested.append(url)
        if len(requested) == 1:
            request = httpx.Request("POST", url)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        return AsyncExitStack(), FakeSession()

    monkeypatch.setattr(connection, "_connect", connect)

    stack, _, fallback = await connection._connect_with_fallback()
    await stack.aclose()

    assert requested == [
        "http://supervisor/core/api/mcp/assist",
        "http://supervisor/core/api/mcp",
    ]
    assert fallback is True


async def test_homeassistant_fallback_is_not_used_for_other_failures(monkeypatch) -> None:
    config = McpServerConfig(name="homeassistant", url="http://supervisor/core/api/mcp/assist")
    connection = McpConnection(config)
    requested = []

    async def connect(url):
        requested.append(url)
        request = httpx.Request("POST", url)
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    monkeypatch.setattr(connection, "_connect", connect)

    with pytest.raises(httpx.HTTPStatusError):
        await connection._connect_with_fallback()
    assert requested == ["http://supervisor/core/api/mcp/assist"]


async def test_connection_reconnects_after_initial_failure(monkeypatch) -> None:
    connection = McpConnection(
        McpServerConfig(name="optional", url="http://optional.test/mcp"),
        initial_backoff=0.001,
        max_backoff=0.002,
        refresh_interval=60,
    )
    attempts = 0

    async def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("offline")
        return AsyncExitStack(), FakeSession(), False

    monkeypatch.setattr(connection, "_connect_with_fallback", connect)
    await connection.start()
    assert connection.health.status == "unavailable"

    async with asyncio.timeout(1):
        while connection.health.status != "connected":
            await asyncio.sleep(0.001)

    assert attempts == 2
    assert connection.health.last_error is None
    await connection.close()


async def test_late_tool_result_after_caller_timeout_keeps_connection_healthy(
    monkeypatch,
) -> None:
    session = DelayedSession()
    connection = McpConnection(
        McpServerConfig(name="optional", url="http://optional.test/mcp"),
        refresh_interval=60,
    )

    async def connect():
        return AsyncExitStack(), session, False

    monkeypatch.setattr(connection, "_connect_with_fallback", connect)
    await connection.start()
    call = asyncio.create_task(connection.call_tool("slow", {}))
    await session.called.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    session.release.set()
    await session.returned.wait()
    await asyncio.sleep(0)

    assert connection.health.status == "connected"
    await connection.close()


def tool(name: str, schema=None):
    return SimpleNamespace(
        name=name,
        description=None,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


async def test_catalog_versions_schema_diagnostics_and_collision_names() -> None:
    broker = McpBroker((McpServerConfig(name="homeassistant", url="http://ha.test/mcp"),))
    connection = broker.connections["homeassistant"]
    connection.health.status = "connected"
    prefix = "a" * 80
    connection.tools = (
        tool(f"{prefix}b"),
        tool(f"{prefix}a"),
        tool("broken", {"type": "string"}),
    )

    await broker._rebuild_catalog()

    assert broker.catalog_version == 1
    assert len(broker.bindings) == 2
    names = sorted(broker.bindings)
    assert all(len(name) <= 64 for name in names)
    assert names[0] != names[1]
    assert connection.health.schema_errors == [
        "invalid input schema for MCP tool homeassistant.broken"
    ]

    await broker._rebuild_catalog()
    assert broker.catalog_version == 1

    connection.tools = (tool("replacement"),)
    await broker._rebuild_catalog()
    assert broker.catalog_version == 2
    assert set(broker.bindings) == {"mcp_homeassistant_replacement"}
