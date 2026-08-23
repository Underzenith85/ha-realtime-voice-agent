import asyncio
from contextlib import asynccontextmanager

from app.routes import OutputRoute
from app.speakers import SpeakerController


class Response:
    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self) -> None:
        self.requests = []

    @asynccontextmanager
    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs["json"]))
        await asyncio.sleep(0)
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
        "media_content_type": "audio/mpeg",
        "announce": False,
        "extra": {"volume": 0.25},
    }
