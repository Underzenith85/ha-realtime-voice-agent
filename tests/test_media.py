import app.media
from app.media import MediaStore


def test_media_lifecycle() -> None:
    store = MediaStore()
    token, item = store.create()
    store.append(item, b"one")
    store.append(item, b"two")
    store.finish(item)

    assert store.get(token) is item
    assert b"".join(item.chunks) == b"onetwo"
    assert item.complete.is_set()


def test_unknown_media_token() -> None:
    assert MediaStore().get("missing") is None


def test_expired_media_token_is_removed(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(app.media.time, "monotonic", lambda: now)
    store = MediaStore(ttl_seconds=5)
    token, _ = store.create()

    now = 1006.0

    assert store.get(token) is None
    assert token not in store._items
