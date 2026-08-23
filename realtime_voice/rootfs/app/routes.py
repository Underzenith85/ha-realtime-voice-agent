"""Persistent per-client output routes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OutputRoute:
    sink: str = "browser"
    entity_id: str | None = None
    mode: str = "buffered"
    announce: bool = True
    volume: float | None = None
    progressive_fallback: bool = True

    def validate(self) -> OutputRoute:
        if self.sink not in {"browser", "media_player"}:
            raise ValueError("invalid sink")
        if self.mode not in {"buffered", "progressive"}:
            raise ValueError("invalid playback mode")
        if self.sink == "media_player" and not (self.entity_id or "").startswith("media_player."):
            raise ValueError("media_player sink requires a media_player entity")
        if self.volume is not None and not 0 <= self.volume <= 1:
            raise ValueError("volume must be between 0 and 1")
        return self


class RouteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._routes: dict[str, OutputRoute] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        self._routes = {key: OutputRoute(**value).validate() for key, value in raw.items()}

    def get(self, client_id: str) -> OutputRoute:
        return self._routes.get(client_id, OutputRoute())

    def set(self, client_id: str, route: OutputRoute) -> None:
        self._routes[client_id] = route.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({key: asdict(value) for key, value in self._routes.items()}))
        tmp.replace(self.path)
