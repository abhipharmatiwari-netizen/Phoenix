"""
Per-IP and per-admin-key rate limiting middleware for admin/control-tower APIs.

Uses a simple sliding window counter stored in memory.
Configurable via environment variables:
  RATE_LIMIT_REQUESTS_PER_MINUTE (default: 60)
  RATE_LIMIT_BURST (default: 10)
"""

from __future__ import annotations

import os
import time
import threading
from collections import defaultdict
from typing import Optional

from fastapi import Request, HTTPException, status


_REQUESTS_PER_MINUTE = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "60"))
_BURST = int(os.getenv("RATE_LIMIT_BURST", "10"))
_WINDOW_SECONDS = 60.0
_MAX_REQUEST_SIZE_BYTES = int(os.getenv("RATE_LIMIT_MAX_REQUEST_BYTES", str(1024 * 1024)))  # 1MB

_lock = threading.Lock()
_buckets: dict[str, list[float]] = defaultdict(list)
_last_cleanup = 0.0
_CLEANUP_INTERVAL = 300.0  # Clean up stale entries every 5 min


def _client_key(request: Request) -> str:
    """Derive rate-limit key from admin key or client IP."""
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key:
        return f"key:{admin_key[:8]}"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client = request.client
    if client:
        return f"ip:{client.host}"
    return "ip:unknown"


def _cleanup_stale_buckets(now: float) -> None:
    global _last_cleanup
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - _WINDOW_SECONDS * 2
    stale_keys = [k for k, timestamps in _buckets.items() if not timestamps or timestamps[-1] < cutoff]
    for k in stale_keys:
        del _buckets[k]


def check_rate_limit(request: Request) -> None:
    """Check rate limit for the current request. Raises 429 if exceeded."""
    now = time.monotonic()
    key = _client_key(request)

    with _lock:
        _cleanup_stale_buckets(now)
        timestamps = _buckets[key]
        cutoff = now - _WINDOW_SECONDS
        # Remove expired entries
        while timestamps and timestamps[0] < cutoff:
            timestamps.pop(0)

        if len(timestamps) >= _REQUESTS_PER_MINUTE:
            retry_after = int(_WINDOW_SECONDS - (now - timestamps[0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

        # Burst check: no more than _BURST requests in 1 second
        one_sec_ago = now - 1.0
        recent = sum(1 for t in timestamps if t >= one_sec_ago)
        if recent >= _BURST:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Burst rate limit exceeded. Slow down.",
                headers={"Retry-After": "1"},
            )

        timestamps.append(now)


async def check_request_size(request: Request) -> None:
    """Reject requests exceeding max body size."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_REQUEST_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Request body too large. Max {_MAX_REQUEST_SIZE_BYTES} bytes.",
        )


__all__ = ["check_rate_limit", "check_request_size"]
