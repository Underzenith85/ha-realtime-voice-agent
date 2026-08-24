"""Recoverable OpenAI Realtime session bridge with bounded in-memory history."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from app.config import Settings
from app.mcp_broker import McpBroker, ToolBinding
from app.rate_limit import RateLimiter
from app.timers import TIMER_TOOLS, TimerManager, VoiceTimer

LOGGER = logging.getLogger(__name__)
AudioCallback = Callable[[bytes], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]

TERMINAL_UPSTREAM_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "invalid_api_key",
        "organization_deactivated",
        "project_deactivated",
    }
)


@dataclass(slots=True)
class ToolRecord:
    name: str
    call_id: str
    arguments: str
    output: str


@dataclass(slots=True)
class ConversationTurn:
    user: str = ""
    assistant: str = ""
    tools: list[ToolRecord] = field(default_factory=list)


class ConversationHistory:
    def __init__(self, limit: int = 20) -> None:
        if limit < 1:
            raise ValueError("history limit must be positive")
        self._turns: deque[ConversationTurn] = deque(maxlen=limit)
        self.current: ConversationTurn | None = None

    def begin_turn(self) -> ConversationTurn:
        turn = ConversationTurn()
        self._turns.append(turn)
        self.current = turn
        return turn

    def set_user(self, transcript: str) -> None:
        if self.current and transcript.strip():
            self.current.user = transcript.strip()

    def append_assistant(self, delta: str) -> None:
        if self.current:
            self.current.assistant += delta

    def add_tool(self, record: ToolRecord, turn: ConversationTurn | None = None) -> None:
        target = turn or self.current
        if target:
            target.tools.append(record)

    def __len__(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()
        self.current = None

    def restore_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for turn in self._turns:
            if turn.user:
                events.append(self._message_event("user", "input_text", "text", turn.user))
            for tool in turn.tools:
                events.extend(
                    [
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call",
                                "name": tool.name,
                                "call_id": tool.call_id,
                                "arguments": tool.arguments,
                                "status": "completed",
                            },
                        },
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": tool.call_id,
                                "output": tool.output,
                            },
                        },
                    ]
                )
            if turn.assistant:
                events.append(
                    self._message_event("assistant", "output_text", "text", turn.assistant)
                )
        return events

    @staticmethod
    def _message_event(role: str, content_type: str, value_name: str, value: str) -> dict[str, Any]:
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, value_name: value}],
            },
        }


class RealtimeSession:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        settings: Settings,
        broker: McpBroker,
        on_audio: AudioCallback,
        on_event: EventCallback,
        client_id: str | None = None,
        timers: TimerManager | None = None,
    ) -> None:
        self.http = http
        self.settings = settings
        self.broker = broker
        self.on_audio = on_audio
        self.on_event = on_event
        self.client_id = client_id
        self.timers = timers
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader: asyncio.Task[None] | None = None
        self.response_active = False
        self.catalog_version = -1
        self.tool_bindings: dict[str, ToolBinding] = {}
        self.history = ConversationHistory(settings.history_turn_limit)
        self.created_at = time.monotonic()
        self.last_activity = self.created_at
        self.reconnects = 0
        self.idle_expired = False
        self.terminal_error = False
        self._closing = False
        self._connected = asyncio.Event()
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._tool_limit = asyncio.Semaphore(4)
        self._tool_rate = RateLimiter(settings.tool_rate_limit_per_minute)
        self._tool_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._closing = False
        await self._connect(restore=False)
        self.reader = asyncio.create_task(self._reader_loop(), name="openai-realtime-reader")

    def begin_turn(self) -> None:
        self.history.begin_turn()
        self.touch()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def sync_tools(self, *, force: bool = False) -> bool:
        version, bindings = self.broker.snapshot()
        if not force and version == self.catalog_version:
            return False
        await self._send(self._session_update(bindings))
        self.catalog_version = version
        self.tool_bindings = bindings
        return True

    async def append_audio(self, pcm: bytes) -> None:
        self.touch()
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        )

    async def commit(self) -> None:
        self.touch()
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})

    async def cancel(self) -> None:
        self.touch()
        if self.response_active:
            await self._send({"type": "response.cancel"})
        await self._send({"type": "input_audio_buffer.clear"})

    async def reset(self) -> None:
        self.history.clear()
        self.response_active = False
        websocket = self.ws
        if websocket:
            await websocket.close()

    async def announce_timer(self, timer: VoiceTimer) -> None:
        await self.cancel()
        self.begin_turn()
        message = f"The {timer.kind} named {timer.label} has completed. Announce it briefly."
        self.history.set_user(message)
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": message}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def close(self) -> None:
        self._closing = True
        self._connected.set()
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.ws:
            await self.ws.close()
        for task in tuple(self._tool_tasks):
            task.cancel()
        await asyncio.gather(*self._tool_tasks, return_exceptions=True)
        self._tool_tasks.clear()
        self.ws = None
        self._connected.clear()

    def diagnostics(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "client_id": self.client_id,
            "age_seconds": round(now - self.created_at, 1),
            "idle_seconds": round(now - self.last_activity, 1),
            "reconnects": self.reconnects,
            "idle_expired": self.idle_expired,
            "history_turns": len(self.history),
            "active_tool_calls": len(self._tool_tasks),
            "timer_count": (
                len(self.timers.list(self.client_id)) if self.timers and self.client_id else 0
            ),
        }

    async def _connect(self, *, restore: bool) -> None:
        async with self._connect_lock:
            if self._closing:
                raise asyncio.CancelledError
            websocket = await self.http.ws_connect(
                f"{self.settings.openai_realtime_url}?model={self.settings.model}",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                heartbeat=20,
            )
            self.ws = websocket
            self._connected.set()
            try:
                await self.sync_tools(force=True)
                if restore:
                    for event in self.history.restore_events():
                        await self._send(event)
            except BaseException:
                self._connected.clear()
                await websocket.close()
                self.ws = None
                raise

    def _session_update(self, bindings: dict[str, ToolBinding]) -> dict[str, Any]:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.settings.model,
                "instructions": self.settings.instructions
                + (
                    " Use the voice_timer tools for timers and voice_alarm_create for alarms. "
                    "List timers before resolving ordinal follow-ups such as 'the second timer'. "
                    f"The current UTC time is {datetime.now(UTC).isoformat()}."
                    if self.timers
                    else ""
                ),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": {"model": self.settings.input_transcription_model},
                        "turn_detection": None,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": self.settings.voice,
                    },
                },
                "tools": [binding.realtime_definition() for binding in bindings.values()]
                + (TIMER_TOOLS if self.timers else []),
                "tool_choice": "auto",
                "parallel_tool_calls": True,
            },
        }

    async def _send(self, event: dict[str, Any]) -> None:
        for attempt in range(2):
            await self._connected.wait()
            if self._closing:
                raise asyncio.CancelledError
            async with self._send_lock:
                websocket = self.ws
                if websocket and not websocket.closed:
                    try:
                        await websocket.send_json(event)
                        return
                    except (aiohttp.ClientConnectionError, ConnectionResetError):
                        self._connected.clear()
                        await websocket.close()
            if attempt == 0:
                await asyncio.wait_for(self._connected.wait(), timeout=10)
        raise ConnectionError("Realtime session is unavailable")

    async def _reader_loop(self) -> None:
        backoff = 0.25
        while not self._closing:
            websocket = self.ws
            if websocket is None:
                return
            await self._read(websocket)
            if self._closing:
                return
            self._connected.clear()
            self.response_active = False
            if self.ws is websocket:
                self.ws = None
            await websocket.close()
            if self.terminal_error:
                return
            await self.on_event({"type": "session.reconnecting"})
            while not self._closing:
                try:
                    await self._connect(restore=True)
                except (aiohttp.ClientError, TimeoutError, ConnectionError) as err:
                    LOGGER.warning("Realtime reconnect failed: %s", type(err).__name__)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                    continue
                self.reconnects += 1
                backoff = 0.25
                await self.on_event({"type": "session.reconnected", "reconnects": self.reconnects})
                break

    async def _read(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for message in websocket:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            event_type = event.get("type")
            if event_type == "response.created":
                self.response_active = True
                await self.on_event(event)
            elif event_type == "response.output_audio.delta":
                await self.on_audio(base64.b64decode(event["delta"]))
            elif event_type == "response.output_audio_transcript.delta":
                self.history.append_assistant(event.get("delta", ""))
                await self.on_event(event)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                self.history.set_user(event.get("transcript", ""))
                await self.on_event(event)
            elif event_type == "response.function_call_arguments.done":
                binding = self.tool_bindings.get(event.get("name", ""))
                await self.on_event(
                    {"type": event_type, "name": event.get("name"), "call_id": event.get("call_id")}
                )
                task = asyncio.create_task(self._call_tool(event, binding, self.history.current))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)
            elif event_type in {"response.output_audio.done", "response.done", "error"}:
                if event_type == "response.done":
                    self.response_active = False
                await self.on_event(event)
                error_code = event.get("error", {}).get("code")
                if event_type == "error" and error_code in TERMINAL_UPSTREAM_ERROR_CODES:
                    self.terminal_error = True
                    LOGGER.error(
                        "Realtime session stopped after terminal upstream error: %s", error_code
                    )
                    await websocket.close()
                    return
                if event_type == "error" and error_code in {
                    "session_expired",
                    "session_expired_error",
                }:
                    await websocket.close()
                    return

    async def _call_tool(
        self,
        event: dict[str, Any],
        binding: ToolBinding | None = None,
        turn: ConversationTurn | None = None,
    ) -> None:
        call_id = event["call_id"]
        arguments_raw = event.get("arguments") or "{}"
        async with self._tool_limit:
            try:
                if not self._tool_rate.allow("session"):
                    raise RuntimeError("tool call rate exceeded")
                arguments = json.loads(arguments_raw)
                if event["name"].startswith(("voice_timer_", "voice_alarm_")):
                    if not self.timers or not self.client_id:
                        raise RuntimeError("timer service is unavailable")
                    output = await self.timers.call(event["name"], self.client_id, arguments)
                else:
                    binding = binding or self.tool_bindings.get(event["name"])
                    if binding is None:
                        raise KeyError("tool binding is no longer available")
                    output = await asyncio.wait_for(
                        self.broker.call_binding(binding, arguments, client_id=self.client_id),
                        timeout=self.settings.tool_timeout_seconds,
                    )
            except Exception as err:  # returned to the model, not hidden in logs
                LOGGER.warning(
                    "Tool call failed: tool=%s error=%s",
                    event.get("name"),
                    type(err).__name__,
                )
                output = json.dumps({"error": type(err).__name__})
            self.history.add_tool(
                ToolRecord(event.get("name", "unknown"), call_id, arguments_raw, output),
                turn,
            )
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    },
                }
            )
            await self._send({"type": "response.create"})
