import pytest
from app.protocol import ProtocolError, parse_hello


def test_parses_hello() -> None:
    hello = parse_hello({"type": "hello", "protocol": 1, "client_id": "kitchen"})
    assert hello.client_id == "kitchen"


@pytest.mark.parametrize(
    "message",
    [
        {"type": "ptt_start", "protocol": 1, "client_id": "x"},
        {"type": "hello", "protocol": 99, "client_id": "x"},
        {"type": "hello", "protocol": 1, "client_id": ""},
    ],
)
def test_rejects_bad_hello(message: dict) -> None:
    with pytest.raises(ProtocolError):
        parse_hello(message)
