import json

import pytest
from app.routes import OutputRoute, RouteStore


def test_route_round_trip(tmp_path) -> None:
    path = tmp_path / "routes.json"
    store = RouteStore(path)
    route = OutputRoute(sink="media_player", entity_id="media_player.kitchen", mode="progressive")
    store.set("browser-1", route)

    loaded = RouteStore(path)
    loaded.load()
    assert loaded.get("browser-1") == route
    assert json.loads(path.read_text())["browser-1"]["announce"] is True


def test_route_requires_media_player_entity() -> None:
    with pytest.raises(ValueError):
        OutputRoute(sink="media_player", entity_id="light.kitchen").validate()


def test_route_validates_volume() -> None:
    with pytest.raises(ValueError):
        OutputRoute(volume=2).validate()
