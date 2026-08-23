"""HTTP, browser WebSocket, media, and session orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from app.config import Settings
from app.encoder import ProgressiveMp3Encoder, encode_mp3
from app.mcp_broker import McpBroker
from app.media import MediaObject, MediaStore
from app.protocol import parse_hello
from app.rate_limit import RateLimiter
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
        self.broker = McpBroker(
            settings.mcp_servers, shared_concurrency=settings.shared_tool_concurrency
        )
        self.sessions: set[RealtimeSession] = set()
        self._session_lock = asyncio.Lock()
        self._session_count = 0
        self.idle_expirations = 0
        self.session_rate = RateLimiter(settings.session_rate_limit_per_minute)
        self.media_rate = RateLimiter(settings.media_rate_limit_per_minute)
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
        peer = request.remote or "unknown"
        if not self.session_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="session connection rate exceeded")
        async with self._session_lock:
            if self._session_count >= self.settings.max_sessions:
                raise web.HTTPServiceUnavailable(text="session limit reached")
            self._session_count += 1
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=512 * 1024)
        try:
            await ws.prepare(request)
            first = await ws.receive_json()
            hello = parse_hello(first)
        except BaseException:
            async with self._session_lock:
                self._session_count -= 1
            raise
        route = self.routes.get(hello.client_id)
        pcm = bytearray()
        progressive: ProgressiveMp3Encoder | None = None
        progressive_item: MediaObject | None = None
        progressive_reader: asyncio.Task[None] | None = None
        progressive_failed = False

        async def cancel_progressive() -> None:
            nonlocal progressive, progressive_item, progressive_reader
            if progressive:
                await progressive.cancel()
            if progressive_reader:
                progressive_reader.cancel()
                with suppress(asyncio.CancelledError):
                    await progressive_reader
            progressive = None
            progressive_item = None
            progressive_reader = None

        async def on_audio(chunk: bytes) -> None:
            nonlocal progressive, progressive_item, progressive_reader, progressive_failed
            current = self.routes.get(hello.client_id)
            if current.sink == "browser":
                await ws.send_bytes(chunk)
                return
            if current.mode == "buffered":
                pcm.extend(chunk)
                return
            pcm.extend(chunk)
            if progressive_failed:
                return
            if progressive is None:
                progressive = ProgressiveMp3Encoder()
                await progressive.start()
                token, progressive_item = self.media.create()
                active_encoder = progressive
                active_item = progressive_item

                async def pump() -> None:
                    async for encoded in active_encoder.chunks():
                        self.media.append(active_item, encoded)
                    self.media.finish(active_item)

                progressive_reader = asyncio.create_task(pump())
                try:
                    result = await self._play(current, request, token)
                    await ws.send_json(
                        {
                            "type": "playback_status",
                            "mode": "progressive",
                            "request_latency_ms": result.request_latency_ms,
                            "replaced_active_playback": result.replaced_active_playback,
                        }
                    )
                except Exception as err:
                    await cancel_progressive()
                    if not current.progressive_fallback:
                        raise
                    progressive_failed = True
                    await ws.send_json(
                        {
                            "type": "playback_status",
                            "mode": "progressive",
                            "ok": False,
                            "fallback": "buffered",
                            "error": {"type": type(err).__name__},
                        }
                    )
                    return
            await progressive.write(chunk)

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal progressive, progressive_item, progressive_reader, progressive_failed
            if event["type"] == "response.output_audio.done":
                current = self.routes.get(hello.client_id)
                if (
                    current.sink == "media_player"
                    and (current.mode == "buffered" or progressive_failed)
                    and pcm
                ):
                    encoded = await encode_mp3(bytes(pcm))
                    pcm.clear()
                    token, item = self.media.create()
                    self.media.append(item, encoded)
                    self.media.finish(item)
                    result = await self._play(current, request, token)
                    await ws.send_json(
                        {
                            "type": "playback_status",
                            "mode": "buffered",
                            "ok": True,
                            "fallback_used": progressive_failed,
                            "request_latency_ms": result.request_latency_ms,
                            "replaced_active_playback": result.replaced_active_playback,
                        }
                    )
                    progressive_failed = False
                elif progressive:
                    await progressive.finish()
                    if progressive_reader:
                        await progressive_reader
                    progressive = None
                    progressive_item = None
                    progressive_reader = None
                    pcm.clear()
            safe = {
                key: event[key]
                for key in ("type", "delta", "transcript", "reconnects", "name", "call_id")
                if key in event
            }
            if event["type"] == "error":
                error = event.get("error", {})
                safe["error"] = {
                    "type": error.get("type", "upstream_error"),
                    "code": error.get("code"),
                    "message": str(error.get("message", "Realtime request failed"))[:300],
                }
            await ws.send_json(safe)

        assert self.http
        realtime = RealtimeSession(
            self.http, self.settings, self.broker, on_audio, on_event, hello.client_id
        )
        self.sessions.add(realtime)
        try:
            await realtime.start()
            await ws.send_json(
                {
                    "type": "session_ready",
                    "client_id": hello.client_id,
                    "route": asdict(route),
                    "mcp": self.broker.status(),
                    "diagnostics": self._diagnostics(realtime),
                }
            )
            while not ws.closed:
                remaining = self.settings.idle_timeout_seconds - (
                    time.monotonic() - realtime.last_activity
                )
                if remaining <= 0:
                    realtime.idle_expired = True
                    self.idle_expirations += 1
                    await ws.send_json({"type": "session.expired", "reason": "idle_timeout"})
                    break
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=remaining)
                except TimeoutError:
                    realtime.idle_expired = True
                    self.idle_expirations += 1
                    await ws.send_json({"type": "session.expired", "reason": "idle_timeout"})
                    break
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                if message.type == WSMsgType.BINARY:
                    await realtime.append_audio(message.data)
                    continue
                if message.type != WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                realtime.touch()
                if event.get("type") == "ptt_start":
                    await cancel_progressive()
                    progressive_failed = False
                    pcm.clear()
                    await realtime.sync_tools()
                    was_responding = realtime.response_active
                    await realtime.cancel()
                    realtime.begin_turn()
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
                elif event.get("type") == "route_test":
                    candidate = OutputRoute(**event["route"]).validate()
                    try:
                        if candidate.sink == "media_player":
                            encoded = await encode_mp3(b"\0" * 12_000)
                            token, item = self.media.create()
                            self.media.append(item, encoded)
                            self.media.finish(item)
                            playback = await self._play(candidate, request, token)
                        else:
                            playback = None
                        await ws.send_json(
                            {
                                "type": "route_test_result",
                                "ok": True,
                                "route": asdict(candidate),
                                "message": "Playback request accepted",
                                "request_latency_ms": (
                                    playback.request_latency_ms if playback else 0
                                ),
                                "replaced_active_playback": (
                                    playback.replaced_active_playback if playback else False
                                ),
                            }
                        )
                    except Exception as err:
                        LOGGER.warning("Route test failed: %s", type(err).__name__)
                        await ws.send_json(
                            {
                                "type": "route_test_result",
                                "ok": False,
                                "route": asdict(candidate),
                                "error": {"type": type(err).__name__, "message": "Playback failed"},
                            }
                        )
                elif event.get("type") == "speakers_list" and self.speakers:
                    await ws.send_json(
                        {"type": "speakers", "items": await self.speakers.list_speakers()}
                    )
                elif event.get("type") == "tools_refresh":
                    await self.broker.refresh()
                    await realtime.sync_tools()
                    await ws.send_json({"type": "mcp_status", "mcp": self.broker.status()})
                elif event.get("type") == "diagnostics_get":
                    await ws.send_json(
                        {"type": "session_diagnostics", **self._diagnostics(realtime)}
                    )
                elif event.get("type") == "conversation_reset":
                    await realtime.reset()
                    await ws.send_json({"type": "conversation_reset", "ok": True})
        except Exception as err:
            LOGGER.warning("Browser session failed: %s", type(err).__name__)
            if not ws.closed:
                await ws.send_json(
                    {
                        "type": "app.error",
                        "error": {"type": type(err).__name__, "message": "Session request failed"},
                    }
                )
        finally:
            await cancel_progressive()
            await realtime.close()
            self.sessions.discard(realtime)
            await ws.close()
            async with self._session_lock:
                self._session_count -= 1
        return ws

    def _diagnostics(self, current: RealtimeSession) -> dict[str, Any]:
        return {
            "session_count": self._session_count,
            "idle_expirations": self.idle_expirations,
            "session": current.diagnostics(),
        }

    async def _play(self, route: OutputRoute, request: web.Request, token: str):
        assert self.speakers
        url = f"{self.settings.speaker_base_url.rstrip('/')}/media/{token}"
        return await self.speakers.play(route, url)

    async def media_stream(self, request: web.Request) -> web.StreamResponse:
        peer = request.remote or "unknown"
        if not self.media_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="media request rate exceeded")
        item = self.media.claim(request.match_info["token"])
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
