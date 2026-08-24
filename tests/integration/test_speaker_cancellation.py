import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from app.routes import OutputRoute
from app.speakers import SpeakerController, SpeakerPlaybackCancelled

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_cancel_stops_active_playback_and_interrupts_queued_wait() -> None:
    requests: list[str] = []

    async def record(request: web.Request) -> web.Response:
        requests.append(request.path.rsplit("/", 1)[-1])
        return web.json_response([])

    app = web.Application()
    app.router.add_post("/api/services/media_player/play_media", record)
    app.router.add_post("/api/services/media_player/media_stop", record)

    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        controller = SpeakerController(session, str(server.make_url("")), "secret")
        route = OutputRoute(sink="media_player", entity_id="media_player.sonos")
        await controller.play(route, "http://voice.test/first", duration_seconds=60)
        queued = asyncio.create_task(controller.play(route, "http://voice.test/queued"))
        await asyncio.sleep(0)

        await asyncio.wait_for(controller.stop("media_player.sonos"), timeout=0.5)
        result = (await asyncio.gather(queued, return_exceptions=True))[0]

    assert isinstance(result, SpeakerPlaybackCancelled)
    assert requests == ["play_media", "media_stop"]
