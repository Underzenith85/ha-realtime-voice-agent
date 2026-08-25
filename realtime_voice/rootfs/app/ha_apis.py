"""Discover Home Assistant LLM APIs while keeping their credentials inside HA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from app.config import McpServerConfig


@dataclass(frozen=True, slots=True)
class MediaPlayerState:
    """The small part of an HA media-player state needed for queue sequencing."""

    state: str
    media_content_id: str | None


async def get_media_player_state(
    session: aiohttp.ClientSession,
    base_url: str,
    headers: dict[str, str],
    entity_id: str,
) -> MediaPlayerState | None:
    """Read a media player state, returning None when HA no longer has the entity."""
    url = f"{base_url.rstrip('/')}/api/states/{quote(entity_id, safe='')}"
    async with session.get(url, headers=headers) as response:
        if response.status == 404:
            return None
        response.raise_for_status()
        payload: dict[str, Any] = await response.json()
    attributes = payload.get("attributes")
    media_content_id = attributes.get("media_content_id") if isinstance(attributes, dict) else None
    return MediaPlayerState(
        state=str(payload.get("state", "unknown")).lower(),
        media_content_id=media_content_id if isinstance(media_content_id, str) else None,
    )


async def discover_managed_mcp_configs(
    session: aiohttp.ClientSession, base_url: str, supervisor_token: str
) -> tuple[McpServerConfig, ...]:
    """Return MCP APIs registered by HA's OAuth-capable MCP client integration."""
    if not supervisor_token:
        return ()
    websocket_url = (
        base_url.rstrip("/").replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        + "/api/websocket"
    )
    async with session.ws_connect(websocket_url, heartbeat=20) as websocket:
        required = await websocket.receive_json()
        if required.get("type") != "auth_required":
            raise ConnectionError("unexpected Home Assistant WebSocket authentication state")
        await websocket.send_json({"type": "auth", "access_token": supervisor_token})
        authenticated = await websocket.receive_json()
        if authenticated.get("type") != "auth_ok":
            raise PermissionError("Home Assistant WebSocket authentication failed")
        await websocket.send_json({"id": 1, "type": "llm/api/list"})
        response: dict[str, Any] = await websocket.receive_json()
        if not response.get("success"):
            raise ConnectionError("Home Assistant LLM API discovery failed")

    configs = []
    for api in response.get("result", []):
        api_id = api.get("id", "")
        if not isinstance(api_id, str) or not api_id.startswith("mcp-"):
            continue
        configs.append(
            McpServerConfig(
                name=f"ha_oauth_{api_id}",
                url=f"{base_url.rstrip('/')}/api/mcp/{quote(api_id, safe='')}",
                headers={"Authorization": f"Bearer {supervisor_token}"},
                expose_all_tools=True,
                managed_by_home_assistant=True,
            )
        )
    return tuple(configs)
