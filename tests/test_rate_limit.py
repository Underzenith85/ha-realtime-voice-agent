import app.rate_limit
from app.rate_limit import RateLimiter


def test_sliding_window_bounds_each_key(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(app.rate_limit.time, "monotonic", lambda: now)
    limiter = RateLimiter(2, window_seconds=60)

    assert limiter.allow("one") is True
    assert limiter.allow("one") is True
    assert limiter.allow("one") is False
    assert limiter.allow("two") is True

    now = 161.0
    assert limiter.allow("one") is True
    limiter.cleanup()
    assert "two" not in limiter._events
