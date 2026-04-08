import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

from app.orders.bridge_loop import SharedBridgeLoop


async def _slow_ok(delay_s: float) -> str:
    await asyncio.sleep(delay_s)
    return "ok"


def test_shared_bridge_loop_enforces_max_inflight_bound():
    bridge = SharedBridgeLoop(max_inflight=2)

    def _submit(_i: int) -> str:
        return bridge.submit(_slow_ok(0.2), timeout_s=2.0)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_submit, range(4)))
    elapsed = time.perf_counter() - started
    bridge.shutdown()

    assert results == ["ok", "ok", "ok", "ok"]
    assert elapsed >= 0.35
