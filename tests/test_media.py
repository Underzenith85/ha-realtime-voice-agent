import asyncio

import app.media
from app.media import MediaStore


def test_media_lifecycle() -> None:
    store = MediaStore()
    token, item = store.create()
    store.append(item, b"one")
    store.append(item, b"two")
    store.finish(item)

    assert store.claim(token) is item
    assert store.claim(token) is item
    assert b"".join(item.chunks) == b"onetwo"
    assert item.complete.is_set()


def test_unknown_media_token() -> None:
    assert MediaStore().claim("missing") is None


def test_expired_media_token_is_removed(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(app.media.time, "monotonic", lambda: now)
    store = MediaStore(ttl_seconds=5)
    token, item = store.create()
    store.finish(item)

    now = 1006.0

    assert store.claim(token) is None
    assert token not in store._items


def test_expired_active_stream_is_retained_until_complete(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(app.media.time, "monotonic", lambda: now)
    store = MediaStore(ttl_seconds=5)
    token, item = store.create()

    now = 1006.0
    store.cleanup()

    assert store.inspect(token) is item
    store.finish(item)
    store.cleanup()
    assert store.inspect(token) is item

    now = 1012.0
    store.cleanup()
    assert store.inspect(token) is None


def test_capacity_evicts_oldest_completed_media() -> None:
    store = MediaStore(max_items=2)
    oldest_token, oldest = store.create()
    store.finish(oldest)
    retained_token, retained = store.create()

    newest_token, newest = store.create()

    assert store.inspect(oldest_token) is None
    assert store.inspect(retained_token) is retained
    assert store.inspect(newest_token) is newest
    assert len(store._items) == 2


def test_capacity_does_not_evict_active_streams() -> None:
    store = MediaStore(max_items=1)
    active_token, active = store.create()

    try:
        store.create()
    except RuntimeError as err:
        assert str(err) == "media store capacity is occupied by active streams"
    else:
        raise AssertionError("expected active streams to prevent capacity eviction")

    assert store.inspect(active_token) is active


async def test_scheduled_cleanup_expires_media_and_stops_cleanly() -> None:
    store = MediaStore(ttl_seconds=0, cleanup_interval_seconds=0.001)
    token, item = store.create()
    store.finish(item)

    await store.start()
    cleanup_task = store._cleanup_task
    try:
        for _ in range(10):
            if token not in store._items:
                break
            await asyncio.sleep(0.001)
        assert token not in store._items
    finally:
        await store.close()
    assert cleanup_task is not None
    assert cleanup_task.done()
    assert store._cleanup_task is None


def test_inspection_does_not_consume_media() -> None:
    store = MediaStore()
    token, item = store.create()
    store.append(item, b"audio")
    store.finish(item)

    assert store.inspect(token) is item
    assert store.inspect(token) is item
    assert store.claim(token) is item
    assert store.inspect(token) is item
