"""Shared asyncio bridge loop for strategy order submission."""

from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import threading
from typing import Any, Coroutine, Optional, TypeVar

T = TypeVar("T")


class StrategyBridgeTimeoutError(TimeoutError):
    """Raised when a bridge submission exceeds its timeout budget."""


class SharedBridgeLoop:
    def __init__(self, *, max_inflight: int) -> None:
        self._max_inflight = max(1, int(max_inflight))
        self._inflight_gate = threading.BoundedSemaphore(value=self._max_inflight)
        self._thread_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()

    @property
    def max_inflight(self) -> int:
        return self._max_inflight

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()
        pending = asyncio.all_tasks(loop)
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        must_wait = False
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                if self._loop is not None:
                    return self._loop
                must_wait = True
            else:
                self._ready.clear()
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="strategy-bridge-shared-loop",
                    daemon=True,
                )
                self._thread.start()
                must_wait = True
        if must_wait and not self._ready.wait(timeout=5.0):
            raise RuntimeError("SharedBridgeLoop failed to start within timeout")
        loop = self._loop
        if loop is None:
            raise RuntimeError("SharedBridgeLoop started without event loop")
        return loop

    def submit(self, coro: Coroutine[Any, Any, T], *, timeout_s: float) -> T:
        loop = self._ensure_started()
        self._inflight_gate.acquire()
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=max(0.1, float(timeout_s)))
        except FutureTimeoutError as exc:
            if future is not None:
                future.cancel()
            raise StrategyBridgeTimeoutError(
                f"strategy bridge submission timed out after {float(timeout_s):.3f}s"
            ) from exc
        finally:
            self._inflight_gate.release()

    def shutdown(self) -> None:
        with self._thread_lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)


_GLOBAL_BRIDGE_LOOP: Optional[SharedBridgeLoop] = None
_GLOBAL_LOCK = threading.Lock()


def get_shared_bridge_loop(*, max_inflight: int) -> SharedBridgeLoop:
    global _GLOBAL_BRIDGE_LOOP
    with _GLOBAL_LOCK:
        if _GLOBAL_BRIDGE_LOOP is None:
            _GLOBAL_BRIDGE_LOOP = SharedBridgeLoop(max_inflight=max_inflight)
            atexit.register(_GLOBAL_BRIDGE_LOOP.shutdown)
        return _GLOBAL_BRIDGE_LOOP


def reset_shared_bridge_loop_for_tests() -> None:
    global _GLOBAL_BRIDGE_LOOP
    with _GLOBAL_LOCK:
        loop = _GLOBAL_BRIDGE_LOOP
        _GLOBAL_BRIDGE_LOOP = None
    if loop is not None:
        loop.shutdown()


__all__ = [
    "SharedBridgeLoop",
    "StrategyBridgeTimeoutError",
    "get_shared_bridge_loop",
    "reset_shared_bridge_loop_for_tests",
]
