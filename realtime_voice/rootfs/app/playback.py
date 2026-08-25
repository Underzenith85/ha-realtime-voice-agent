"""Speaker audio encoding, publication, playback, and failure coordination."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.encoder import PCM_BYTES_PER_SECOND, ProgressiveMp3Encoder, encode_mp3
from app.media import MediaObject, MediaStore
from app.routes import OutputRoute
from app.speakers import PlaybackResult, SpeakerController, SpeakerRequestError

LOGGER = logging.getLogger(__name__)
ROUTE_TEST_PCM = b"\0" * 12_000


@dataclass(frozen=True, slots=True)
class BufferedPlayback:
    """A completed buffered speaker request and its browser-fallback audio."""

    result: PlaybackResult
    raw_audio: bytes


@dataclass(slots=True)
class ProgressivePlayback:
    """Resources owned by one progressive speaker stream."""

    encoder: ProgressiveMp3Encoder
    reader: asyncio.Task[None]
    result: PlaybackResult
    media: MediaStore
    item: MediaObject
    completion: asyncio.Future[float]

    async def write(self, chunk: bytes) -> None:
        await self.encoder.write(chunk)

    async def finish(self) -> None:
        try:
            await self.encoder.finish()
            await self.reader
        finally:
            if not self.completion.done():
                self.completion.set_result(self.encoder.duration_seconds)

    async def cancel(self) -> None:
        try:
            await self.encoder.cancel()
        finally:
            if not self.completion.done():
                self.completion.set_result(0)
            if not self.reader.done():
                self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
            self.media.discard(self.item)


class SpeakerPlaybackCoordinator:
    """Own speaker encoding, signed media publication, and playback operations."""

    def __init__(
        self,
        media: MediaStore,
        speakers: SpeakerController,
        base_url: str,
        *,
        encode: Callable[[bytes], Awaitable[bytes]] = encode_mp3,
        progressive_factory: Callable[[], ProgressiveMp3Encoder] = ProgressiveMp3Encoder,
    ) -> None:
        self.media = media
        self.speakers = speakers
        self.base_url = base_url.rstrip("/")
        self.encode = encode
        self.progressive_factory = progressive_factory

    async def play_buffered(self, route: OutputRoute, pcm: bytes) -> BufferedPlayback:
        encoded = await self.encode(pcm)
        media_url = self._publish_complete(encoded)
        result = await self.speakers.play(
            route,
            media_url,
            duration_seconds=len(pcm) / PCM_BYTES_PER_SECOND,
        )
        return BufferedPlayback(result=result, raw_audio=pcm)

    async def start_progressive(self, route: OutputRoute) -> ProgressivePlayback:
        encoder = self.progressive_factory()
        await encoder.start()
        token, item = self.media.create()
        reader = asyncio.create_task(self._pump(encoder, item))
        completion = asyncio.get_running_loop().create_future()
        try:
            result = await self.speakers.play(
                route,
                self._media_url(token),
                progressive_completion=completion,
            )
        except BaseException:
            try:
                await encoder.cancel()
            finally:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                self.media.discard(item)
            raise
        return ProgressivePlayback(
            encoder=encoder,
            reader=reader,
            result=result,
            media=self.media,
            item=item,
            completion=completion,
        )

    async def test_output(self, route: OutputRoute) -> PlaybackResult | None:
        if route.sink != "media_player":
            return None
        encoded = await self.encode(ROUTE_TEST_PCM)
        media_url = self._publish_complete(encoded)
        return await self.speakers.play(
            route,
            media_url,
            duration_seconds=len(ROUTE_TEST_PCM) / PCM_BYTES_PER_SECOND,
        )

    async def cancel(self, entity_id: str, *, stop_active: bool = True) -> None:
        await self.speakers.stop(entity_id, stop_active=stop_active)

    @staticmethod
    def failure(err: Exception, context: str) -> dict[str, object]:
        error: dict[str, object] = {"type": type(err).__name__}
        if isinstance(err, SpeakerRequestError):
            error.update(
                {
                    "operation": err.operation,
                    "status": err.status,
                    "message": err.detail,
                }
            )
            LOGGER.warning(
                "%s failed: operation=%s status=%s detail=%s",
                context,
                err.operation,
                err.status,
                err.detail,
            )
        else:
            LOGGER.warning("%s failed: %s", context, type(err).__name__)
        return error

    async def _pump(self, encoder: ProgressiveMp3Encoder, item: MediaObject) -> None:
        try:
            async for chunk in encoder.chunks():
                self.media.append(item, chunk)
            self.media.finish(item)
        except BaseException:
            self.media.discard(item)
            raise

    def _publish_complete(self, encoded: bytes) -> str:
        token, item = self.media.create()
        self.media.append(item, encoded)
        self.media.finish(item)
        return self._media_url(token)

    def _media_url(self, token: str) -> str:
        # Sonos derives protocol metadata from the URI as well as HTTP headers.
        return f"{self.base_url}/media/{token}.mp3"
