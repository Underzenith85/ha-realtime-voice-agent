import asyncio

from app.media import MediaStore
from app.playback import PCM_BYTES_PER_SECOND, SpeakerPlaybackCoordinator
from app.routes import OutputRoute
from app.speakers import PlaybackResult, SpeakerRequestError


class Speakers:
    def __init__(self) -> None:
        self.plays = []
        self.stops = []

    async def play(
        self, route, media_url, duration_seconds=0, progressive_completion=None
    ):
        self.plays.append((route, media_url, duration_seconds, progressive_completion))
        return PlaybackResult(request_latency_ms=12, replaced_active_playback=False)

    async def stop(self, entity_id, *, stop_active=True):
        self.stops.append((entity_id, stop_active))


class FailingSpeakers(Speakers):
    async def play(
        self, route, media_url, duration_seconds=0, progressive_completion=None
    ):
        raise SpeakerRequestError("play_media", 500, f"failed {media_url}")


class ProgressiveEncoder:
    def __init__(self) -> None:
        self.started = False
        self.finished = False
        self.cancelled = False
        self.writes = []
        self.queue = asyncio.Queue()

    @property
    def duration_seconds(self) -> float:
        return sum(map(len, self.writes)) / PCM_BYTES_PER_SECOND

    async def start(self) -> None:
        self.started = True

    async def write(self, chunk: bytes) -> None:
        self.writes.append(chunk)
        await self.queue.put(b"encoded:" + chunk)

    async def chunks(self):
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def finish(self) -> None:
        self.finished = True
        await self.queue.put(None)

    async def cancel(self) -> None:
        self.cancelled = True
        await self.queue.put(None)


async def test_buffered_playback_encodes_publishes_and_tracks_duration() -> None:
    media = MediaStore()
    speakers = Speakers()

    async def encode(pcm: bytes) -> bytes:
        return b"mp3:" + pcm

    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        media, speakers, "http://voice.test:8099", encode=encode
    )
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")
    pcm = b"a" * PCM_BYTES_PER_SECOND

    playback = await coordinator.play_buffered(route, pcm)

    assert playback.raw_audio is pcm
    assert playback.result.request_latency_ms == 12
    _, media_url, duration, completion = speakers.plays[0]
    assert media_url.startswith("http://voice.test:8099/media/")
    assert media_url.endswith(".mp3")
    assert duration == 1
    assert completion is None
    token = media_url.rsplit("/", 1)[-1].removesuffix(".mp3")
    item = media.inspect(token)
    assert item is not None
    assert b"".join(item.chunks) == b"mp3:" + pcm
    assert item.complete.is_set()


async def test_progressive_playback_owns_encoder_pump_and_cleanup() -> None:
    media = MediaStore()
    speakers = Speakers()
    encoder = ProgressiveEncoder()
    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        media,
        speakers,
        "http://voice.test:8099",
        progressive_factory=lambda: encoder,  # type: ignore[arg-type]
    )
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    playback = await coordinator.start_progressive(route)
    completion = speakers.plays[0][3]
    assert completion is playback.completion
    assert not completion.done()
    await playback.write(b"audio")
    await playback.finish()

    assert encoder.started is True
    assert encoder.finished is True
    assert completion.result() == len(b"audio") / PCM_BYTES_PER_SECOND
    media_url = speakers.plays[0][1]
    token = media_url.rsplit("/", 1)[-1].removesuffix(".mp3")
    item = media.inspect(token)
    assert item is not None
    assert b"".join(item.chunks) == b"encoded:audio"
    assert item.complete.is_set()


async def test_cancelled_progressive_playback_discards_media() -> None:
    media = MediaStore(max_items=1)
    speakers = Speakers()
    encoder = ProgressiveEncoder()
    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        media,
        speakers,
        "http://voice.test:8099",
        progressive_factory=lambda: encoder,  # type: ignore[arg-type]
    )
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    playback = await coordinator.start_progressive(route)
    await playback.write(b"partial")
    await playback.cancel()

    media_url = speakers.plays[0][1]
    token = media_url.rsplit("/", 1)[-1].removesuffix(".mp3")
    assert media.inspect(token) is None
    _, replacement = media.create()
    assert replacement is not None


async def test_route_test_browser_noops_and_cancel_delegates() -> None:
    speakers = Speakers()
    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        MediaStore(), speakers, "http://voice.test:8099"
    )

    result = await coordinator.test_output(OutputRoute())
    await coordinator.cancel("media_player.sonos")

    assert result is None
    assert speakers.plays == []
    assert speakers.stops == [("media_player.sonos", True)]


async def test_barge_in_cancels_queue_without_stopping_inactive_response() -> None:
    speakers = Speakers()
    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        MediaStore(), speakers, "http://voice.test:8099"
    )

    await coordinator.cancel("media_player.sonos", stop_active=False)

    assert speakers.stops == [("media_player.sonos", False)]


async def test_failed_progressive_start_cleans_up_encoder() -> None:
    encoder = ProgressiveEncoder()
    media = MediaStore(max_items=1)
    coordinator = SpeakerPlaybackCoordinator(  # type: ignore[arg-type]
        media,
        FailingSpeakers(),
        "http://voice.test:8099",
        progressive_factory=lambda: encoder,  # type: ignore[arg-type]
    )
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    try:
        await coordinator.start_progressive(route)
    except SpeakerRequestError:
        pass
    else:
        raise AssertionError("expected progressive playback to fail")

    assert encoder.cancelled is True
    _, replacement = media.create()
    assert replacement is not None


def test_failure_serialization_is_shared_and_sanitized() -> None:
    error = SpeakerPlaybackCoordinator.failure(
        SpeakerRequestError("play_media", 500, "failed http://voice.test/media/sensitive.mp3"),
        "Route test",
    )

    assert error == {
        "type": "SpeakerRequestError",
        "operation": "play_media",
        "status": 500,
        "message": "failed http://voice.test/media/[redacted].mp3",
    }
