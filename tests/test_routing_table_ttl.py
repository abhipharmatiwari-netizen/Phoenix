from __future__ import annotations

import time

import pytest

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


def _fake_accounts_empty():
    return []


def _fake_accounts_one(monkeypatch):
    from app.hub.routing_table import HubRoute
    from types import SimpleNamespace

    account = SimpleNamespace(
        broker_account_id="BA1",
        tenant_id="T1",
    )
    return [account]


def test_live_refresh_raises_when_routes_empty(monkeypatch):
    """TRADE_MODE=LIVE must hard-fail refresh() when routing table resolves to zero routes."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("HUB_ROUTES_JSON", raising=False)

    import app.hub.routing_table as rt_module
    monkeypatch.setattr(rt_module, "get_active_broker_accounts", _fake_accounts_empty)

    table = HubRoutingTable()
    with pytest.raises(ValueError, match="TRADE_MODE=LIVE requires at least one valid hub route"):
        table.refresh()


def test_paper_refresh_does_not_raise_when_routes_empty(monkeypatch):
    """Non-LIVE mode must NOT raise when routes are empty — silent empty table is acceptable."""
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("HUB_ROUTES_JSON", raising=False)

    import app.hub.routing_table as rt_module
    monkeypatch.setattr(rt_module, "get_active_broker_accounts", _fake_accounts_empty)

    table = HubRoutingTable()
    table.refresh()  # must not raise

    with table._lock:
        assert table._routes_by_strategy == {}


def test_live_refresh_succeeds_when_env_routes_provided(monkeypatch):
    """TRADE_MODE=LIVE must succeed when HUB_ROUTES_JSON provides valid routes."""
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv(
        "HUB_ROUTES_JSON",
        '{"ema20_strategy":[{"tenant_id":"T1","broker_account_id":"BA1"}]}',
    )

    import app.hub.routing_table as rt_module
    monkeypatch.setattr(rt_module, "get_active_broker_accounts", _fake_accounts_empty)

    table = HubRoutingTable()
    table.refresh()  # must not raise — env fallback supplies the route

    with table._lock:
        assert len(table._routes_by_strategy) == 1
