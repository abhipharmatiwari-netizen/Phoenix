import time
import importlib

import pytest


def test_rate_limiter_no_limit_is_noop(monkeypatch):
    mod = importlib.import_module("app.core.rate_limiter")
    RateLimiter = mod.RateLimiter

    rl = RateLimiter()
    now_calls = dict(rl.calls)
    rl.acquire("unknown-key")
    assert rl.calls == now_calls, "acquire() should not mutate calls if limit not set"


def test_rate_limiter_under_limit(monkeypatch):
    mod = importlib.import_module("app.core.rate_limiter")
    RateLimiter = mod.RateLimiter

    fake_time = [1000.0]

    def fake_time_fn():
        return fake_time[0]

    def fake_sleep(secs):
        # ensure sleep is never called when under limit
        raise AssertionError(f"sleep should not be called when under limit, got {secs}")

    monkeypatch.setattr(mod.time, "time", fake_time_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    rl = RateLimiter()
    rl.set_limit("k", window_seconds=60, max_calls=5)
    for _ in range(3):
        rl.acquire("k")


def test_rate_limiter_hits_limit_and_sleeps(monkeypatch):
    mod = importlib.import_module("app.core.rate_limiter")
    RateLimiter = mod.RateLimiter

    times = [1000.0, 1001.0, 1002.0, 1003.0, 1065.0]
    idx = {"i": 0}

    def fake_time_fn():
        i = idx["i"]
        if i < len(times):
            t = times[i]
            idx["i"] += 1
            return t
        return times[-1]

    slept = {"duration": None}

    def fake_sleep(secs):
        slept["duration"] = secs

    monkeypatch.setattr(mod.time, "time", fake_time_fn)
    monkeypatch.setattr(mod.time, "sleep", fake_sleep)

    rl = RateLimiter()
    rl.set_limit("k", window_seconds=60, max_calls=3)

    rl.acquire("k")
    rl.acquire("k")
    rl.acquire("k")
    rl.acquire("k")

    assert slept["duration"] is not None, "RateLimiter should sleep once limit is exceeded"
    assert slept["duration"] >= 0
