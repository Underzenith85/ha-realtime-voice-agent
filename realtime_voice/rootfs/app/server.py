"""HTTP, browser WebSocket, media, and session orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from app.config import HardwareClientConfig, Settings
from app.encoder import ProgressiveMp3Encoder, encode_mp3
from app.ha_apis import discover_managed_mcp_configs
from app.mcp_broker import McpBroker
from app.media import MediaStore
from app.playback import ProgressivePlayback, SpeakerPlaybackCoordinator
from app.protocol import parse_hello
from app.rate_limit import RateLimiter
from app.realtime import RealtimeSession
from app.routes import OutputRoute, RouteStore
from app.speakers import SpeakerController, SpeakerPlaybackCancelled
from app.timers import TimerManager

LOGGER = logging.getLogger(__name__)
WEB_ROOT = Path(__file__).parent / "web"


@web.middleware
async def ingress_or_media_only(request: web.Request, handler: Any) -> web.StreamResponse:
    """Keep the control UI ingress-only while allowing signed speaker media pulls."""
    if (
        not request.path.startswith("/media/")
        and request.path != "/device/ws"
        and "X-Ingress-Path" not in request.headers
    ):
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
        self.timers = TimerManager(settings.timers_path)
        self.http = None
        self.speakers = None
        self.playback = None
        self.hardware_clients = {client.client_id: client for client in settings.hardware_clients}

    async def start(self, app: web.Application) -> None:
        import aiohttp

        self.http = aiohttp.ClientSession()
        token = os.getenv("SUPERVISOR_TOKEN", "")
        self.speakers = SpeakerController(self.http, self.settings.ha_api_url, token)
        self.playback = SpeakerPlaybackCoordinator(
            self.media,
            self.speakers,
            self.settings.speaker_base_url,
            encode=encode_mp3,
            progressive_factory=ProgressiveMp3Encoder,
        )
        await self.timers.start()
        try:
            managed = await discover_managed_mcp_configs(self.http, self.settings.ha_api_url, token)
            await self.broker.reconcile_managed(managed)
        except Exception as err:
            LOGGER.warning("HA-managed MCP API discovery unavailable: %s", type(err).__name__)
        await self.broker.start()
        await self.media.start()

    async def stop(self, app: web.Application) -> None:
        await asyncio.gather(*(session.close() for session in tuple(self.sessions)))
        await asyncio.gather(self.broker.close(), self.timers.close(), self.media.close())
        if self.http:
            await self.http.close()

    async def device_websocket(self, request: web.Request) -> web.WebSocketResponse:
        peer = request.remote or "unknown"
        if not self.session_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="device authentication rate exceeded")
        authorization = request.headers.get("Authorization", "")
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        digest = hashlib.sha256(token.encode()).hexdigest()
        hardware = next(
            (
                client
                for client in self.hardware_clients.values()
                if hmac.compare_digest(client.token_sha256, digest)
            ),
            None,
        )
        if hardware is None:
            raise web.HTTPUnauthorized(text="invalid device credential")
        return await self.websocket(request, hardware=hardware, rate_checked=True)

    async def websocket(
        self,
        request: web.Request,
        hardware: HardwareClientConfig | None = None,
        rate_checked: bool = False,
    ) -> web.WebSocketResponse:
        peer = request.remote or "unknown"
        if not rate_checked and not self.session_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="session connection rate exceeded")
        async with self._session_lock:
            if self._session_count >= self.settings.max_sessions:
                raise web.HTTPServiceUnavailable(text="session limit reached")
            self._session_count += 1
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=512 * 1024)
        try:
            await ws.prepare(request)
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            hello = parse_hello(first)
            if hardware and (
                hello.client_id != hardware.client_id or hello.client_type != "voice_pe"
            ):
                await ws.close(code=1008, message=b"device identity mismatch")
                async with self._session_lock:
                    self._session_count -= 1
                return ws
        except BaseException:
            async with self._session_lock:
                self._session_count -= 1
            raise
        route = self.routes.get(hello.client_id)
        if hardware and not self.routes.contains(hello.client_id):
            route = OutputRoute(
                sink="media_player" if hardware.entity_id else "browser",
                entity_id=hardware.entity_id,
                mode=hardware.mode,
                announce=hardware.announce,
            ).validate()
            self.routes.set(hello.client_id, route)
        pcm = bytearray()
        progressive: ProgressivePlayback | None = None
        progressive_failed = False

        async def cancel_progressive() -> None:
            nonlocal progressive
            if progressive:
                await progressive.cancel()
            progressive = None

        async def on_audio(chunk: bytes) -> None:
            nonlocal progressive, progressive_failed
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
                try:
                    assert self.playback
                    progressive = await self.playback.start_progressive(current)
                    await ws.send_json(
                        {
                            "type": "playback_status",
                            "mode": "progressive",
                            "request_latency_ms": progressive.result.request_latency_ms,
                            "replaced_active_playback": (
                                progressive.result.replaced_active_playback
                            ),
                        }
                    )
                except SpeakerPlaybackCancelled:
                    await cancel_progressive()
                    return
                except Exception as err:
                    await cancel_progressive()
                    if not current.progressive_fallback:
                        raise
                    progressive_failed = True
                    assert self.playback
                    await ws.send_json(
                        {
                            "type": "playback_status",
                            "mode": "progressive",
                            "ok": False,
                            "fallback": "buffered",
                            "error": self.playback.failure(err, "Progressive playback"),
                        }
                    )
                    return
            await progressive.write(chunk)

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal progressive, progressive_failed
            if event["type"] == "response.output_audio.done":
                current = self.routes.get(hello.client_id)
                if (
                    current.sink == "media_player"
                    and (current.mode == "buffered" or progressive_failed)
                    and pcm
                ):
                    raw_audio = bytes(pcm)
                    pcm.clear()
                    try:
                        assert self.playback
                        buffered = await self.playback.play_buffered(current, raw_audio)
                        await ws.send_json(
                            {
                                "type": "playback_status",
                                "mode": "buffered",
                                "ok": True,
                                "fallback_used": progressive_failed,
                                "request_latency_ms": buffered.result.request_latency_ms,
                                "replaced_active_playback": (
                                    buffered.result.replaced_active_playback
                                ),
                            }
                        )
                    except SpeakerPlaybackCancelled:
                        progressive_failed = False
                    except Exception as err:
                        assert self.playback
                        error = self.playback.failure(err, "Speaker playback")
                        await ws.send_bytes(raw_audio)
                        await ws.send_json(
                            {
                                "type": "playback_status",
                                "mode": "browser",
                                "ok": True,
                                "fallback_used": True,
                                "error": error,
                            }
                        )
                    progressive_failed = False
                elif progressive:
                    await progressive.finish()
                    progressive = None
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
            self.http,
            self.settings,
            self.broker,
            on_audio,
            on_event,
            hello.client_id,
            self.timers,
        )
        timer_callback = realtime.announce_timer
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
            self.timers.register(hello.client_id, timer_callback)
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
                    if route.entity_id and self.playback:
                        await self.playback.cancel(route.entity_id, stop_active=was_responding)
                elif event.get("type") == "ptt_stop":
                    await realtime.commit()
                elif event.get("type") == "cancel":
                    await cancel_progressive()
                    progressive_failed = False
                    pcm.clear()
                    await realtime.cancel()
                    if route.entity_id and self.playback:
                        await self.playback.cancel(route.entity_id)
                elif event.get("type") == "route_set":
                    route = OutputRoute(**event["route"]).validate()
                    self.routes.set(hello.client_id, route)
                    await ws.send_json({"type": "route", "route": asdict(route)})
                elif event.get("type") == "route_test":
                    candidate = OutputRoute(**event["route"]).validate()
                    try:
                        assert self.playback
                        playback = await self.playback.test_output(candidate)
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
                        assert self.playback
                        error = self.playback.failure(err, "Route test")
                        error.setdefault("message", "Playback failed")
                        await ws.send_json(
                            {
                                "type": "route_test_result",
                                "ok": False,
                                "route": asdict(candidate),
                                "error": error,
                            }
                        )
                elif event.get("type") == "speakers_list" and self.speakers:
                    await ws.send_json(
                        {"type": "speakers", "items": await self.speakers.list_speakers()}
                    )
                elif event.get("type") == "tools_refresh":
                    try:
                        managed = await discover_managed_mcp_configs(
                            self.http,
                            self.settings.ha_api_url,
                            os.getenv("SUPERVISOR_TOKEN", ""),
                        )
                        await self.broker.reconcile_managed(managed)
                    except Exception as err:
                        LOGGER.warning(
                            "HA-managed MCP API refresh unavailable: %s", type(err).__name__
                        )
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
            self.timers.unregister(hello.client_id, timer_callback)
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

    async def media_stream(self, request: web.Request) -> web.StreamResponse:
        peer = request.remote or "unknown"
        if not self.media_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="media request rate exceeded")
        item = self.media.claim(request.match_info["token"])
        if item is None:
            raise web.HTTPNotFound()
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Cache-Control": "no-store",
                "Accept-Ranges": "bytes",
            },
        )
        if item.complete.is_set():
            body = b"".join(item.chunks)
            byte_range = request.http_range
            if byte_range.start is not None or byte_range.stop is not None:
                start, stop, _ = byte_range.indices(len(body))
                if start >= len(body) or start >= stop:
                    raise web.HTTPRequestRangeNotSatisfiable(
                        headers={"Content-Range": f"bytes */{len(body)}"}
                    )
                ranged = body[start:stop]
                return web.Response(
                    status=206,
                    body=ranged,
                    content_type="audio/mpeg",
                    headers={
                        "Cache-Control": "no-store",
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes {start}-{stop - 1}/{len(body)}",
                    },
                )
            return web.Response(
                body=body,
                content_type="audio/mpeg",
                headers={"Cache-Control": "no-store", "Accept-Ranges": "bytes"},
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

    async def media_metadata(self, request: web.Request) -> web.StreamResponse:
        """Answer player probes without consuming the single-use audio URL."""
        peer = request.remote or "unknown"
        if not self.media_rate.allow(peer):
            raise web.HTTPTooManyRequests(text="media request rate exceeded")
        item = self.media.inspect(request.match_info["token"])
        if item is None:
            raise web.HTTPNotFound()
        headers = {
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        }
        if item.complete.is_set():
            headers["Content-Length"] = str(sum(len(chunk) for chunk in item.chunks))
        return web.Response(status=200, headers=headers)


def create_app(settings: Settings) -> web.Application:
    service = VoiceServer(settings)
    app = web.Application(middlewares=[ingress_or_media_only])
    app["voice"] = service
    app.router.add_get("/ws", service.websocket)
    app.router.add_get("/device/ws", service.device_websocket)
    app.router.add_head("/media/{token}.mp3", service.media_metadata)
    app.router.add_get("/media/{token}.mp3", service.media_stream, allow_head=False)
    app.router.add_get("/", lambda request: web.FileResponse(WEB_ROOT / "index.html"))
    app.router.add_static("/static", WEB_ROOT)
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.stop)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_app(Settings.load()), host="0.0.0.0", port=8099)
