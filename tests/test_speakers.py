import asyncio
from contextlib import asynccontextmanager

import aiohttp
from app.routes import OutputRoute
from app.speakers import SpeakerController, SpeakerRequestError


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


class StopRejectingSession(Session):
    @asynccontextmanager
    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs["json"]))
        await asyncio.sleep(0)
        if url.endswith("/media_stop"):
            raise aiohttp.ClientConnectionError("speaker already idle")
        yield Response()


async def test_latest_response_stops_previous_playback_and_preserves_options() -> None:
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
    assert second.replaced_active_playback is True
    assert session.requests[1][0].endswith("/media_stop")
    assert session.requests[2][1] == {
        "entity_id": "media_player.sonos",
        "media_content_id": "http://voice.test/second",
        "media_content_type": "music",
        "announce": False,
        "extra": {"volume": 0.25},
    }


async def test_rejected_stale_stop_does_not_suppress_new_playback() -> None:
    session = StopRejectingSession()
    controller = SpeakerController(session, "http://ha.test", "secret")  # type: ignore[arg-type]
    route = OutputRoute(sink="media_player", entity_id="media_player.sonos")

    await controller.play(route, "http://voice.test/first")
    result = await controller.play(route, "http://voice.test/second")

    assert result.replaced_active_playback is True
    assert session.requests[-1][0].endswith("/play_media")
    assert session.requests[-1][1]["media_content_id"] == "http://voice.test/second"


async def test_speaker_failure_is_actionable_and_redacts_media_token() -> None:
    try:
        await SpeakerController._check_response(RejectedResponse(), "play_media")  # type: ignore[arg-type]
    except SpeakerRequestError as err:
        assert err.operation == "play_media"
        assert err.status == 500
        assert err.detail == "Failed URL http://voice.test/media/[redacted]: UPnP 714"
    else:
        raise AssertionError("expected SpeakerRequestError")
