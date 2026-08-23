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
DEFAULT_HISTORY_TURN_LIMIT = 20
DEFAULT_INPUT_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
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
    call_timeout_seconds: float = 30
    expose_all_tools: bool = False
    managed_by_home_assistant: bool = False

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
            call_timeout_seconds=float(raw.get("call_timeout_seconds", 30)),
        )


@dataclass(frozen=True, slots=True)
class HardwareClientConfig:
    client_id: str
    name: str
    token_sha256: str
    entity_id: str | None = None
    mode: str = "buffered"
    announce: bool = True

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HardwareClientConfig:
        client_id = str(raw["client_id"]).strip()
        if not client_id or len(client_id) > 128:
            raise ValueError("hardware client client_id must contain 1-128 characters")
        digest = str(raw["token_sha256"]).lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("hardware client token_sha256 must be a SHA-256 hex digest")
        mode = str(raw.get("mode", "buffered"))
        if mode not in {"buffered", "progressive"}:
            raise ValueError("hardware client mode must be buffered or progressive")
        return cls(
            client_id=client_id,
            name=str(raw.get("name", client_id))[:80],
            token_sha256=digest,
            entity_id=raw.get("entity_id"),
            mode=mode,
            announce=bool(raw.get("announce", True)),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    openai_api_key: str
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    instructions: str = DEFAULT_INSTRUCTIONS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT
    history_turn_limit: int = DEFAULT_HISTORY_TURN_LIMIT
    input_transcription_model: str = DEFAULT_INPUT_TRANSCRIPTION_MODEL
    ha_mcp_url: str = DEFAULT_HA_MCP_URL
    ha_api_url: str = DEFAULT_HA_API_URL
    openai_realtime_url: str = DEFAULT_OPENAI_REALTIME_URL
    speaker_base_url: str = DEFAULT_SPEAKER_BASE_URL
    routes_path: str = "/data/routes.json"
    timers_path: str = "/data/timers.json"
    tool_timeout_seconds: float = 30
    session_rate_limit_per_minute: int = 30
    media_rate_limit_per_minute: int = 60
    tool_rate_limit_per_minute: int = 60
    shared_tool_concurrency: int = 16
    mcp_servers: tuple[McpServerConfig, ...] = ()
    hardware_clients: tuple[HardwareClientConfig, ...] = ()

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
                    expose_all_tools=True,
                ),
            )
        hardware_clients = tuple(
            HardwareClientConfig.from_dict(item) for item in raw.get("hardware_clients", [])
        )
        client_ids = [client.client_id for client in hardware_clients]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("hardware client IDs must be unique")
        if len({client.token_sha256 for client in hardware_clients}) != len(hardware_clients):
            raise ValueError("hardware client credentials must be unique")

        return cls(
            openai_api_key=api_key,
            model=raw.get("model", DEFAULT_MODEL),
            voice=raw.get("voice", DEFAULT_VOICE),
            instructions=raw.get("instructions", DEFAULT_INSTRUCTIONS),
            max_sessions=int(raw.get("max_sessions", DEFAULT_MAX_SESSIONS)),
            idle_timeout_seconds=int(raw.get("idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT)),
            history_turn_limit=int(raw.get("history_turn_limit", DEFAULT_HISTORY_TURN_LIMIT)),
            input_transcription_model=raw.get(
                "input_transcription_model", DEFAULT_INPUT_TRANSCRIPTION_MODEL
            ),
            ha_mcp_url=raw.get("ha_mcp_url", DEFAULT_HA_MCP_URL),
            ha_api_url=raw.get("ha_api_url", DEFAULT_HA_API_URL),
            speaker_base_url=raw.get("speaker_base_url", DEFAULT_SPEAKER_BASE_URL),
            session_rate_limit_per_minute=int(raw.get("session_rate_limit_per_minute", 30)),
            media_rate_limit_per_minute=int(raw.get("media_rate_limit_per_minute", 60)),
            tool_rate_limit_per_minute=int(raw.get("tool_rate_limit_per_minute", 60)),
            shared_tool_concurrency=int(raw.get("shared_tool_concurrency", 16)),
            mcp_servers=tuple(servers),
            hardware_clients=hardware_clients,
        )
