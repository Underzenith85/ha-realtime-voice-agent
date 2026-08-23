import json

from app.config import Settings


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
