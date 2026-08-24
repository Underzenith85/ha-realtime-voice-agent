from __future__ import annotations

import asyncio
import base64
import hashlib
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
from app.config import HardwareClientConfig, McpServerConfig, Settings
from app.server import create_app
from mcp.server.fastmcp import FastMCP

pytestmark = pytest.mark.integration


@dataclass
class FakeServices:
    openai_events: list[dict[str, Any]] = field(default_factory=list)
    tool_outputs: list[dict[str, Any]] = field(default_factory=list)
    play_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)
    held_response: asyncio.Event = field(default_factory=asyncio.Event)
    openai_connections: list[list[dict[str, Any]]] = field(default_factory=list)
    fail_next_play: bool = False
    reject_with_quota_error: bool = False

    async def realtime(self, request: web.Request) -> web.WebSocketResponse:
        assert request.headers["Authorization"] == "Bearer test-key"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        audio = bytearray()
        connection_events: list[dict[str, Any]] = []
        self.openai_connections.append(connection_events)
        awaiting_tool = False
        tool_completed = False
        timer_completion = False
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            self.openai_events.append(event)
            connection_events.append(event)
            if event["type"] == "session.update" and self.reject_with_quota_error:
                await ws.send_json(
                    {
                        "type": "error",
                        "error": {
                            "type": "insufficient_quota",
                            "code": "insufficient_quota",
                            "message": "You have no credits remaining.",
                        },
                    }
                )
                await ws.close()
                break
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
            elif event["type"] == "input_audio_buffer.commit" and audio == b"disconnect":
                await ws.close()
                break
            elif event["type"] == "input_audio_buffer.commit":
                await ws.send_json(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": audio.decode(),
                    }
                )
                await self._send_response(ws, b"browser audio", "Hello from the fake model")
            elif (
                event["type"] == "conversation.item.create"
                and event.get("item", {}).get("type") == "function_call_output"
            ):
                self.tool_outputs.append(event)
                tool_completed = True
            elif (
                event["type"] == "conversation.item.create"
                and event.get("item", {}).get("type") == "message"
                and "has completed" in event["item"]["content"][0].get("text", "")
            ):
                timer_completion = True
            elif event["type"] == "response.create" and awaiting_tool and tool_completed:
                await self._send_response(ws, b"speaker audio", "The kitchen light is on")
                awaiting_tool = False
                tool_completed = False
            elif event["type"] == "response.create" and timer_completion:
                await self._send_response(ws, b"timer audio", "Your timer is complete")
                timer_completion = False
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
        if self.fail_next_play:
            self.fail_next_play = False
            raise web.HTTPInternalServerError(text="simulated player rejection")
        return web.json_response([])

    async def stop_media(self, request: web.Request) -> web.Response:
        self.stop_calls.append(await request.json())
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
async def run_mcp_server(
    transport: str = "streamable_http",
) -> AsyncIterator[McpTestServer]:
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
        yield McpTestServer(f"http://127.0.0.1:{port}{path}", mcp)
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


async def start_device_client(client: TestClient, client_id: str, token: str) -> Any:
    ws = await client.ws_connect("/device/ws", headers={"Authorization": f"Bearer {token}"})
    await ws.send_json(
        {
            "type": "hello",
            "protocol": 1,
            "client_id": client_id,
            "client_type": "voice_pe",
            "name": "Integration Voice PE",
        }
    )
    assert (await receive_type(ws, "session_ready"))["client_id"] == client_id
    return ws


