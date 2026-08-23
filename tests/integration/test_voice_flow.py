from __future__ import annotations

import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
import uvicorn
from aiohttp import WSMsgType, WSServerHandshakeError, web
from aiohttp.test_utils import TestClient, TestServer
from app.config import McpServerConfig, Settings
from app.server import create_app
from mcp.server.fastmcp import FastMCP

pytestmark = pytest.mark.integration


@dataclass
class FakeServices:
    openai_events: list[dict[str, Any]] = field(default_factory=list)
    tool_outputs: list[dict[str, Any]] = field(default_factory=list)
    play_calls: list[dict[str, Any]] = field(default_factory=list)
    held_response: asyncio.Event = field(default_factory=asyncio.Event)

    async def realtime(self, request: web.Request) -> web.WebSocketResponse:
        assert request.headers["Authorization"] == "Bearer test-key"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        audio = bytearray()
        awaiting_tool = False
        tool_completed = False
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            self.openai_events.append(event)
            if event["type"] == "input_audio_buffer.clear":
                audio.clear()
            elif event["type"] == "input_audio_buffer.append":
                audio.extend(base64.b64decode(event["audio"]))
            elif event["type"] == "input_audio_buffer.commit" and audio == b"use-tool":
                awaiting_tool = True
                await ws.send_json(
                    {
                        "type": "response.function_call_arguments.done",
                        "call_id": "call-1",
                        "name": "mcp_homeassistant_get_light",
                        "arguments": '{"entity_id":"light.kitchen"}',
                    }
                )
            elif event["type"] == "input_audio_buffer.commit" and audio == b"hold-response":
                await ws.send_json({"type": "response.created"})
                self.held_response.set()
            elif event["type"] == "input_audio_buffer.commit":
                await self._send_response(ws, b"browser audio", "Hello from the fake model")
            elif event["type"] == "conversation.item.create":
                self.tool_outputs.append(event)
                tool_completed = True
            elif event["type"] == "response.create" and awaiting_tool and tool_completed:
                await self._send_response(ws, b"speaker audio", "The kitchen light is on")
                awaiting_tool = False
                tool_completed = False
        return ws

    async def _send_response(
        self, ws: web.WebSocketResponse, audio: bytes, transcript: str
    ) -> None:
        await ws.send_json({"type": "response.created"})
        await ws.send_json({"type": "response.output_audio_transcript.delta", "delta": transcript})
        await ws.send_json(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()}
        )
        await ws.send_json({"type": "response.output_audio.done"})
        await ws.send_json({"type": "response.done"})

    async def states(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer supervisor-token"
        return web.json_response(
            [
                {
                    "entity_id": "media_player.sonos_beam",
                    "attributes": {"friendly_name": "Sonos Beam"},
                },
                {"entity_id": "light.kitchen", "attributes": {}},
            ]
        )

    async def play_media(self, request: web.Request) -> web.Response:
        self.play_calls.append(await request.json())
        return web.json_response([])


@asynccontextmanager
async def run_aiohttp_app(app: web.Application) -> AsyncIterator[TestServer]:
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


@asynccontextmanager
async def run_mcp_server(transport: str = "streamable_http") -> AsyncIterator[str]:
    mcp = FastMCP("Fake Home Assistant", stateless_http=True, json_response=True)

    @mcp.tool()
    def get_light(entity_id: str) -> dict[str, str]:
        """Read a fake exposed light."""
        return {"entity_id": entity_id, "state": "on"}

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    if transport == "sse":
        app = mcp.sse_app()
        path = "/sse"
    else:
        app = mcp.streamable_http_app()
        path = "/mcp"
    config = uvicorn.Config(app, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
        await asyncio.sleep(0)
    try:
        yield f"http://127.0.0.1:{port}{path}"
    finally:
        server.should_exit = True
        await task


async def receive_type(ws: Any, event_type: str) -> Any:
    async with asyncio.timeout(5):
        while True:
            message = await ws.receive()
            if message.type == WSMsgType.BINARY and event_type == "audio":
                return message.data
            if message.type == WSMsgType.TEXT:
                event = json.loads(message.data)
                if event.get("type") == event_type:
                    return event


async def start_voice_client(client: TestClient, client_id: str) -> Any:
    ws = await client.ws_connect("/ws", headers={"X-Ingress-Path": "/test"})
    await ws.send_json(
        {"type": "hello", "protocol": 1, "client_id": client_id, "name": "Integration test"}
    )
    assert (await receive_type(ws, "session_ready"))["client_id"] == client_id
    return ws


class FakeProgressiveEncoder:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def write(self, chunk: bytes) -> None:
        await self.queue.put(b"progressive:" + chunk)

    async def chunks(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def finish(self) -> None:
        await self.queue.put(None)

    async def cancel(self) -> None:
        await self.queue.put(None)


@pytest.mark.asyncio
async def test_complete_browser_and_mcp_assisted_speaker_turns(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)
    external.router.add_get("/api/states", services.states)
    external.router.add_post("/api/services/media_player/play_media", services.play_media)

    async def fake_encode(pcm: bytes) -> bytes:
        return b"fake-mp3:" + pcm

    monkeypatch.setattr("app.server.encode_mp3", fake_encode)
    monkeypatch.setattr("app.server.ProgressiveMp3Encoder", FakeProgressiveEncoder)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")

    async with (
        run_aiohttp_app(external) as external_server,
        run_mcp_server() as mcp_url,
        run_mcp_server("sse") as sse_url,
    ):
        base_url = str(external_server.make_url("")).rstrip("/")
        settings = Settings(
            openai_api_key="test-key",
            openai_realtime_url=f"{base_url}/v1/realtime",
            ha_api_url=base_url,
            speaker_base_url="http://voice.test:8099",
            routes_path=str(tmp_path / "routes.json"),
            mcp_servers=(
                McpServerConfig(name="homeassistant", url=mcp_url),
                McpServerConfig(
                    name="legacy",
                    url=sse_url,
                    transport="sse",
                    allowed_tools=frozenset({"get_light"}),
                ),
            ),
        )
        voice_app = create_app(settings)
        async with TestClient(TestServer(voice_app)) as client:
            browser = await start_voice_client(client, "browser-client")
            await browser.send_json({"type": "ptt_start"})
            await browser.send_bytes(b"plain-turn")
            await browser.send_json({"type": "ptt_stop"})
            assert await receive_type(browser, "audio") == b"browser audio"
            assert (await receive_type(browser, "response.done"))["type"] == "response.done"

            speaker = await start_voice_client(client, "speaker-client")
            await speaker.send_json(
                {
                    "type": "route_set",
                    "route": {
                        "sink": "media_player",
                        "entity_id": "media_player.sonos_beam",
                        "mode": "buffered",
                        "announce": True,
                        "volume": None,
                    },
                }
            )
            await receive_type(speaker, "route")
            await speaker.send_json({"type": "speakers_list"})
            speakers = await receive_type(speaker, "speakers")
            assert speakers["items"] == [
                {"entity_id": "media_player.sonos_beam", "name": "Sonos Beam"}
            ]
            await speaker.send_json({"type": "ptt_start"})
            await speaker.send_bytes(b"use-tool")
            await speaker.send_json({"type": "ptt_stop"})
            await receive_type(speaker, "response.done")

            async with asyncio.timeout(5):
                while not services.play_calls:
                    await asyncio.sleep(0.01)
            assert services.play_calls[0]["entity_id"] == "media_player.sonos_beam"
            assert services.play_calls[0]["announce"] is True
            assert services.tool_outputs
            output = json.loads(services.tool_outputs[0]["item"]["output"])
            assert output["content"][0]["text"].find("light.kitchen") >= 0

            token = services.play_calls[0]["media_content_id"].rsplit("/", 1)[-1]
            response = await client.get(f"/media/{token}")
            assert response.status == 200
            assert await response.read() == b"fake-mp3:speaker audio"

            overlap = await start_voice_client(client, "overlap-client")
            progressive = await start_voice_client(client, "progressive-client")
            session_update = next(
                event for event in services.openai_events if event["type"] == "session.update"
            )
            tool_names = {tool["name"] for tool in session_update["session"]["tools"]}
            assert tool_names == {
                "mcp_homeassistant_get_light",
                "mcp_legacy_get_light",
            }

            with pytest.raises(WSServerHandshakeError) as rejected:
                await client.ws_connect("/ws", headers={"X-Ingress-Path": "/test"})
            assert rejected.value.status == 503

            await progressive.send_json(
                {
                    "type": "route_set",
                    "route": {
                        "sink": "media_player",
                        "entity_id": "media_player.sonos_beam",
                        "mode": "progressive",
                        "announce": False,
                        "volume": None,
                    },
                }
            )
            await receive_type(progressive, "route")
            await progressive.send_json({"type": "ptt_start"})
            await progressive.send_bytes(b"plain-turn")
            await progressive.send_json({"type": "ptt_stop"})

            await overlap.send_json({"type": "ptt_start"})
            await overlap.send_bytes(b"use-tool")
            await overlap.send_json({"type": "ptt_stop"})
            await browser.send_json({"type": "ptt_start"})
            await browser.send_bytes(b"use-tool")
            await browser.send_json({"type": "ptt_stop"})
            await asyncio.gather(
                receive_type(progressive, "response.done"),
                receive_type(overlap, "response.done"),
                receive_type(browser, "response.done"),
            )
            assert len(services.tool_outputs) == 3

            await overlap.send_json({"type": "ptt_start"})
            await overlap.send_bytes(b"hold-response")
            await overlap.send_json({"type": "ptt_stop"})
            await asyncio.wait_for(services.held_response.wait(), timeout=5)
            async with asyncio.timeout(5):
                while not any(session.response_active for session in voice_app["voice"].sessions):
                    await asyncio.sleep(0.01)
            await overlap.send_json({"type": "ptt_start"})
            async with asyncio.timeout(5):
                while not any(
                    event["type"] == "response.cancel" for event in services.openai_events
                ):
                    await asyncio.sleep(0.01)

            async with asyncio.timeout(5):
                while len(services.play_calls) < 2:
                    await asyncio.sleep(0.01)
            progressive_token = services.play_calls[1]["media_content_id"].rsplit("/", 1)[-1]
            progressive_response = await client.get(f"/media/{progressive_token}")
            assert progressive_response.status == 200
            assert await progressive_response.read() == b"progressive:browser audio"

            await browser.close()
            async with asyncio.timeout(5):
                while len(voice_app["voice"].sessions) >= 4:
                    await asyncio.sleep(0.01)
            replacement = await start_voice_client(client, "replacement-client")
            await replacement.close()
            await speaker.close()
            await overlap.close()
            await progressive.close()
