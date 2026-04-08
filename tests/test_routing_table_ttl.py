from __future__ import annotations

import time

from app.hub.routing_table import HubRoute, HubRoutingTable


def test_routing_table_refresh_once_per_ttl_window(monkeypatch):
    table = HubRoutingTable()
    refresh_calls = {"count": 0}

    def _fake_refresh() -> None:
        refresh_calls["count"] += 1
        with table._lock:
            table._routes_by_strategy = {
                "ema20_strategy": [
                    HubRoute(tenant_id="t1", broker_account_id="ba1")
                ]
            }
            table._last_refresh_mono = time.monotonic()

    monkeypatch.setattr(table, "refresh", _fake_refresh)
    monkeypatch.setenv("HUB_ROUTING_REFRESH_MIN_SECONDS", "30")

    routes_1 = table.get_routes_for_strategy("ema20_strategy")
    routes_2 = table.get_routes_for_strategy("ema20_strategy")
    assert refresh_calls["count"] == 1
    assert len(routes_1) == 1
    assert len(routes_2) == 1

    with table._lock:
        table._last_refresh_mono = time.monotonic() - 31.0
    routes_3 = table.get_routes_for_strategy("ema20_strategy")

    deadline = time.monotonic() + 1.0
    while refresh_calls["count"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert refresh_calls["count"] == 2
    assert len(routes_3) == 1
