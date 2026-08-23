"""OpenAI Realtime session bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

import aiohttp

from app.config import Settings
from app.mcp_broker import McpBroker, ToolBinding

LOGGER = logging.getLogger(__name__)
AudioCallback = Callable[[bytes], Awaitable[None]]
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class RealtimeSession:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        settings: Settings,
        broker: McpBroker,
        on_audio: AudioCallback,
        on_event: EventCallback,
    ) -> None:
        self.http = http
        self.settings = settings
        self.broker = broker
        self.on_audio = on_audio
        self.on_event = on_event
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader: asyncio.Task[None] | None = None
        self.response_active = False
        self.catalog_version = -1
        self.tool_bindings: dict[str, ToolBinding] = {}

    async def start(self) -> None:
        self.ws = await self.http.ws_connect(
            f"{self.settings.openai_realtime_url}?model={self.settings.model}",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            heartbeat=20,
        )
        await self.sync_tools(force=True)
        self.reader = asyncio.create_task(self._read(), name="openai-realtime-reader")

    async def sync_tools(self, *, force: bool = False) -> bool:
        version, bindings = self.broker.snapshot()
        if not force and version == self.catalog_version:
            return False
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": self.settings.model,
                    "instructions": self.settings.instructions,
                    "output_modalities": ["audio"],
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "turn_detection": None,
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "voice": self.settings.voice,
                        },
                    },
                    "tools": [binding.realtime_definition() for binding in bindings.values()],
                    "tool_choice": "auto",
                },
            }
        )
        self.catalog_version = version
        self.tool_bindings = bindings
        return True

    async def append_audio(self, pcm: bytes) -> None:
        await self._send(
            {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}
        )

    async def commit(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})
        await self._send({"type": "response.create"})

    async def cancel(self) -> None:
        if self.response_active:
            await self._send({"type": "response.cancel"})
        await self._send({"type": "input_audio_buffer.clear"})

    async def close(self) -> None:
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
            self.reader = None
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def _send(self, event: dict[str, Any]) -> None:
        if not self.ws:
            raise RuntimeError("Realtime session is not connected")
        await self.ws.send_json(event)

    async def _read(self) -> None:
        assert self.ws
        async for message in self.ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            event_type = event.get("type")
            if event_type == "response.created":
                self.response_active = True
            elif event_type == "response.output_audio.delta":
                await self.on_audio(base64.b64decode(event["delta"]))
            elif event_type == "response.function_call_arguments.done":
                binding = self.tool_bindings.get(event.get("name", ""))
                asyncio.create_task(self._call_tool(event, binding))
            elif event_type in {
                "response.output_audio.done",
                "response.output_audio_transcript.delta",
                "response.done",
                "error",
            }:
                if event_type == "response.done":
                    self.response_active = False
                await self.on_event(event)

    async def _call_tool(self, event: dict[str, Any], binding: ToolBinding | None = None) -> None:
        call_id = event["call_id"]
        try:
            arguments = json.loads(event.get("arguments") or "{}")
            binding = binding or self.tool_bindings.get(event["name"])
            if binding is None:
                raise KeyError("tool binding is no longer available")
            output = await asyncio.wait_for(
                self.broker.call_binding(binding, arguments),
                timeout=self.settings.tool_timeout_seconds,
            )
        except Exception as err:  # returned to the model, not hidden in logs
            LOGGER.exception("Tool call failed: %s", event.get("name"))
            output = json.dumps({"error": type(err).__name__, "message": str(err)[:500]})
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            }
        )
        await self._send({"type": "response.create"})
