"""Tests for ExternalFillReconciler.

Pins the contract that:
  - Phoenix-placed orders (in order_submission_outbox) are skipped.
  - Already-claimed orders (in trade_processed_markers) are skipped.
  - Truly external fills are booked exactly once (claim_processed,
    pnl_engine.on_trade, insert_trade_postgres).
  - Fail-closed on outbox lookup failure: when DB is unreachable we treat
    every order as Phoenix-placed rather than risk double-booking.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import pytest

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.external_fill_reconciler import (
    EXTERNAL_FILL_STRATEGY_ID,
    ExternalFillReconciler,
)
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


# ---- helpers ----------------------------------------------------------

class _CapturingPnLEngine:
    """Minimal stand-in: records every TradeEvent fed to on_trade."""
    def __init__(self) -> None:
        self.events = []

    def on_trade(self, event):
        self.events.append(event)


def _mk_order(
    *,
    order_id: str,
    side: str,
    qty: int,
    price: float,
    status: str = "COMPLETE",
    symbol: str = "NATURALGAS22MAY26255CE",
    updated_at: str = "2026-05-07T21:47:59+05:30",
):
    """Mimic the OrderStatus dataclass shape but as a SimpleNamespace so we
    don't need to instantiate the real broker class."""
    return SimpleNamespace(
        order_id=order_id,
        symbol=symbol,
        side=side,
        status=status,
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=qty,
        filled_quantity=qty,
        average_price=price,
        price=price,
        updated_at=updated_at,
    )


def _patch_outbox(monkeypatch, phoenix_placed_ids):
    """Stub the outbox lookup so we don't need a real Postgres connection.
    Returns the IDs the test says are 'Phoenix-placed' (i.e. should be
    skipped by the reconciler)."""
    def _fake(*, broker_account_id, broker_order_ids, dsn=None):
        return set(phoenix_placed_ids) & set(broker_order_ids)

    monkeypatch.setattr(
        "app.orders.external_fill_reconciler._phoenix_placed_order_ids",
        _fake,
    )


def _patch_pg_insert(monkeypatch, captured: List):
    def _fake(record, *, dsn=None):
        captured.append(dict(record))
        return True
    monkeypatch.setattr(
        "app.orders.external_fill_reconciler.insert_trade_postgres",
        _fake,
    )


# ---- tests ------------------------------------------------------------

def test_external_fill_is_ingested_once(monkeypatch):
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)

    _patch_outbox(monkeypatch, phoenix_placed_ids=[])  # nothing in the outbox -> all external
    pg_inserts: List = []
    _patch_pg_insert(monkeypatch, pg_inserts)

    orders = [
        _mk_order(order_id="EXT-1", side="SELL", qty=1250, price=17.30),
    ]
    n = reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    )
    assert n == 1
    assert len(pnl.events) == 1
    ev = pnl.events[0]
    assert ev.symbol == "NATURALGAS22MAY26255CE"
    assert ev.qty == -1250  # SELL -> negative signed qty
    assert ev.price == pytest.approx(17.30)
    assert ev.strategy_id == EXTERNAL_FILL_STRATEGY_ID
    assert len(pg_inserts) == 1
    assert pg_inserts[0]["execution_mode"] == "EXTERNAL"


def test_external_fill_is_idempotent_across_calls(monkeypatch):
    """Running the reconciler twice on the same broker order list must not
    double-book — the processed_store guards via claim_processed."""
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_outbox(monkeypatch, phoenix_placed_ids=[])
    _patch_pg_insert(monkeypatch, [])

    orders = [_mk_order(order_id="EXT-1", side="SELL", qty=1250, price=17.30)]
    reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    )
    reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    )
    assert len(pnl.events) == 1


def test_phoenix_placed_orders_are_skipped(monkeypatch):
    """When the outbox shows Phoenix placed the order, the reconciler must
    skip it — emit_trade is the canonical path for those."""
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_outbox(monkeypatch, phoenix_placed_ids=["PHX-1"])
    _patch_pg_insert(monkeypatch, [])

    orders = [
        _mk_order(order_id="PHX-1", side="SELL", qty=1250, price=10.70),
        _mk_order(order_id="EXT-2", side="SELL", qty=1250, price=17.30),
    ]
    n = reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    )
    assert n == 1
    assert pnl.events[0].price == pytest.approx(17.30)


