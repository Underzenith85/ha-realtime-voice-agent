"""Home Assistant media-player output."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass

import aiohttp

from app.routes import OutputRoute

LOGGER = logging.getLogger(__name__)
SIGNED_MEDIA_PATTERN = re.compile(r"/media/[A-Za-z0-9_-]+")
PLAYBACK_PADDING_SECONDS = 0.35


class SpeakerRequestError(RuntimeError):
    """A sanitized Home Assistant speaker-service failure."""

    def __init__(self, operation: str, status: int, detail: str) -> None:
        self.operation = operation
        self.status = status
        self.detail = SIGNED_MEDIA_PATTERN.sub("/media/[redacted]", detail)[:300]
        super().__init__(f"{operation} failed with HTTP {status}: {self.detail}")


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
        self._ready_at: defaultdict[str, float] = defaultdict(float)

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

    async def play(
        self, route: OutputRoute, media_url: str, duration_seconds: float = 0
    ) -> PlaybackResult:
        assert route.entity_id
        async with self._locks[route.entity_id]:
            started = time.monotonic()
            remaining = max(0.0, self._ready_at[route.entity_id] - started)
            if remaining:
                LOGGER.info(
                    "Waiting %.0fms for prior speaker playback: entity=%s",
                    remaining * 1000,
                    route.entity_id,
                )
                await asyncio.sleep(remaining)
            await self._play_request(route, media_url)
            self._active.add(route.entity_id)
            if duration_seconds > 0:
                self._ready_at[route.entity_id] = (
                    time.monotonic() + duration_seconds + PLAYBACK_PADDING_SECONDS
                )
            return PlaybackResult(
                request_latency_ms=round((time.monotonic() - started) * 1000),
                replaced_active_playback=False,
            )

    async def _play_request(self, route: OutputRoute, media_url: str) -> None:
        data: dict[str, object] = {
            "entity_id": route.entity_id,
            "media_content_id": media_url,
            # HA media_player integrations classify URL playback as music. Sonos
            # rejects the MIME value audio/mpeg as an invalid content type.
            "media_content_type": "music",
            "announce": route.announce,
        }
        if route.volume is not None:
            data["extra"] = {"volume": route.volume}
        LOGGER.info(
            "Requesting speaker playback: entity=%s content_type=%s announce=%s "
            "volume_override=%s media_url=%s",
            route.entity_id,
            data["media_content_type"],
            route.announce,
            route.volume is not None,
            SIGNED_MEDIA_PATTERN.sub("/media/[redacted]", media_url),
        )
        async with self.session.post(
            f"{self.base_url}/api/services/media_player/play_media",
            headers=self.headers,
            json=data,
        ) as response:
            await self._check_response(response, "play_media")

    async def stop(self, entity_id: str) -> None:
        async with self._locks[entity_id]:
            await self._stop_request(entity_id)
            self._active.discard(entity_id)
            self._ready_at.pop(entity_id, None)

    async def _stop_request(self, entity_id: str) -> None:
        async with self.session.post(
            f"{self.base_url}/api/services/media_player/media_stop",
            headers=self.headers,
            json={"entity_id": entity_id},
        ) as response:
            await self._check_response(response, "media_stop")

    @staticmethod
    async def _check_response(response: aiohttp.ClientResponse, operation: str) -> None:
        status = getattr(response, "status", 200)
        if status < 400:
            return
        detail = await response.text()
        raise SpeakerRequestError(operation, status, detail or response.reason or "request failed")
