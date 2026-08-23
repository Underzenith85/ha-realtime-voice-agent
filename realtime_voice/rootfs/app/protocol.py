"""Browser wire protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
INPUT_SAMPLE_RATE = 24_000


class ProtocolError(ValueError):
    """Raised for malformed client control messages."""


@dataclass(frozen=True, slots=True)
class Hello:
    client_id: str
    name: str
    client_type: str = "browser"


def parse_hello(message: dict[str, Any]) -> Hello:
    if message.get("type") != "hello":
        raise ProtocolError("first message must be hello")
    if message.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    client_id = str(message.get("client_id", "")).strip()
    if not client_id or len(client_id) > 128:
        raise ProtocolError("invalid client_id")
    client_type = str(message.get("client_type", "browser"))
    if client_type not in {"browser", "voice_pe"}:
        raise ProtocolError("unsupported client type")
    return Hello(
        client_id=client_id,
        name=str(message.get("name", "Browser"))[:80],
        client_type=client_type,
    )
