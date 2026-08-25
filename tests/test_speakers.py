import asyncio
from contextlib import asynccontextmanager

import app.speakers
import pytest
from app.ha_apis import MediaPlayerState
from app.routes import OutputRoute
from app.speakers import SpeakerController, SpeakerPlaybackCancelled, SpeakerRequestError


class Response:
    def raise_for_status(self) -> None:
        return None


class RejectedResponse:
    status = 500
    reason = "Internal Server Error"

    async def text(self) -> str:
        return "Failed URL http://voice.test/media/sensitive-token: UPnP 714"


class Session:
    def __init__(self) -> None:
        self.requests = []

    @asynccontextmanager
    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs["json"]))
        await asyncio.sleep(0)
        yield Response()


@pytest.fixture(autouse=True)
def unavailable_state(monkeypatch):
    async def get_state(*args):
        return None

    monkeypatch.setattr(app.speakers, "get_media_player_state", get_state)


async def test_latest_response_replaces_directly_and_preserves_options() -> None:
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(
        sink="media_player",
        entity_id="media_player.sonos",
        announce=False,
        volume=0.25,
    )

    first = await controller.play(route, "http://voice.test/first")
    second = await controller.play(route, "http://voice.test/second")

    assert first.replaced_active_playback is False
    assert second.replaced_active_playback is False
    assert len(session.requests) == 2
    assert all(request[0].endswith("/play_media") for request in session.requests)
    assert session.requests[1][1] == {
        "entity_id": "media_player.sonos",
        "media_content_id": "http://voice.test/second",
        "media_content_type": "music",
        "announce": False,
        "extra": {"volume": 0.25},
    }


async def test_explicit_stop_clears_active_playback() -> None:
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first")
    await controller.stop("media_player.sonos")
    result = await controller.play(route, "http://voice.test/second")

    assert session.requests[1][0].endswith("/media_stop")
    assert result.replaced_active_playback is False
    assert session.requests[-1][0].endswith("/play_media")
    assert session.requests[-1][1]["media_content_id"] == "http://voice.test/second"


async def test_next_play_waits_for_prior_audio_duration(monkeypatch) -> None:
    now = 100.0
    waits = []

    async def advance(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    monkeypatch.setattr(app.speakers.asyncio, "sleep", advance)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=2)
    await controller.play(route, "http://voice.test/second", duration_seconds=1)

    assert round(sum(waits), 2) == 2.0
    assert len(session.requests) == 2


async def test_next_play_waits_for_progressive_completion_and_remaining_duration(
    monkeypatch,
) -> None:
    now = 100.0
    waits = []

    async def advance(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    monkeypatch.setattr(app.speakers.asyncio, "sleep", advance)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")
    completion = asyncio.get_running_loop().create_future()

    await controller.play(
        route,
        "http://voice.test/progressive",
        progressive_completion=completion,
    )
    queued = asyncio.create_task(controller.play(route, "http://voice.test/follow-up"))
    await asyncio.sleep(0)
    assert len(session.requests) == 1

    now += 0.5
    completion.set_result(2.0)
    await queued

    assert round(sum(waits), 2) == 1.5
    assert len(session.requests) == 2


async def test_stop_immediately_cancels_queued_wait(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=60)
    queued = asyncio.create_task(controller.play(route, "http://voice.test/queued"))
    await asyncio.sleep(0)

    await asyncio.wait_for(controller.stop("media_player.sonos"), timeout=0.1)

    result = (await asyncio.gather(queued, return_exceptions=True))[0]
    assert isinstance(result, SpeakerPlaybackCancelled)
    assert [url.rsplit("/", 1)[-1] for url, _ in session.requests] == [
        "play_media",
        "media_stop",
    ]


async def test_play_submitted_after_stop_queues_normally(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=60)
    queued = asyncio.create_task(controller.play(route, "http://voice.test/cancelled"))
    await asyncio.sleep(0)
    await controller.stop("media_player.sonos")
    replacement = await controller.play(route, "http://voice.test/replacement")
    await asyncio.gather(queued, return_exceptions=True)

    assert replacement.replaced_active_playback is False
    assert session.requests[-1][1]["media_content_id"] == "http://voice.test/replacement"


async def test_non_stopping_cancellation_preserves_progressive_sequence(
    monkeypatch,
) -> None:
    now = 100.0
    waits = []

    async def advance(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    monkeypatch.setattr(app.speakers.asyncio, "sleep", advance)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")
    completion = asyncio.get_running_loop().create_future()

    await controller.play(
        route,
        "http://voice.test/progressive",
        progressive_completion=completion,
    )
    completion.set_result(1.0)
    await controller.stop("media_player.sonos", stop_active=False)
    await controller.play(route, "http://voice.test/follow-up")

    assert round(sum(waits), 2) == 1.0
    assert [url.rsplit("/", 1)[-1] for url, _ in session.requests] == [
        "play_media",
        "play_media",
    ]


async def test_state_completion_releases_queue_without_duration_padding(monkeypatch) -> None:
    states = iter(
        [
            MediaPlayerState("playing", "http://voice.test/first"),
            MediaPlayerState("idle", "http://voice.test/first"),
        ]
    )

    async def get_state(*args):
        return next(states)

    monkeypatch.setattr(app.speakers, "get_media_player_state", get_state)
    controller = SpeakerController(Session(), "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=10)
    await controller.play(route, "http://voice.test/second")


async def test_delayed_startup_does_not_treat_initial_idle_as_completion(monkeypatch) -> None:
    observed = []
    states = iter(
        [
            MediaPlayerState("idle", None),
            MediaPlayerState("idle", None),
            MediaPlayerState("playing", "http://voice.test/first"),
            MediaPlayerState("paused", "http://voice.test/first"),
        ]
    )

    async def get_state(*args):
        state = next(states)
        observed.append(state.state)
        return state

    monkeypatch.setattr(app.speakers, "get_media_player_state", get_state)
    controller = SpeakerController(Session(), "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=1)
    await controller.play(route, "http://voice.test/second")

    assert observed == ["idle", "idle", "playing", "paused"]


async def test_stale_playing_state_releases_queue_at_maximum_timeout(monkeypatch) -> None:
    now = 100.0

    async def advance(delay: float) -> None:
        nonlocal now
        now += delay

    async def get_state(*args):
        return MediaPlayerState("playing", "http://voice.test/first")

    monkeypatch.setattr(app.speakers.time, "monotonic", lambda: now)
    monkeypatch.setattr(app.speakers.asyncio, "sleep", advance)
    monkeypatch.setattr(app.speakers, "get_media_player_state", get_state)
    controller = SpeakerController(Session(), "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=1)
    await controller.play(route, "http://voice.test/second")

    assert now == 120.0


async def test_replaced_playback_releases_queue_immediately(monkeypatch) -> None:
    async def get_state(*args):
        return MediaPlayerState("playing", "http://music.test/other")

    monkeypatch.setattr(app.speakers, "get_media_player_state", get_state)
    session = Session()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first", duration_seconds=30)
    await controller.play(route, "http://voice.test/second")

    assert len(session.requests) == 2


async def test_speaker_failure_is_actionable_and_redacts_media_token() -> None:
    try:
        await SpeakerController._check_response(RejectedResponse(), "play_media")  # type: ignore[arg-type]
    except SpeakerRequestError as err:
        assert err.operation == "play_media"
        assert err.status == 500
        assert err.detail == "Failed URL http://voice.test/media/[redacted]: UPnP 714"
    else:
        raise AssertionError("expected SpeakerRequestError")
