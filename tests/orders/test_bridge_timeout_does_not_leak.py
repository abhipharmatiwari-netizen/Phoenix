import asyncio
import threading
import time

import pytest

from app.orders.bridge_loop import SharedBridgeLoop, StrategyBridgeTimeoutError


async def _sleep_then_ok(delay_s: float) -> str:
    await asyncio.sleep(delay_s)
    return "ok"


def _bridge_thread_count() -> int:
    return len(
        [
            t
            for t in threading.enumerate()
            if t.name == "strategy-bridge-shared-loop"
        ]
    )


def test_shared_bridge_timeouts_do_not_leak_threads():
    baseline = _bridge_thread_count()
    bridge = SharedBridgeLoop(max_inflight=4)

    for _ in range(8):
        with pytest.raises(StrategyBridgeTimeoutError):
            bridge.submit(_sleep_then_ok(0.2), timeout_s=0.01)

    assert bridge.submit(_sleep_then_ok(0.01), timeout_s=1.0) == "ok"
    assert _bridge_thread_count() <= baseline + 1

    bridge.shutdown()
    time.sleep(0.05)
    assert _bridge_thread_count() <= baseline
