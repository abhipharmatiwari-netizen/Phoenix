from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import random
import time


@dataclass
class BackoffState:
    base_seconds: float = 5.0
    max_seconds: float = 300.0
    jitter_ratio: float = 0.2
    failures_count: int = 0
    next_allowed_ts: float = 0.0
    last_reason: Optional[str] = None

    def can_attempt(self, now: Optional[float] = None) -> bool:
        ts = now if now is not None else time.monotonic()
        return ts >= self.next_allowed_ts

    def remaining_delay(self, now: Optional[float] = None) -> float:
        ts = now if now is not None else time.monotonic()
        return max(0.0, self.next_allowed_ts - ts)

    def register_blocked(
        self,
        reason: str,
        *,
        retry_after_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> float:
        ts = now if now is not None else time.monotonic()
        self.failures_count += 1
        self.last_reason = reason
        if retry_after_seconds is not None and retry_after_seconds > 0:
            delay = float(retry_after_seconds)
        else:
            delay = min(self.max_seconds, self.base_seconds * (2 ** (self.failures_count - 1)))
            if self.jitter_ratio:
                delay *= 1.0 + random.uniform(0.0, self.jitter_ratio)
        self.next_allowed_ts = ts + delay
        return delay

    def register_ok(self) -> None:
        self.failures_count = 0
        self.next_allowed_ts = 0.0
        self.last_reason = None


__all__ = ["BackoffState"]
