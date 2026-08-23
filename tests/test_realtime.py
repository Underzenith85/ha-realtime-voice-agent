import asyncio
import json
from typing import Any

from app.config import Settings
from app.mcp_broker import ToolBinding
from app.realtime import ConversationHistory, RealtimeSession, ToolRecord


class SlowBroker:
    async def call_binding(
        self, binding: ToolBinding, arguments: dict[str, Any], *, client_id: str | None = None
    ) -> str:
        await asyncio.sleep(60)
        return "unreachable"


class RecordingWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False

    async def send_json(self, event: dict[str, Any]) -> None:
        self.events.append(event)


class MutableBroker:
    def __init__(self, binding: ToolBinding) -> None:
        self.version = 1
        self.bindings = {binding.public_name: binding}
        self.called: list[ToolBinding] = []

    def snapshot(self):
        return self.version, self.bindings.copy()

    async def call_binding(
        self, binding: ToolBinding, arguments: dict[str, Any], *, client_id: str | None = None
    ) -> str:
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
    session._connected.set()

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
    session._connected.set()
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


def test_conversation_history_is_bounded_and_restorable() -> None:
    history = ConversationHistory(20)
    for index in range(25):
        history.begin_turn()
        history.set_user(f"user {index}")
        history.add_tool(ToolRecord("lookup", f"call-{index}", "{}", '{"ok":true}'))
        history.append_assistant(f"assistant {index}")

    events = history.restore_events()

    assert len(history) == 20
    assert events[0]["item"]["content"][0]["text"] == "user 5"
    assert events[-1]["item"]["content"][0]["text"] == "assistant 24"
    assert len(events) == 80


class ConcurrentBroker(MutableBroker):
    def __init__(self, binding: ToolBinding) -> None:
        super().__init__(binding)
        self.active = 0
        self.max_active = 0

    async def call_binding(
        self, binding: ToolBinding, arguments: dict[str, Any], *, client_id: str | None = None
    ) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return json.dumps(arguments)


async def test_tool_calls_are_limited_to_four_and_correlated() -> None:
    binding = ToolBinding(
        public_name="mcp_test_lookup",
        server_name="test",
        remote_name="lookup",
        description="Lookup",
        schema={"type": "object", "properties": {}},
    )
    broker = ConcurrentBroker(binding)
    session = RealtimeSession(
        None,  # type: ignore[arg-type]
        Settings(openai_api_key="test"),
        broker,  # type: ignore[arg-type]
        noop_audio,
        noop_event,
    )
    websocket = RecordingWebSocket()
    session.ws = websocket  # type: ignore[assignment]
    session._connected.set()
    session.begin_turn()

    await asyncio.gather(
        *(
            session._call_tool(
                {
                    "name": binding.public_name,
                    "call_id": f"call-{index}",
                    "arguments": json.dumps({"index": index}),
                },
                binding,
            )
            for index in range(8)
        )
    )

    outputs = [
        event["item"]["call_id"]
        for event in websocket.events
        if event.get("type") == "conversation.item.create"
    ]
    assert broker.max_active == 4
    assert set(outputs) == {f"call-{index}" for index in range(8)}
