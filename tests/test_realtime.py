import asyncio
import json
from typing import Any

from app.config import Settings
from app.mcp_broker import ToolBinding
from app.realtime import RealtimeSession


class SlowBroker:
    async def call_binding(self, binding: ToolBinding, arguments: dict[str, Any]) -> str:
        await asyncio.sleep(60)
        return "unreachable"


class RecordingWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class MutableBroker:
    def __init__(self, binding: ToolBinding) -> None:
        self.version = 1
        self.bindings = {binding.public_name: binding}
        self.called: list[ToolBinding] = []

    def snapshot(self):
        return self.version, self.bindings.copy()

    async def call_binding(self, binding: ToolBinding, arguments: dict[str, Any]) -> str:
        self.called.append(binding)
        return json.dumps({"ok": True})


async def noop_audio(chunk: bytes) -> None:
    return None


async def noop_event(event: dict[str, Any]) -> None:
    return None


async def test_tool_timeout_is_returned_to_realtime_model() -> None:
    session = RealtimeSession(
        None,  # type: ignore[arg-type]
        Settings(openai_api_key="test", tool_timeout_seconds=0.001),
        SlowBroker(),  # type: ignore[arg-type]
        noop_audio,
        noop_event,
    )
    websocket = RecordingWebSocket()
    session.ws = websocket  # type: ignore[assignment]

    await session._call_tool(
        {
            "name": "mcp_slow_wait",
            "call_id": "call-timeout",
            "arguments": "{}",
        },
        ToolBinding(
            public_name="mcp_slow_wait",
            server_name="slow",
            remote_name="wait",
            description="Wait forever",
            schema={"type": "object", "properties": {}},
        ),
    )

    output = json.loads(websocket.events[0]["item"]["output"])
    assert output["error"] == "TimeoutError"
    assert websocket.events[1] == {"type": "response.create"}


async def test_catalog_sync_preserves_binding_for_active_tool_call() -> None:
    original = ToolBinding(
        public_name="mcp_homeassistant_light",
        server_name="homeassistant",
        remote_name="old_light",
        description="Old binding",
        schema={"type": "object", "properties": {}},
    )
    replacement = ToolBinding(
        public_name="mcp_homeassistant_light",
        server_name="homeassistant",
        remote_name="new_light",
        description="New binding",
        schema={"type": "object", "properties": {}},
    )
    broker = MutableBroker(original)
    session = RealtimeSession(
        None,  # type: ignore[arg-type]
        Settings(openai_api_key="test"),
        broker,  # type: ignore[arg-type]
        noop_audio,
        noop_event,
    )
    websocket = RecordingWebSocket()
    session.ws = websocket  # type: ignore[assignment]
    await session.sync_tools(force=True)
    active_binding = session.tool_bindings[original.public_name]

    broker.version = 2
    broker.bindings = {replacement.public_name: replacement}
    await session._call_tool(
        {
            "name": original.public_name,
            "call_id": "call-active",
            "arguments": "{}",
        },
        active_binding,
    )
    await session.sync_tools()

    assert broker.called == [original]
    assert session.catalog_version == 2
    assert session.tool_bindings[replacement.public_name] is replacement
    assert websocket.events[-1]["session"]["tools"][0]["description"] == "New binding"
