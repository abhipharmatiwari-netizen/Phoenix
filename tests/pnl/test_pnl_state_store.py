from __future__ import annotations

from datetime import datetime, timezone

from app.pnl.pnl_engine import PnLEngine
from app.pnl.state_store import InMemoryPnLStateStore, build_pnl_state_store
from app.pnl.types import TradeEvent


def test_build_pnl_state_store_inmemory_backend(monkeypatch):
    monkeypatch.setenv("PNL_STATE_BACKEND", "memory")
    store = build_pnl_state_store()
    assert isinstance(store, InMemoryPnLStateStore)


def test_shared_state_store_keeps_snapshots_across_engine_instances(monkeypatch):
    monkeypatch.setenv("PNL_STATE_BACKEND", "memory")
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: type("S", (), {"default_time_zone": "UTC"})(),
    )
    shared_store = InMemoryPnLStateStore()
    engine_a = PnLEngine(state_store=shared_store)
    engine_b = PnLEngine(state_store=shared_store)

    engine_a.on_trade(
        TradeEvent(
            tenant_id="tenant-1",
            broker_account_id="acc-1",
            strategy_id="strat-1",
            symbol="SBIN",
            qty=1,
            price=100.0,
            trade_time=datetime.now(timezone.utc),
            fees=0.0,
        )
    )

    realized = engine_b.get_current_realized_pnl(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id=None,
    )
    assert realized == -100.0
