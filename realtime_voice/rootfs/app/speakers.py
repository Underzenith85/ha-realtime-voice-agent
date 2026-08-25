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

from app.ha_apis import MediaPlayerState, get_media_player_state
from app.routes import OutputRoute

LOGGER = logging.getLogger(__name__)
SIGNED_MEDIA_PATTERN = re.compile(r"/media/[A-Za-z0-9_-]+")
STATE_POLL_SECONDS = 0.25
STARTUP_GRACE_SECONDS = 5.0
MAX_OVERRUN_SECONDS = 15.0


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


@dataclass(slots=True)
class _TrackedPlayback:
    media_url: str
    started_at: float
    duration_seconds: float
    progressive_completion: Awaitable[float] | None = None


class SpeakerController:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cancel_events: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self._active: set[str] = set()
        self._playbacks: dict[str, _TrackedPlayback] = {}

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
            prior = self._playbacks.pop(route.entity_id, None)
            if prior:
                if prior.progressive_completion is not None:
                    prior.duration_seconds = await self._wait_for_completion(
                        prior.progressive_completion, cancel_event
                    )
                await self._wait_for_playback(route.entity_id, prior, cancel_event)
            if cancel_event.is_set():
                raise SpeakerPlaybackCancelled
            await self._play_request(route, media_url)
            self._active.add(route.entity_id)
            playback_started = time.monotonic()
            if progressive_completion is not None or duration_seconds > 0:
                self._playbacks[route.entity_id] = _TrackedPlayback(
                    media_url, playback_started, duration_seconds, progressive_completion
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

    async def _wait_for_playback(
        self,
        entity_id: str,
        playback: _TrackedPlayback,
        cancel_event: asyncio.Event,
    ) -> None:
        """Wait for HA to report completion, with duration and deadline safeguards."""
        duration_deadline = playback.started_at + playback.duration_seconds
        startup_deadline = playback.started_at + STARTUP_GRACE_SECONDS
        maximum_deadline = max(duration_deadline, startup_deadline) + MAX_OVERRUN_SECONDS
        observed_playing = False
        last_state = "unknown"
        while time.monotonic() < maximum_deadline:
            if cancel_event.is_set():
                raise SpeakerPlaybackCancelled
            try:
                current = await get_media_player_state(
                    self.session, self.base_url, self.headers, entity_id
                )
            except (TimeoutError, aiohttp.ClientError):
                current = None

            if current is None or current.state == "unavailable":
                # HA cannot provide a useful signal; fall back to the PCM duration.
                if time.monotonic() >= duration_deadline:
                    return
            else:
                last_state = current.state
                if self._is_replaced(current, playback.media_url):
                    LOGGER.info("Speaker playback was replaced: entity=%s", entity_id)
                    return
                if current.state == "playing":
                    observed_playing = True
                elif observed_playing and current.state in {"idle", "paused", "off", "standby"}:
                    return
                elif not observed_playing and current.state in {"idle", "paused", "off", "standby"}:
                    # An initial idle/paused state may simply be Sonos startup latency.
                    if time.monotonic() >= max(duration_deadline, startup_deadline):
                        return

            await self._sleep_or_cancel(STATE_POLL_SECONDS, cancel_event)

        LOGGER.warning(
            "Speaker state did not complete before timeout: entity=%s state=%s",
            entity_id,
            last_state,
        )

    @staticmethod
    def _is_replaced(state: MediaPlayerState, expected_url: str) -> bool:
        return bool(state.media_content_id and state.media_content_id != expected_url)

    @staticmethod
    async def _sleep_or_cancel(delay: float, cancel_event: asyncio.Event) -> None:
        sleep = asyncio.create_task(asyncio.sleep(delay))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait({sleep, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleep, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleep, cancelled, return_exceptions=True)
        if cancelled in done:
            raise SpeakerPlaybackCancelled

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
                self._playbacks.pop(entity_id, None)

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
