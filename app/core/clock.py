"""Time abstractions for deterministic orchestration and tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from threading import Lock
from typing import Protocol


class IClock(Protocol):
    def now_utc(self) -> datetime:
        ...

    def now_local(self, tz: tzinfo) -> datetime:
        ...


class SystemClock:
    """Default wall-clock implementation used in production runtime."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_local(self, tz: tzinfo) -> datetime:
        return self.now_utc().astimezone(tz)


class SimulatedClock:
    """Deterministic clock for tests and backtests."""

    def __init__(self, initial_utc: datetime) -> None:
        if initial_utc.tzinfo is None:
            initial_utc = initial_utc.replace(tzinfo=timezone.utc)
        self._current_utc = initial_utc.astimezone(timezone.utc)
        self._lock = Lock()

    def now_utc(self) -> datetime:
        with self._lock:
            return self._current_utc

    def now_local(self, tz: tzinfo) -> datetime:
        return self.now_utc().astimezone(tz)

    def advance(self, delta: timedelta) -> datetime:
        with self._lock:
            self._current_utc = self._current_utc + delta
            return self._current_utc
