import asyncio
import json
import logging
from contextlib import AsyncExitStack
from types import SimpleNamespace

import httpx
import pytest
from app.config import McpServerConfig
from app.mcp_broker import McpBroker, McpConnection, ToolBinding, _safe_error, _validated_schema


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


class RecordingConnection:
    def __init__(self, result=None, timeout: float = 30) -> None:
        self.config = McpServerConfig(
            name="test", url="http://test.invalid/mcp", call_timeout_seconds=timeout
        )
        self.calls = []
        self.result = result or SimpleNamespace(model_dump=lambda mode: {"ok": True})

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def secure_binding() -> ToolBinding:
    return ToolBinding(
        public_name="mcp_test_secure",
        server_name="test",
        remote_name="secure",
        description="Secure tool",
        schema={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    )


async def test_invalid_arguments_never_reach_server() -> None:
    broker = McpBroker(())
    connection = RecordingConnection()
    broker.connections["test"] = connection  # type: ignore[assignment]

    with pytest.raises(ValueError, match="advertised schema"):
        await broker.call_binding(secure_binding(), {"entity_id": 123})

    assert connection.calls == []


async def test_audit_has_hash_metadata_without_raw_arguments(caplog) -> None:
    broker = McpBroker(())
    connection = RecordingConnection()
    broker.connections["test"] = connection  # type: ignore[assignment]
    caplog.set_level(logging.INFO, logger="app.mcp_broker")

    await broker.call_binding(
        secure_binding(), {"entity_id": "secret.entity"}, client_id="browser-1"
    )

    record = caplog.records[-1].getMessage()
    assert '"tool":"mcp_test_secure"' in record
    assert '"server":"test"' in record
    assert '"client_id":"browser-1"' in record
    assert '"outcome":"success"' in record
    assert "argument_sha256" in record
    assert "secret.entity" not in record


async def test_policy_hook_can_deny_without_changing_default() -> None:
    connection = RecordingConnection()
    default = McpBroker(())
    default.connections["test"] = connection  # type: ignore[assignment]
    await default.call_binding(secure_binding(), {"entity_id": "light.safe"})

    denied = McpBroker((), policy_hook=lambda binding, arguments, client_id: False)
    denied.connections["test"] = connection  # type: ignore[assignment]
    with pytest.raises(PermissionError, match="denied by policy"):
        await denied.call_binding(secure_binding(), {"entity_id": "lock.front"})
    assert len(connection.calls) == 1


async def test_oversized_tool_result_is_bounded_valid_json() -> None:
    result = SimpleNamespace(model_dump=lambda mode: {"secret": "x" * 10_000})
    broker = McpBroker((), output_limit=256)
    broker.connections["test"] = RecordingConnection(result)  # type: ignore[assignment]

    payload = await broker.call_binding(secure_binding(), {"entity_id": "light.safe"})

    decoded = json.loads(payload)
    assert decoded["truncated"] is True
    assert len(payload.encode()) <= 256


def test_errors_are_redacted_to_type_or_status() -> None:
    assert _safe_error(ConnectionError("Bearer super-secret")) == "ConnectionError"
    request = httpx.Request(
        "POST", "https://example.invalid/mcp", headers={"Authorization": "Bearer secret"}
    )
    response = httpx.Response(401, request=request)
    error = httpx.HTTPStatusError("secret response", request=request, response=response)
    assert _safe_error(error) == "HTTP 401"


class ConcurrentConnection(RecordingConnection):
    def __init__(self, timeout: float = 30) -> None:
        super().__init__(timeout=timeout)
        self.active = 0
        self.max_active = 0

    async def call_tool(self, name, arguments):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return self.result
        finally:
            self.active -= 1


async def test_per_server_timeout_and_shared_concurrency() -> None:
    timed = McpBroker(())
    timed.connections["test"] = ConcurrentConnection(timeout=0.001)  # type: ignore[assignment]
    with pytest.raises(TimeoutError):
        await timed.call_binding(secure_binding(), {"entity_id": "light.slow"})

    bounded = McpBroker((), shared_concurrency=2)
    connection = ConcurrentConnection()
    bounded.connections["test"] = connection  # type: ignore[assignment]
    await asyncio.gather(
        *(
            bounded.call_binding(
                secure_binding(), {"entity_id": f"light.{index}"}, client_id=f"client-{index}"
            )
            for index in range(6)
        )
    )
    assert connection.max_active == 2
