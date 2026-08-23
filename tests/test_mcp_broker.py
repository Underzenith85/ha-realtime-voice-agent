import pytest
from app.mcp_broker import ToolBinding, _validated_schema


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


@pytest.mark.parametrize(
    "schema",
    [
        None,
        {"type": "string"},
        {"type": "object", "properties": "not-an-object"},
    ],
)
def test_invalid_mcp_tool_schema_is_rejected(schema) -> None:
    with pytest.raises(ValueError, match=r"invalid input schema.*fake\.broken"):
        _validated_schema(schema, "fake", "broken")
