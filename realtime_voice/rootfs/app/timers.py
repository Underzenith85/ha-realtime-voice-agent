"""Persistent, client-scoped voice timers."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CompletionCallback = Callable[["VoiceTimer"], Awaitable[None]]


@dataclass(slots=True)
class VoiceTimer:
    timer_id: str
    client_id: str
    label: str
    due_at: float
    state: str = "active"
    remaining_seconds: float | None = None
    kind: str = "timer"

    def public(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        remaining = self.remaining_seconds if self.state == "paused" else max(0, self.due_at - now)
        return {
            "timer_id": self.timer_id,
            "label": self.label,
            "kind": self.kind,
            "state": self.state,
            "remaining_seconds": round(remaining or 0),
        }


class TimerManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.timers: dict[str, VoiceTimer] = {}
        self.pending: dict[str, list[VoiceTimer]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._callbacks: dict[str, CompletionCallback] = {}
        self._deliveries: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.timers = {item["timer_id"]: VoiceTimer(**item) for item in raw.get("timers", [])}
            self.pending = {
                key: [VoiceTimer(**item) for item in items]
                for key, items in raw.get("pending", {}).items()
            }
        for timer in tuple(self.timers.values()):
            if timer.state == "active":
                self._schedule(timer)

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for task in self._deliveries:
            task.cancel()
        await asyncio.gather(*self._deliveries, return_exceptions=True)
        self._deliveries.clear()

    def register(self, client_id: str, callback: CompletionCallback) -> None:
        self._callbacks[client_id] = callback
        pending = self.pending.pop(client_id, [])
        for timer in pending:
            task = asyncio.create_task(self._deliver(timer, callback))
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)
        if pending:
            self._save()

    async def _deliver(self, timer: VoiceTimer, callback: CompletionCallback) -> None:
        try:
            await callback(timer)
        except asyncio.CancelledError:
            self.pending.setdefault(timer.client_id, []).append(timer)
            self._save()
            raise
        except Exception:
            self.pending.setdefault(timer.client_id, []).append(timer)
            self._save()

    def unregister(self, client_id: str, callback: CompletionCallback) -> None:
        if self._callbacks.get(client_id) is callback:
            self._callbacks.pop(client_id, None)

    async def call(self, name: str, client_id: str, arguments: dict[str, Any]) -> str:
        operation = name.removeprefix("voice_timer_")
        if name == "voice_alarm_create":
            fire_at = datetime.fromisoformat(arguments["fire_at"].replace("Z", "+00:00"))
            if fire_at.tzinfo is None:
                raise ValueError("fire_at must include a timezone")
            duration = round(fire_at.timestamp() - time.time())
            timer = self.create(client_id, duration, arguments.get("label"), kind="alarm")
            result = timer.public()
        elif operation == "create":
            timer = self.create(
                client_id, int(arguments["duration_seconds"]), arguments.get("label")
            )
            result: Any = timer.public()
        elif operation == "list":
            result = self.list(client_id)
        else:
            timer = self._resolve(client_id, arguments)
            if operation == "cancel":
                self.cancel(timer)
                result = {"cancelled": timer.public()}
            elif operation == "pause":
                self.pause(timer)
                result = {"paused": timer.public()}
            elif operation == "resume":
                self.resume(timer)
                result = {"resumed": timer.public()}
            else:
                raise KeyError("unknown timer operation")
        return json.dumps(result, separators=(",", ":"))

    def create(
        self, client_id: str, duration: int, label: str | None = None, *, kind: str = "timer"
    ) -> VoiceTimer:
        if not 1 <= duration <= 604800:
            raise ValueError("duration_seconds must be between 1 and 604800")
        if label is not None and (not label.strip() or len(label) > 100):
            raise ValueError("label must contain 1 to 100 characters")
        timer = VoiceTimer(
            uuid.uuid4().hex,
            client_id,
            label or kind.title(),
            time.time() + duration,
            kind=kind,
        )
        self.timers[timer.timer_id] = timer
        self._schedule(timer)
        self._save()
        return timer

    def list(self, client_id: str) -> list[dict[str, Any]]:
        timers = sorted(
            (timer for timer in self.timers.values() if timer.client_id == client_id),
            key=lambda timer: timer.due_at,
        )
        return [{"position": index, **timer.public()} for index, timer in enumerate(timers, 1)]

    def cancel(self, timer: VoiceTimer) -> None:
        task = self._tasks.pop(timer.timer_id, None)
        if task:
            task.cancel()
        self.timers.pop(timer.timer_id, None)
        self._save()

    def pause(self, timer: VoiceTimer) -> None:
        if timer.state != "active":
            raise ValueError("timer is not active")
        timer.remaining_seconds = max(0, timer.due_at - time.time())
        timer.state = "paused"
        task = self._tasks.pop(timer.timer_id, None)
        if task:
            task.cancel()
        self._save()

    def resume(self, timer: VoiceTimer) -> None:
        if timer.state != "paused":
            raise ValueError("timer is not paused")
        timer.due_at = time.time() + (timer.remaining_seconds or 0)
        timer.remaining_seconds = None
        timer.state = "active"
        self._schedule(timer)
        self._save()

    def _resolve(self, client_id: str, arguments: dict[str, Any]) -> VoiceTimer:
        owned = [timer for timer in self.timers.values() if timer.client_id == client_id]
        if timer_id := arguments.get("timer_id"):
            timer = self.timers.get(timer_id)
            if timer and timer.client_id == client_id:
                return timer
        if position := arguments.get("position"):
            ordered = sorted(owned, key=lambda timer: timer.due_at)
            if 1 <= int(position) <= len(ordered):
                return ordered[int(position) - 1]
        raise KeyError("timer not found")

    def _schedule(self, timer: VoiceTimer) -> None:
        self._tasks[timer.timer_id] = asyncio.create_task(self._wait(timer))

    async def _wait(self, timer: VoiceTimer) -> None:
        try:
            await asyncio.sleep(max(0, timer.due_at - time.time()))
            self.timers.pop(timer.timer_id, None)
            callback = self._callbacks.get(timer.client_id)
            if callback:
                try:
                    await callback(timer)
                except asyncio.CancelledError:
                    self.pending.setdefault(timer.client_id, []).append(timer)
                    self._save()
                    raise
                except Exception:
                    self.pending.setdefault(timer.client_id, []).append(timer)
            else:
                self.pending.setdefault(timer.client_id, []).append(timer)
            self._save()
        except asyncio.CancelledError:
            raise
        finally:
            self._tasks.pop(timer.timer_id, None)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timers": [asdict(timer) for timer in self.timers.values()],
            "pending": {
                key: [asdict(timer) for timer in items] for key, items in self.pending.items()
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.replace(self.path)


TIMER_TOOLS = [
    {
        "type": "function",
        "name": f"voice_timer_{operation}",
        "description": description,
        "parameters": schema,
    }
    for operation, description, schema in (
        (
            "create",
            "Create a client-scoped timer.",
            {
                "type": "object",
                "properties": {
                    "duration_seconds": {"type": "integer", "minimum": 1},
                    "label": {"type": "string"},
                },
                "required": ["duration_seconds"],
            },
        ),
        (
            "list",
            "List this client's timers in follow-up order.",
            {"type": "object", "properties": {}},
        ),
        (
            "cancel",
            "Cancel a timer by ID or list position.",
            {
                "type": "object",
                "properties": {
                    "timer_id": {"type": "string"},
                    "position": {"type": "integer", "minimum": 1},
                },
            },
        ),
        (
            "pause",
            "Pause a timer by ID or list position.",
            {
                "type": "object",
                "properties": {
                    "timer_id": {"type": "string"},
                    "position": {"type": "integer", "minimum": 1},
                },
            },
        ),
        (
            "resume",
            "Resume a timer by ID or list position.",
            {
                "type": "object",
                "properties": {
                    "timer_id": {"type": "string"},
                    "position": {"type": "integer", "minimum": 1},
                },
            },
        ),
    )
]
TIMER_TOOLS.append(
    {
        "type": "function",
        "name": "voice_alarm_create",
        "description": "Create a client-scoped alarm at an RFC 3339 time with timezone.",
        "parameters": {
            "type": "object",
            "properties": {"fire_at": {"type": "string"}, "label": {"type": "string"}},
            "required": ["fire_at"],
        },
    }
)
