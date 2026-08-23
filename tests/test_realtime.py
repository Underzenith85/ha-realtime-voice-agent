import asyncio
import json
from typing import Any

from app.config import Settings
from app.realtime import RealtimeSession


class SlowBroker:
    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        await asyncio.sleep(60)
        return "unreachable"


class RecordingWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, event: dict[str, Any]) -> None:
        self.events.append(event)


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
        }
    )

    output = json.loads(websocket.events[0]["item"]["output"])
    assert output["error"] == "TimeoutError"
    assert websocket.events[1] == {"type": "response.create"}
