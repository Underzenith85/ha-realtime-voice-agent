"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_INSTRUCTIONS = "You are a concise Home Assistant voice agent."
DEFAULT_MAX_SESSIONS = 4
DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_HA_MCP_URL = "http://supervisor/core/api/mcp/assist"
DEFAULT_HA_API_URL = "http://supervisor/core"
DEFAULT_OPENAI_REALTIME_URL = "https://api.openai.com/v1/realtime"
DEFAULT_SPEAKER_BASE_URL = "http://homeassistant.local:8099"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    name: str
    url: str
    transport: str = "streamable_http"
    token: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    allowed_tools: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> McpServerConfig:
        headers = {item["name"]: item["value"] for item in raw.get("headers", [])}
        if token := raw.get("token"):
            headers.setdefault("Authorization", f"Bearer {token}")
        return cls(
            name=raw["name"],
            url=raw["url"],
            transport=raw.get("transport", "streamable_http"),
            token=raw.get("token"),
            headers=headers,
            allowed_tools=frozenset(raw.get("allowed_tools", [])),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    openai_api_key: str
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    instructions: str = DEFAULT_INSTRUCTIONS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT
    ha_mcp_url: str = DEFAULT_HA_MCP_URL
    ha_api_url: str = DEFAULT_HA_API_URL
    openai_realtime_url: str = DEFAULT_OPENAI_REALTIME_URL
    speaker_base_url: str = DEFAULT_SPEAKER_BASE_URL
    routes_path: str = "/data/routes.json"
    tool_timeout_seconds: float = 30
    mcp_servers: tuple[McpServerConfig, ...] = ()

    @classmethod
    def load(cls, path: str | Path = "/data/options.json") -> Settings:
        raw = json.loads(Path(path).read_text())
        api_key = raw.get("openai_api_key", "").strip()
        if not api_key:
            raise ValueError("openai_api_key is required")
        supervisor_token = os.getenv("SUPERVISOR_TOKEN")
        servers = [McpServerConfig.from_dict(item) for item in raw.get("mcp_servers", [])]
        if supervisor_token:
            servers.insert(
                0,
                McpServerConfig(
                    name="homeassistant",
                    url=raw.get("ha_mcp_url", DEFAULT_HA_MCP_URL),
                    headers={"Authorization": f"Bearer {supervisor_token}"},
                ),
            )
        return cls(
            openai_api_key=api_key,
            model=raw.get("model", DEFAULT_MODEL),
            voice=raw.get("voice", DEFAULT_VOICE),
            instructions=raw.get("instructions", DEFAULT_INSTRUCTIONS),
            max_sessions=int(raw.get("max_sessions", DEFAULT_MAX_SESSIONS)),
            idle_timeout_seconds=int(raw.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT)),
            ha_mcp_url=raw.get("ha_mcp_url", DEFAULT_HA_MCP_URL),
            speaker_base_url=raw.get("speaker_base_url", DEFAULT_SPEAKER_BASE_URL),
            mcp_servers=tuple(servers),
        )