@pytest.mark.asyncio
async def test_authenticated_voice_pe_and_browser_run_concurrently(
    tmp_path: Any,
) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)
    token = "one-time-generated-device-secret"
    client_id = "kitchen-voice-pe"

    async with run_aiohttp_app(external) as external_server:
        base_url = str(external_server.make_url("")).rstrip("/")
        settings = Settings(
            openai_api_key="test-key",
            openai_realtime_url=f"{base_url}/v1/realtime",
            ha_api_url=base_url,
            routes_path=str(tmp_path / "routes.json"),
            timers_path=str(tmp_path / "timers.json"),
            hardware_clients=(
                HardwareClientConfig(
                    client_id=client_id,
                    name="Kitchen Voice PE",
                    token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                ),
            ),
        )
        app = create_app(settings)
        async with TestClient(TestServer(app)) as client:
            with pytest.raises(WSServerHandshakeError) as missing:
                await client.ws_connect("/device/ws")
            assert missing.value.status == 401
            with pytest.raises(WSServerHandshakeError) as invalid:
                await client.ws_connect(
                    "/device/ws", headers={"Authorization": "Bearer revoked-token"}
                )
            assert invalid.value.status == 401

            mismatched = await client.ws_connect(
                "/device/ws", headers={"Authorization": f"Bearer {token}"}
            )
            await mismatched.send_json(
                {
                    "type": "hello",
                    "protocol": 1,
                    "client_id": "other-device",
                    "client_type": "voice_pe",
                }
            )
            assert (await mismatched.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

            device = await start_device_client(client, client_id, token)
            browser = await start_voice_client(client, "browser-alongside-device")
            assert app["voice"].routes.contains(client_id)
            assert app["voice"].routes.get(client_id).sink == "browser"

            await device.send_json({"type": "ptt_start"})
            await device.send_bytes(b"device-turn")
            await device.send_json({"type": "ptt_stop"})
            await browser.send_json({"type": "ptt_start"})
            await browser.send_bytes(b"browser-turn")
            await browser.send_json({"type": "ptt_stop"})

            assert await receive_type(device, "audio") == b"browser audio"
            assert await receive_type(browser, "audio") == b"browser audio"
            await receive_type(device, "response.done")
            await receive_type(browser, "response.done")
            await device.close()
            await browser.close()


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


@dataclass(frozen=True)
class McpTestServer:
    url: str
    mcp: FastMCP


@pytest.mark.asyncio
async def test_complete_browser_and_mcp_assisted_speaker_turns(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)
    external.router.add_get("/api/states", services.states)
    external.router.add_post("/api/services/media_player/play_media", services.play_media)
    external.router.add_post("/api/services/media_player/media_stop", services.stop_media)

    async def fake_encode(pcm: bytes) -> bytes:
        return b"fake-mp3:" + pcm

    monkeypatch.setattr("app.server.encode_mp3", fake_encode)
    monkeypatch.setattr("app.server.ProgressiveMp3Encoder", FakeProgressiveEncoder)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    unavailable_socket = socket.socket()
    unavailable_socket.bind(("127.0.0.1", 0))
    unavailable_port = unavailable_socket.getsockname()[1]
    unavailable_socket.close()

    async with (
        run_aiohttp_app(external) as external_server,
        run_mcp_server() as mcp_server,
        run_mcp_server("sse") as sse_server,
    ):
        base_url = str(external_server.make_url("")).rstrip("/")
        settings = Settings(
            openai_api_key="test-key",
            openai_realtime_url=f"{base_url}/v1/realtime",
            ha_api_url=base_url,
            speaker_base_url="http://voice.test:8099",
            routes_path=str(tmp_path / "routes.json"),
            mcp_servers=(
                McpServerConfig(name="homeassistant", url=mcp_server.url),
                McpServerConfig(
                    name="legacy",
                    url=sse_server.url,
                    transport="sse",
                    allowed_tools=frozenset({"get_light"}),
                ),
                McpServerConfig(
                    name="offline",
                    url=f"http://127.0.0.1:{unavailable_port}/mcp",
                    allowed_tools=frozenset({"missing"}),
                ),
            ),
        )
        voice_app = create_app(settings)
        async with TestClient(TestServer(voice_app)) as client:
            status = voice_app["voice"].broker.status()
            assert status["tool_count"] == 2
            assert (
                next(server for server in status["servers"] if server["name"] == "offline")[
                    "status"
                ]
                == "unavailable"
            )
            browser = await start_voice_client(client, "browser-client")
            await browser.send_json({"type": "tools_refresh"})
            refreshed = await receive_type(browser, "mcp_status")
            assert refreshed["mcp"]["tool_count"] == 2
            assert refreshed["mcp"]["catalog_version"] >= 1
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
            assert services.play_calls[0]["media_content_type"] == "music"
            assert services.play_calls[0]["announce"] is True
            assert services.tool_outputs
            output = json.loads(services.tool_outputs[0]["item"]["output"])
            assert output["content"][0]["text"].find("light.kitchen") >= 0

            token = services.play_calls[0]["media_content_id"].rsplit("/", 1)[-1]
            probe = await client.head(f"/media/{token}")
            assert probe.status == 200
            assert probe.headers["Content-Type"] == "audio/mpeg"
            response = await client.get(f"/media/{token}")
            assert response.status == 200
            assert await response.read() == b"fake-mp3:speaker audio"
            assert (await client.get(f"/media/{token}")).status == 404

            await speaker.send_json(
                {
                    "type": "route_test",
                    "route": {
                        "sink": "media_player",
                        "entity_id": "media_player.sonos_beam",
                        "mode": "buffered",
                        "announce": True,
                        "volume": None,
                    },
                }
            )
            route_test = await receive_type(speaker, "route_test_result")
            assert route_test["ok"] is True
            assert len(services.play_calls) == 2
            assert services.stop_calls[-1]["entity_id"] == "media_player.sonos_beam"

            overlap = await start_voice_client(client, "overlap-client")
            progressive = await start_voice_client(client, "progressive-client")
            session_update = next(
                event for event in services.openai_events if event["type"] == "session.update"
            )
            tool_names = {tool["name"] for tool in session_update["session"]["tools"]}
            assert {name for name in tool_names if name.startswith("mcp_")} == {
                "mcp_homeassistant_get_light",
                "mcp_legacy_get_light",
            }
            assert "voice_timer_create" in tool_names

            @mcp_server.mcp.tool()
            def newly_added() -> str:
                """A tool added while voice sessions are active."""
                return "available"

            await browser.send_json({"type": "tools_refresh"})
            added_status = await receive_type(browser, "mcp_status")
            assert added_status["mcp"]["tool_count"] == 3
            latest_update = [
                event for event in services.openai_events if event["type"] == "session.update"
            ][-1]
            assert "mcp_homeassistant_newly_added" in {
                tool["name"] for tool in latest_update["session"]["tools"]
            }

            mcp_server.mcp.remove_tool("newly_added")
            await browser.send_json({"type": "tools_refresh"})
            removed_status = await receive_type(browser, "mcp_status")
            assert removed_status["mcp"]["tool_count"] == 2
            latest_update = [
                event for event in services.openai_events if event["type"] == "session.update"
            ][-1]
            assert "mcp_homeassistant_newly_added" not in {
                tool["name"] for tool in latest_update["session"]["tools"]
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
                while len(services.play_calls) < 3:
                    await asyncio.sleep(0.01)
            progressive_token = services.play_calls[2]["media_content_id"].rsplit("/", 1)[-1]
            progressive_response = await client.get(f"/media/{progressive_token}")
            assert progressive_response.status == 200
            assert await progressive_response.read() == b"progressive:browser audio"

            services.fail_next_play = True
            await progressive.send_json({"type": "ptt_start"})
            await progressive.send_bytes(b"plain-turn")
            await progressive.send_json({"type": "ptt_stop"})
            failed_progressive = await receive_type(progressive, "playback_status")
            assert failed_progressive["ok"] is False
            assert failed_progressive["fallback"] == "buffered"
            buffered_fallback = await receive_type(progressive, "playback_status")
            assert buffered_fallback["mode"] == "buffered"
            assert buffered_fallback["fallback_used"] is True
            await receive_type(progressive, "response.done")
            fallback_token = services.play_calls[-1]["media_content_id"].rsplit("/", 1)[-1]
            fallback_response = await client.get(f"/media/{fallback_token}")
            assert await fallback_response.read() == b"fake-mp3:browser audio"

            await browser.close()
            async with asyncio.timeout(5):
                while len(voice_app["voice"].sessions) >= 4:
                    await asyncio.sleep(0.01)
            replacement = await start_voice_client(client, "replacement-client")
            await replacement.close()
            await speaker.close()
            await overlap.close()
            await progressive.close()


@pytest.mark.asyncio
async def test_timer_completion_uses_client_route_and_falls_back_to_browser(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)
    external.router.add_post("/api/services/media_player/play_media", services.play_media)
    external.router.add_post("/api/services/media_player/media_stop", services.stop_media)

    async def fake_encode(pcm: bytes) -> bytes:
        return b"fake-mp3:" + pcm

    monkeypatch.setattr("app.server.encode_mp3", fake_encode)
    async with run_aiohttp_app(external) as external_server:
        base_url = str(external_server.make_url("")).rstrip("/")
        app = create_app(
            Settings(
                openai_api_key="test-key",
                openai_realtime_url=f"{base_url}/v1/realtime",
                ha_api_url=base_url,
                speaker_base_url="http://voice.test:8099",
                routes_path=str(tmp_path / "routes.json"),
                timers_path=str(tmp_path / "timers.json"),
            )
        )
        async with TestClient(TestServer(app)) as client:
            ws = await start_voice_client(client, "timer-client")
            await ws.send_json(
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
            await receive_type(ws, "route")
            app["voice"].timers.create("timer-client", 1, "Tea")
            async with asyncio.timeout(5):
                while not services.play_calls:
                    await asyncio.sleep(0.01)
            token = services.play_calls[-1]["media_content_id"].rsplit("/", 1)[-1]
            response = await client.get(f"/media/{token}")
            assert await response.read() == b"fake-mp3:timer audio"

            services.fail_next_play = True
            app["voice"].timers.create("timer-client", 1, "Fallback")
            assert await receive_type(ws, "audio") == b"timer audio"
            fallback = await receive_type(ws, "playback_status")
            assert fallback["mode"] == "browser"
            await ws.close()


@pytest.mark.asyncio
async def test_reconnect_restores_context_and_reports_diagnostics(tmp_path: Any) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)

    async with run_aiohttp_app(external) as external_server:
        base_url = str(external_server.make_url("")).rstrip("/")
        app = create_app(
            Settings(
                openai_api_key="test-key",
                openai_realtime_url=f"{base_url}/v1/realtime",
                routes_path=str(tmp_path / "routes.json"),
            )
        )
        async with TestClient(TestServer(app)) as client:
            ws = await start_voice_client(client, "recovering-client")
            await ws.send_json({"type": "ptt_start"})
            await ws.send_bytes(b"remember me")
            await ws.send_json({"type": "ptt_stop"})
            await receive_type(ws, "response.done")

            await ws.send_json({"type": "ptt_start"})
            await ws.send_bytes(b"disconnect")
            await ws.send_json({"type": "ptt_stop"})
            assert (await receive_type(ws, "session.reconnected"))["reconnects"] == 1
            await ws.send_json({"type": "diagnostics_get"})
            diagnostics = await receive_type(ws, "session_diagnostics")
            assert diagnostics["session_count"] == 1
            assert diagnostics["session"]["reconnects"] == 1
            assert diagnostics["session"]["history_turns"] == 2

            restored = services.openai_connections[1]
            restored_messages = [
                event["item"]
                for event in restored
                if event.get("type") == "conversation.item.create"
                and event.get("item", {}).get("type") == "message"
            ]
            assert restored_messages[0]["content"][0]["text"] == "remember me"
            assert restored_messages[1]["content"][0]["text"] == "Hello from the fake model"

            await ws.send_json({"type": "conversation_reset"})
            assert (await receive_type(ws, "conversation_reset"))["ok"] is True
            await ws.send_json({"type": "diagnostics_get"})
            reset_diagnostics = await receive_type(ws, "session_diagnostics")
            assert reset_diagnostics["session"]["history_turns"] == 0
            await ws.close()


@pytest.mark.asyncio
async def test_quota_error_is_terminal_and_does_not_reconnect(tmp_path: Any) -> None:
    services = FakeServices(reject_with_quota_error=True)
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)

    async with run_aiohttp_app(external) as external_server:
        base_url = str(external_server.make_url("")).rstrip("/")
        app = create_app(
            Settings(
                openai_api_key="test-key",
                openai_realtime_url=f"{base_url}/v1/realtime",
                routes_path=str(tmp_path / "routes.json"),
            )
        )
        async with TestClient(TestServer(app)) as client:
            ws = await start_voice_client(client, "quota-client")
            error = await receive_type(ws, "error")
            assert error["error"] == {
                "type": "insufficient_quota",
                "code": "insufficient_quota",
                "message": "You have no credits remaining.",
            }
            await asyncio.sleep(0.1)
            assert len(services.openai_connections) == 1
            await ws.close()


@pytest.mark.asyncio
async def test_idle_expiration_and_four_session_history_isolation(tmp_path: Any) -> None:
    services = FakeServices()
    external = web.Application()
    external.router.add_get("/v1/realtime", services.realtime)

    async with run_aiohttp_app(external) as external_server:
        base_url = str(external_server.make_url("")).rstrip("/")
        app = create_app(
            Settings(
                openai_api_key="test-key",
                openai_realtime_url=f"{base_url}/v1/realtime",
                routes_path=str(tmp_path / "routes.json"),
                idle_timeout_seconds=1,
            )
        )
        async with TestClient(TestServer(app)) as client:
            clients = [await start_voice_client(client, f"client-{index}") for index in range(4)]
            for index, ws in enumerate(clients):
                await ws.send_json({"type": "ptt_start"})
                await ws.send_bytes(f"unique-{index}".encode())
                await ws.send_json({"type": "ptt_stop"})
                await receive_type(ws, "response.done")

            histories = {
                session.client_id: session.history.current.user
                for session in app["voice"].sessions
                if session.history.current
            }
            assert histories == {f"client-{index}": f"unique-{index}" for index in range(4)}

            expired = await asyncio.gather(*(receive_type(ws, "session.expired") for ws in clients))
            assert all(event["reason"] == "idle_timeout" for event in expired)
            await asyncio.gather(*(ws.close() for ws in clients))
            async with asyncio.timeout(5):
                while app["voice"]._session_count:
                    await asyncio.sleep(0.01)
            assert app["voice"].idle_expirations == 4
