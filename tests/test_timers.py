import asyncio
import json
import time

from app.timers import TIMER_TOOLS, TimerManager


async def test_timer_crud_ordering_pause_resume_and_client_isolation(tmp_path) -> None:
    manager = TimerManager(tmp_path / "timers.json")
    await manager.start()
    first = manager.create("client-a", 30, "Tea")
    second = manager.create("client-a", 60, "Laundry")
    manager.create("client-b", 10, "Private")

    listed = manager.list("client-a")
    assert [item["label"] for item in listed] == ["Tea", "Laundry"]
    assert [item["position"] for item in listed] == [1, 2]

    paused = json.loads(await manager.call("voice_timer_pause", "client-a", {"position": 2}))
    assert paused["paused"]["state"] == "paused"
    resumed = json.loads(
        await manager.call("voice_timer_resume", "client-a", {"timer_id": second.timer_id})
    )
    assert resumed["resumed"]["state"] == "active"
    cancelled = json.loads(await manager.call("voice_timer_cancel", "client-a", {"position": 1}))
    assert cancelled["cancelled"]["timer_id"] == first.timer_id
    assert [item["label"] for item in manager.list("client-b")] == ["Private"]
    await manager.close()


async def test_timer_persists_across_restart_and_delivers_after_reconnect(tmp_path) -> None:
    path = tmp_path / "timers.json"
    original = TimerManager(path)
    await original.start()
    original.create("client", 60, "Persistent")
    await original.close()

    restored = TimerManager(path)
    await restored.start()
    assert restored.list("client")[0]["label"] == "Persistent"

    completed = []
    timer = restored.create("offline", 1, "Reconnect")
    timer.due_at = time.time() - 1
    restored._tasks[timer.timer_id].cancel()
    await asyncio.gather(restored._tasks[timer.timer_id], return_exceptions=True)
    restored._schedule(timer)
    async with asyncio.timeout(1):
        while not restored.pending.get("offline"):
            await asyncio.sleep(0)
    restored.register("offline", lambda item: _record(completed, item.label))
    async with asyncio.timeout(1):
        while completed != ["Reconnect"]:
            await asyncio.sleep(0)
    await restored.close()


async def _record(items, value) -> None:
    items.append(value)


async def test_alarm_requires_timezone_and_is_exposed() -> None:
    manager = TimerManager("/tmp/not-used-timers.json")
    names = {tool["name"] for tool in TIMER_TOOLS}
    assert names == {
        "voice_timer_create",
        "voice_timer_list",
        "voice_timer_cancel",
        "voice_timer_pause",
        "voice_timer_resume",
        "voice_alarm_create",
    }
    try:
        await manager.call("voice_alarm_create", "client", {"fire_at": "2030-01-01T07:00:00"})
    except ValueError as error:
        assert "timezone" in str(error)
    else:
        raise AssertionError("timezone-less alarm was accepted")
