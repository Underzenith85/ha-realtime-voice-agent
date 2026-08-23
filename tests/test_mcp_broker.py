from app.mcp_broker import ToolBinding


def test_realtime_tool_definition() -> None:
    binding = ToolBinding(
        public_name="mcp_homeassistant_turn_on",
        server_name="homeassistant",
        remote_name="HassTurnOn",
        description="Turn on an exposed entity",
        schema={"type": "object", "properties": {"name": {"type": "string"}}},
    )

    assert binding.realtime_definition() == {
        "type": "function",
        "name": "mcp_homeassistant_turn_on",
        "description": "Turn on an exposed entity",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    }
