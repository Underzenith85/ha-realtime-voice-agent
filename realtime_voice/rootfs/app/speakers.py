"""Home Assistant media-player output."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable
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


class SpeakerPlaybackCancelled(RuntimeError):
    """Playback cancelled before its queued speaker request was submitted."""


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
        self._cancel_events: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._active: set[str] = set()
        self._ready_at: defaultdict[str, float] = defaultdict(float)
        self._progressive: dict[str, tuple[Awaitable[float], float]] = {}

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
        self,
        route: OutputRoute,
        media_url: str,
        duration_seconds: float = 0,
        progressive_completion: Awaitable[float] | None = None,
    ) -> PlaybackResult:
        assert route.entity_id
        cancel_event = self._cancel_events[route.entity_id]
        async with self._locks[route.entity_id]:
            if cancel_event.is_set():
                raise SpeakerPlaybackCancelled
            started = time.monotonic()
            prior_progressive = self._progressive.get(route.entity_id)
            if prior_progressive:
                completion, playback_started = prior_progressive
                duration_seconds_completed = await self._wait_for_completion(
                    completion, cancel_event
                )
                self._progressive.pop(route.entity_id, None)
                self._ready_at[route.entity_id] = max(
                    self._ready_at[route.entity_id],
                    playback_started
                    + duration_seconds_completed
                    + PLAYBACK_PADDING_SECONDS,
                )
            remaining = max(0.0, self._ready_at[route.entity_id] - time.monotonic())
            if remaining:
                LOGGER.info(
                    "Waiting %.0fms for prior speaker playback: entity=%s",
                    remaining * 1000,
                    route.entity_id,
                )
                sleep = asyncio.create_task(asyncio.sleep(remaining))
                cancelled = asyncio.create_task(cancel_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {sleep, cancelled}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (sleep, cancelled):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sleep, cancelled, return_exceptions=True)
                if cancelled in done:
                    raise SpeakerPlaybackCancelled
            if cancel_event.is_set():
                raise SpeakerPlaybackCancelled
            await self._play_request(route, media_url)
            self._active.add(route.entity_id)
            playback_started = time.monotonic()
            if progressive_completion is not None:
                self._progressive[route.entity_id] = (
                    progressive_completion,
                    playback_started,
                )
            elif duration_seconds > 0:
                self._ready_at[route.entity_id] = (
                    playback_started + duration_seconds + PLAYBACK_PADDING_SECONDS
                )
            return PlaybackResult(
                request_latency_ms=round((time.monotonic() - started) * 1000),
                replaced_active_playback=False,
            )

    @staticmethod
    async def _wait_for_completion(
        completion: Awaitable[float], cancel_event: asyncio.Event
    ) -> float:
        completed = asyncio.ensure_future(completion)
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {completed, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not cancelled.done():
                cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        if cancelled in done:
            raise SpeakerPlaybackCancelled
        return completed.result()

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

    async def stop(self, entity_id: str, *, stop_active: bool = True) -> None:
        # Wake queued plays before taking the lock they are holding during their
        # estimated playback wait. A fresh event lets playback submitted after
        # this cancellation queue normally behind the stop request.
        self._cancel_events[entity_id].set()
        self._cancel_events[entity_id] = asyncio.Event()
        async with self._locks[entity_id]:
            if stop_active and entity_id in self._active:
                await self._stop_request(entity_id)
            if stop_active:
                self._active.discard(entity_id)
                self._ready_at.pop(entity_id, None)
                self._progressive.pop(entity_id, None)

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
