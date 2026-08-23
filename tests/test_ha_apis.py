from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from app.ha_apis import discover_managed_mcp_configs


async def test_discovers_only_ha_managed_mcp_apis_without_public_credentials() -> None:
    async def websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        assert await ws.receive_json() == {"type": "auth", "access_token": "super-secret"}
        await ws.send_json({"type": "auth_ok"})
        assert await ws.receive_json() == {"id": 1, "type": "llm/api/list"}
        await ws.send_json(
            {
                "id": 1,
                "type": "result",
                "success": True,
                "result": [
                    {"id": "assist", "name": "Assist"},
                    {"id": "mcp-oauth-entry", "name": "Tasks"},
                ],
            }
        )
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", websocket)
    async with TestClient(TestServer(app)) as client:
        configs = await discover_managed_mcp_configs(
            client.session, str(client.make_url("/")).rstrip("/"), "super-secret"
        )

    assert len(configs) == 1
    config = configs[0]
    assert config.name == "ha_oauth_mcp-oauth-entry"
    assert config.url.endswith("/api/mcp/mcp-oauth-entry")
    assert config.expose_all_tools is True
    assert config.managed_by_home_assistant is True
    assert config.headers["Authorization"] == "Bearer super-secret"


async def test_no_supervisor_token_skips_discovery() -> None:
    assert await discover_managed_mcp_configs(None, "http://ha.test", "") == ()  # type: ignore[arg-type]
