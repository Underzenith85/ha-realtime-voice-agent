"""Short-lived media objects for HA speaker playback."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class MediaObject:
    expires_at: float
    complete: asyncio.Event = field(default_factory=asyncio.Event)
    chunks: list[bytes] = field(default_factory=list)
    subscribers: set[asyncio.Queue[bytes | None]] = field(default_factory=set)


class MediaStore:
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, MediaObject] = {}

    def create(self) -> tuple[str, MediaObject]:
        token = secrets.token_urlsafe(32)
        item = MediaObject(expires_at=time.monotonic() + self.ttl_seconds)
        self._items[token] = item
        return token, item

    def claim(self, token: str) -> MediaObject | None:
        """Return media without consuming it so network players may retry the URL."""
        return self.inspect(token)

    def inspect(self, token: str) -> MediaObject | None:
        """Return unexpired media metadata without consuming the signed URL."""
        item = self._items.get(token)
        if item is None:
            return None
        if item.expires_at < time.monotonic():
            self._items.pop(token, None)
            return None
        return item

    def append(self, item: MediaObject, chunk: bytes) -> None:
        item.chunks.append(chunk)
        for queue in tuple(item.subscribers):
            queue.put_nowait(chunk)

    def finish(self, item: MediaObject) -> None:
        item.complete.set()
        for queue in tuple(item.subscribers):
            queue.put_nowait(None)

    def cleanup(self) -> None:
        now = time.monotonic()
        for token, item in tuple(self._items.items()):
            if item.expires_at < now:
                self._items.pop(token, None)
