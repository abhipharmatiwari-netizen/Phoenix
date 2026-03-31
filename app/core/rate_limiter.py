"""
Simple per-key rate limiter used to throttle broker API calls.
Uses a sliding window and sleeps until capacity is available.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from typing import Dict, Tuple


class RateLimiter:
    # Initialize storage for limits and recent call timestamps.
    def __init__(self) -> None:
        # key -> list of (window_seconds, max_calls)
        self.limits: Dict[str, Tuple[int, int]] = {}
        # key -> deque of timestamps
        self.calls: Dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    # Define the max calls allowed for a key in a time window.
    def set_limit(self, key: str, window_seconds: int, max_calls: int) -> None:
        self.limits[key] = (window_seconds, max_calls)


    # Block until the caller is allowed to proceed for the given key.
    def acquire(self, key: str) -> None:
        """Block until the caller is allowed to proceed for the given key.

        Thread-safe:
          - Uses a lock to coordinate concurrent callers.
          - Uses time.monotonic() so system clock changes do not break the logic.
          - Releases the lock while sleeping, so other keys/threads can progress.
        """
        window_seconds, max_calls = self.limits.get(key, (0, 0))
        if max_calls <= 0 or window_seconds <= 0:
            return

        window = float(window_seconds)

        while True:
            with self._lock:
                now = time.monotonic()
                dq = self.calls[key]

                while dq and now - dq[0] > window:
                    dq.popleft()

                if len(dq) < max_calls:
                    dq.append(now)
                    return

                sleep_time = window - (now - dq[0]) + 0.001

            time.sleep(max(0.0, sleep_time))


rate_limiter = RateLimiter()
# Provide a module-level helper for monkeypatching/tests
# Proxy acquire() to the shared rate_limiter instance.
def acquire(key: str) -> None:
    return rate_limiter.acquire(key)

# Configure key endpoints based on Angel One throttling
rate_limiter.set_limit("loginByPassword", 1, 1)  # 1 req/sec
rate_limiter.set_limit("generateTokens", 1, 1)  # 1 req/sec
rate_limiter.set_limit("quote", 1, 10)  # 10 req/sec
rate_limiter.set_limit("placeOrder", 1, 9)  # 9 req/sec
rate_limiter.set_limit("logout", 1, 1)  # 1 req/sec
rate_limiter.set_limit("getRMS", 1, 2)  # 2 req/sec
# Positions / orderbook throttles (keep conservative to avoid SmartAPI blocks)
rate_limiter.set_limit("getPosition", 1, 1)        # 1 req/sec
rate_limiter.set_limit("getAllPositions", 1, 1)    # 1 req/sec
rate_limiter.set_limit("getOrderBook", 1, 2)       # 2 req/sec
rate_limiter.set_limit("getTradeBook", 1, 2)       # 2 req/sec (if you call it)
