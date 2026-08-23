"""Home Assistant media-player output."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass

import aiohttp

from app.routes import OutputRoute


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    request_latency_ms: int
    replaced_active_playback: bool


class SpeakerController:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._active: set[str] = set()

    async def list_speakers(self) -> list[dict[str, str]]:
        async with self.session.get(
            f"{self.base_url}/api/states", headers=self.headers
        ) as response:
            response.raise_for_status()
            states = await response.json()
        return [
            {
                "entity_id": state["entity_id"],
                "name": state["attributes"].get("friendly_name", state["entity_id"]),
            }
            for state in states
            if state["entity_id"].startswith("media_player.")
        ]

    async def play(self, route: OutputRoute, media_url: str) -> PlaybackResult:
        assert route.entity_id
        async with self._locks[route.entity_id]:
            replaced = route.entity_id in self._active
            if replaced:
                await self._stop_request(route.entity_id)
                self._active.discard(route.entity_id)
            started = time.monotonic()
            await self._play_request(route, media_url)
            self._active.add(route.entity_id)
            return PlaybackResult(
                request_latency_ms=round((time.monotonic() - started) * 1000),
                replaced_active_playback=replaced,
            )

    async def _play_request(self, route: OutputRoute, media_url: str) -> None:
        data: dict[str, object] = {
            "entity_id": route.entity_id,
            "media_content_id": media_url,
            "media_content_type": "audio/mpeg",
            "announce": route.announce,
        }
        if route.volume is not None:
            data["extra"] = {"volume": route.volume}
        async with self.session.post(
            f"{self.base_url}/api/services/media_player/play_media",
            headers=self.headers,
            json=data,
        ) as response:
            response.raise_for_status()

    async def stop(self, entity_id: str) -> None:
        async with self._locks[entity_id]:
            await self._stop_request(entity_id)
            self._active.discard(entity_id)

    async def _stop_request(self, entity_id: str) -> None:
        async with self.session.post(
            f"{self.base_url}/api/services/media_player/media_stop",
            headers=self.headers,
            json={"entity_id": entity_id},
        ) as response:
            response.raise_for_status()
