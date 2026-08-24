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
    def __init__(
        self,
        ttl_seconds: float = 300,
        *,
        max_items: int = 64,
        cleanup_interval_seconds: float = 30,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must not be negative")
        if max_items < 1:
            raise ValueError("max_items must be positive")
        if cleanup_interval_seconds <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self._items: dict[str, MediaObject] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start proactive expiration for this store."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="media-store-cleanup")

    async def close(self) -> None:
        """Stop proactive expiration and wait for its task to finish."""
        task = self._cleanup_task
        self._cleanup_task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def create(self) -> tuple[str, MediaObject]:
        self.cleanup()
        while len(self._items) >= self.max_items:
            if not self._evict_oldest_complete():
                raise RuntimeError("media store capacity is occupied by active streams")
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
        if item.complete.is_set() and item.expires_at <= time.monotonic():
            self._items.pop(token, None)
            return None
        return item

    def append(self, item: MediaObject, chunk: bytes) -> None:
        item.chunks.append(chunk)
        for queue in tuple(item.subscribers):
            queue.put_nowait(chunk)

    def finish(self, item: MediaObject) -> None:
        # Give players the full retry window after a progressive stream completes,
        # even when encoding took longer than the original TTL.
        item.expires_at = time.monotonic() + self.ttl_seconds
        item.complete.set()
        for queue in tuple(item.subscribers):
            queue.put_nowait(None)

    def discard(self, item: MediaObject) -> None:
        """Remove an abandoned media object without affecting a replacement."""
        for token, stored_item in tuple(self._items.items()):
            if stored_item is item:
                self._items.pop(token, None)
                break
        item.complete.set()
        for queue in tuple(item.subscribers):
            queue.put_nowait(None)

    def cleanup(self) -> None:
        now = time.monotonic()
        for token, item in tuple(self._items.items()):
            if item.complete.is_set() and item.expires_at <= now:
                self._items.pop(token, None)

    def _evict_oldest_complete(self) -> bool:
        completed = (
            (item.expires_at, token)
            for token, item in self._items.items()
            if item.complete.is_set()
        )
        try:
            _, token = min(completed)
        except ValueError:
            return False
        self._items.pop(token)
        return True

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            self.cleanup()
