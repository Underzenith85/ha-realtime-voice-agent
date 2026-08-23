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
