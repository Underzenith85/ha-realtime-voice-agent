import pytest
from app.protocol import ProtocolError, parse_hello


def test_parses_hello() -> None:
    hello = parse_hello({"type": "hello", "protocol": 1, "client_id": "kitchen"})
    assert hello.client_id == "kitchen"
    assert hello.client_type == "browser"


def test_parses_voice_pe_hello() -> None:
    hello = parse_hello(
        {
            "type": "hello",
            "protocol": 1,
            "client_id": "kitchen",
            "client_type": "voice_pe",
        }
    )
    assert hello.client_type == "voice_pe"


@pytest.mark.parametrize(
    "message",
    [
        {"type": "ptt_start", "protocol": 1, "client_id": "x"},
        {"type": "hello", "protocol": 99, "client_id": "x"},
        {"type": "hello", "protocol": 1, "client_id": ""},
        {"type": "hello", "protocol": 1, "client_id": "x", "client_type": "watch"},
    ],
)
def test_rejects_bad_hello(message: dict) -> None:
    with pytest.raises(ProtocolError):
        parse_hello(message)