def test_non_terminal_orders_are_ignored(monkeypatch):
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_outbox(monkeypatch, phoenix_placed_ids=[])
    _patch_pg_insert(monkeypatch, [])

    orders = [
        _mk_order(order_id="OPEN-1", side="BUY", qty=1250, price=14.0, status="OPEN"),
        _mk_order(order_id="REJ-1", side="BUY", qty=1250, price=14.0, status="REJECTED"),
    ]
    assert reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    ) == 0


def test_zero_filled_qty_is_ignored(monkeypatch):
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_outbox(monkeypatch, phoenix_placed_ids=[])
    _patch_pg_insert(monkeypatch, [])

    o = _mk_order(order_id="EXT-X", side="BUY", qty=1250, price=14.0)
    o.filled_quantity = 0
    assert reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=[o],
    ) == 0


def test_db_outage_treats_all_as_phoenix_placed(monkeypatch):
    """When the outbox lookup fails (e.g. Postgres is down), the reconciler
    must NOT treat unknown orders as external — that would double-book once
    Phoenix's emit_trade catches up. Default to fail-closed."""
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_pg_insert(monkeypatch, [])

    # Simulate DB failure inside the outbox helper by forcing it to behave
    # the way the real helper does on exception (returns all IDs as
    # 'Phoenix-placed' which means 'skip').
    def _fail_closed(*, broker_account_id, broker_order_ids, dsn=None):
        return set(broker_order_ids)
    monkeypatch.setattr(
        "app.orders.external_fill_reconciler._phoenix_placed_order_ids",
        _fail_closed,
    )

    orders = [_mk_order(order_id="UNKNOWN-1", side="SELL", qty=1250, price=17.30)]
    assert reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    ) == 0
    assert pnl.events == []


def test_user_scenario_naturalgas_manual_exit(monkeypatch):
    """Reproduces the 2026-05-07 A1 scenario exactly:
    - 3 Phoenix-placed orders (already booked via emit_trade flow)
    - 1 external manual SELL @ 17.30 (the 4th order, placed via Angel UI)
    Reconciler should ingest only the 4th, leaving Phoenix's 3 alone."""
    pnl = _CapturingPnLEngine()
    store = InMemoryProcessedTradeStore()
    reconciler = ExternalFillReconciler(pnl_engine=pnl, processed_store=store)
    _patch_outbox(
        monkeypatch,
        phoenix_placed_ids=[
            "260507001433140",  # SELL 10.70 (Phoenix entry)
            "260507001449079",  # BUY  12.10 (Phoenix close-and-flip)
            "260507001449159",  # BUY  12.90 (Phoenix continuation)
        ],
    )
    pg_inserts: List = []
    _patch_pg_insert(monkeypatch, pg_inserts)

    orders = [
        _mk_order(order_id="260507001433140", side="SELL", qty=1250, price=10.70),
        _mk_order(order_id="260507001449079", side="BUY", qty=1250, price=12.10),
        _mk_order(order_id="260507001449159", side="BUY", qty=1250, price=12.90),
        _mk_order(order_id="ANGEL-MANUAL-1", side="SELL", qty=1250, price=17.30),
    ]
    n = reconciler.reconcile_orders(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        orders=orders,
    )
    assert n == 1
    assert len(pnl.events) == 1
    assert pnl.events[0].symbol == "NATURALGAS22MAY26255CE"
    assert pnl.events[0].qty == -1250
    assert pnl.events[0].price == pytest.approx(17.30)
    # That single ingest is exactly +1250 × 17.30 = +21,625, which
    # reconciles -17,875 (Phoenix snapshot) to +3,750 (Angel truth).
    assert len(pg_inserts) == 1
    assert pg_inserts[0]["trade_id"] == "ANGEL-MANUAL-1"
