"""HTTP, browser WebSocket, media, and session orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from app.config import Settings
from app.encoder import ProgressiveMp3Encoder, encode_mp3
from app.mcp_broker import McpBroker
from app.media import MediaObject, MediaStore
from app.protocol import parse_hello
from app.realtime import RealtimeSession
from app.routes import OutputRoute, RouteStore
from app.speakers import SpeakerController

LOGGER = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).parent / "web"


@web.middleware
async def ingress_or_media_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """Keep the control UI ingress-only while allowing signed speaker media pulls."""
    if not request.path.startswith("/media/") and "X-Ingress-Path" not in request.headers:
        raise web.HTTPForbidden(text="Open this interface through Home Assistant ingress")
    return await handler(request)


class VoiceServer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.routes = RouteStore(settings.routes_path)
        self.routes.load()
        self.media = MediaStore()
        self.broker = McpBroker(settings.mcp_servers)
        self.sessions: set[RealtimeSession] = set()
        self.http = None
        self.speakers = None

    async def start(self, app: web.Application) -> None:
        import aiohttp

        self.http = aiohttp.ClientSession()
        token = os.getenv("SUPERVISOR_TOKEN", "")
        self.speakers = SpeakerController(self.http, self.settings.ha_api_url, token)
        await self.broker.start()

    async def stop(self, app: web.Application) -> None:
        await asyncio.gather(*(session.close() for session in tuple(self.sessions)))
        await self.broker.close()
        if self.http:
            await self.http.close()

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        if len(self.sessions) >= self.settings.max_sessions:
            raise web.HTTPServiceUnavailable(text="session limit reached")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=512 * 1024)
        await ws.prepare(request)
        first = await ws.receive_json()
        hello = parse_hello(first)
        route = self.routes.get(hello.client_id)
        pcm = bytearray()
        progressive: ProgressiveMp3Encoder | None = None
        progressive_item: MediaObject | None = None
        progressive_reader: asyncio.Task[None] | None = None

        async def on_audio(chunk: bytes) -> None:
            nonlocal progressive, progressive_item, progressive_reader
            current = self.routes.get(hello.client_id)
            if current.sink == "browser":
                await ws.send_bytes(chunk)
                return
            if current.mode == "buffered":
                pcm.extend(chunk)
                return
            if progressive is None:
                progressive = ProgressiveMp3Encoder()
                await progressive.start()
                token, progressive_item = self.media.create()

                async def pump() -> None:
                    assert progressive and progressive_item
                    async for encoded in progressive.chunks():
                        self.media.append(progressive_item, encoded)
                    self.media.finish(progressive_item)

                progressive_reader = asyncio.create_task(pump())
                await self._play(current, request, token)
            await progressive.write(chunk)

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal progressive, progressive_item, progressive_reader
            if event["type"] == "response.output_audio.done":
                current = self.routes.get(hello.client_id)
                if current.sink == "media_player" and current.mode == "buffered" and pcm:
                    encoded = await encode_mp3(bytes(pcm))
                    pcm.clear()
                    token, item = self.media.create()
                    self.media.append(item, encoded)
                    self.media.finish(item)
                    await self._play(current, request, token)
                elif progressive:
                    await progressive.finish()
                    if progressive_reader:
                        await progressive_reader
                    progressive = None
                    progressive_item = None
                    progressive_reader = None
            safe = {key: event[key] for key in ("type", "delta") if key in event}
            await ws.send_json(safe)

        assert self.http
        realtime = RealtimeSession(self.http, self.settings, self.broker, on_audio, on_event)
        self.sessions.add(realtime)
        try:
            await realtime.start()
            await ws.send_json(
                {"type": "session_ready", "client_id": hello.client_id, "route": asdict(route)}
            )
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    await realtime.append_audio(message.data)
                    continue
                if message.type != WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                if event.get("type") == "ptt_start":
                    was_responding = realtime.response_active
                    await realtime.cancel()
                    if was_responding and route.entity_id and self.speakers:
                        await self.speakers.stop(route.entity_id)
                elif event.get("type") == "ptt_stop":
                    await realtime.commit()
                elif event.get("type") == "cancel":
                    await realtime.cancel()
                elif event.get("type") == "route_set":
                    route = OutputRoute(**event["route"]).validate()
                    self.routes.set(hello.client_id, route)
                    await ws.send_json({"type": "route", "route": asdict(route)})
                elif event.get("type") == "speakers_list" and self.speakers:
                    await ws.send_json(
                        {"type": "speakers", "items": await self.speakers.list_speakers()}
                    )
        finally:
            await realtime.close()
            self.sessions.discard(realtime)
            await ws.close()
        return ws

    async def _play(self, route: OutputRoute, request: web.Request, token: str) -> None:
        assert self.speakers
        url = f"{self.settings.speaker_base_url.rstrip('/')}/media/{token}"
        await self.speakers.play(route, url)

    async def media_stream(self, request: web.Request) -> web.StreamResponse:
        item = self.media.get(request.match_info["token"])
        if item is None:
            raise web.HTTPNotFound()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-store"},
        )
        if item.complete.is_set():
            body = b"".join(item.chunks)
            return web.Response(
                body=body,
                content_type="audio/mpeg",
                headers={"Cache-Control": "no-store"},
            )
        await response.prepare(request)
        for chunk in item.chunks:
            await response.write(chunk)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        item.subscribers.add(queue)
        try:
            while (chunk := await queue.get()) is not None:
                await response.write(chunk)
        finally:
            item.subscribers.discard(queue)
        await response.write_eof()
        return response


def create_app(settings: Settings) -> web.Application:
    service = VoiceServer(settings)
    app = web.Application(middlewares=[ingress_or_media_only])
    app["voice"] = service
    app.router.add_get("/ws", service.websocket)
    app.router.add_get("/media/{token}", service.media_stream)
    app.router.add_get("/", lambda request: web.FileResponse(WEB_ROOT / "index.html"))
    app.router.add_static("/static", WEB_ROOT)
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.stop)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(Settings.load()), host="0.0.0.0", port=8099)
