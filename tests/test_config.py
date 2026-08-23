import hashlib
import json

import pytest
from app.config import HardwareClientConfig, Settings


def test_settings_loads_servers_and_supervisor_token(tmp_path, monkeypatch) -> None:
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "openai_api_key": "secret",
                "mcp_servers": [
                    {
                        "name": "tasks",
                        "url": "https://example.test/mcp",
                        "transport": "streamable_http",
                        "token": "mcp-secret",
                        "allowed_tools": ["list_tasks"],
                        "headers": [],
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("SUPERVISOR_TOKEN", "ha-secret")

    settings = Settings.load(path)

    assert settings.model == "gpt-realtime-2.1"
    assert settings.mcp_servers[0].name == "homeassistant"
    assert settings.mcp_servers[1].headers["Authorization"] == "Bearer mcp-secret"


def test_settings_loads_hardware_client_digest_and_defaults(tmp_path) -> None:
    token = "device-secret"
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "openai_api_key": "secret",
                "hardware_clients": [
                    {
                        "client_id": "kitchen-voice-pe",
                        "name": "Kitchen Voice PE",
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "entity_id": "media_player.kitchen",
                    }
                ],
            }
        )
    )

    client = Settings.load(path).hardware_clients[0]

    assert client.client_id == "kitchen-voice-pe"
    assert client.entity_id == "media_player.kitchen"
    assert client.mode == "buffered"
    assert client.announce is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_id": ""},
        {"token_sha256": "not-a-digest"},
        {"mode": "unknown"},
    ],
)
def test_rejects_invalid_hardware_client(overrides) -> None:
    raw = {
        "client_id": "voice-pe",
        "name": "Voice PE",
        "token_sha256": "a" * 64,
        **overrides,
    }

    with pytest.raises(ValueError):
        HardwareClientConfig.from_dict(raw)
