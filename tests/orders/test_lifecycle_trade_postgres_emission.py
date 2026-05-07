"""Regression tests for the LIVE-path Postgres trade ledger.

OrderLifecycleService._emit_trade is the canonical fill emission path in
LIVE mode (OrderRouter does not call _record_trade_and_pnl directly when
order_lifecycle is wired). Without a Postgres write here, the OCI-only
deployment leaves /me/trades permanently empty even though
pnl_snapshots.realized_pnl accumulates correctly.

These tests pin:
  - _emit_trade calls insert_trade_postgres with the same shape that
    fetch_trades_postgres + the /me/trades response model expect.
  - Failures in the Postgres path do NOT raise out of _emit_trade.
  - The record carries tenant_id / broker_account_id / trade_id /
    side / qty / price / trade_time so the read path can filter it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.order_lifecycle import OrderLifecycleService, SubmissionContext
from app.orders.order_state import OrderLifecycleState
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


def _mk_service() -> OrderLifecycleService:
    class _NoopStateStore:
        def get_balance(self, *_a, **_kw): return None
        def get_positions(self, *_a, **_kw): return []
        def set_positions(self, *_a, **_kw): return None
        def upsert_position(self, *_a, **_kw): return None
        def set_position(self, *_a, **_kw): return None

    return OrderLifecycleService(
        state_store=_NoopStateStore(),
        pnl_engine=None,
        position_ownership_store=None,
        processed_store=InMemoryProcessedTradeStore(),
    )


def _mk_ctx(*, side: str, qty: int, broker_order_id: str = "260507001433140") -> SubmissionContext:
    return SubmissionContext(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("ema20_strategy"),
        hub_order_id="hub-1",
        broker_order_id=broker_order_id,
        symbol="NATURALGAS22MAY26255CE",
        side=side,
        fallback_qty=qty,
        fallback_price=None,
        product_type="INTRADAY",
        purpose="ENTRY",
    )


def _drive_emit(svc: OrderLifecycleService, ctx: SubmissionContext, *, qty: int, price: float):
    svc._contexts[svc._order_key(ctx.broker_account_id, ctx.broker_order_id)] = ctx
    svc._emit_trade(
        ctx,
        filled_qty=qty,
        price=price,
        trade_time=datetime(2026, 5, 7, 13, 10, 54, tzinfo=timezone.utc),
        transition_source="terminal_emission_test",
        terminal_status="FILLED",
        lifecycle_state=OrderLifecycleState.FILLED,
    )


def test_emit_trade_calls_postgres_insert_with_expected_shape():
    captured: List[Dict[str, Any]] = []

    def _fake_insert(record: Dict[str, Any], *, dsn=None) -> bool:
        captured.append(dict(record))
        return True

    svc = _mk_service()
    ctx = _mk_ctx(side="SELL", qty=1250)
    with patch("app.data.trade_store_postgres.insert_trade_postgres", _fake_insert):
        _drive_emit(svc, ctx, qty=1250, price=9.75)

    assert len(captured) == 1, f"expected one Postgres insert, got {len(captured)}"
    rec = captured[0]
    assert rec["tenant_id"] == "tenant-1"
    assert rec["broker_account_id"] == "A1"
    assert rec["strategy_id"] == "ema20_strategy"
    assert rec["symbol"] == "NATURALGAS22MAY26255CE"
    assert rec["side"] == "SELL"
    assert int(rec["qty"]) == 1250
    assert float(rec["price"]) == pytest.approx(9.75)
    assert rec["trade_id"] == "260507001433140"
    assert rec["execution_mode"] == "LIVE"
    assert rec["virtual"] is False


def test_emit_trade_handles_postgres_failure_without_raising():
    """If Postgres is unreachable, _emit_trade must continue (PnL update + audit
    log still happen). The trade record is lost from the ledger but the system
    keeps trading — fail-open is the right tradeoff here because pnl_snapshots
    is the source of truth for risk decisions."""
    def _failing_insert(_record: Dict[str, Any], *, dsn=None) -> bool:
        raise RuntimeError("simulated Postgres outage")

    svc = _mk_service()
    ctx = _mk_ctx(side="BUY", qty=1250)
    with patch("app.data.trade_store_postgres.insert_trade_postgres", _failing_insert):
        # Should NOT raise.
        _drive_emit(svc, ctx, qty=1250, price=14.15)


def test_normalize_record_coerces_types():
    """Trade store must coerce string-formatted numeric/datetime fields the
    way fetch_trades_for_tenant clients expect."""
    from app.data.trade_store_postgres import _normalize_record

    raw = {
        "trade_id": "T-1",
        "broker_account_id": "A1",
        "tenant_id": "tenant-1",
        "strategy_id": "ema20_strategy",
        "hub_order_id": "hub-x",
        "symbol": "NG_CE",
        "side": "buy",  # lowercase
        "qty": "1250",  # string
        "price": "14.15",
        "trade_time": "2026-05-07T13:10:54+00:00",
        "realized_pnl": "",  # blank
        "fees": None,
        "execution_mode": "LIVE",
        "virtual": False,
    }
    out = _normalize_record(raw)
    assert out["side"] == "BUY"
    assert isinstance(out["qty"], int) and out["qty"] == 1250
    assert isinstance(out["price"], float) and out["price"] == pytest.approx(14.15)
    assert isinstance(out["trade_time"], datetime)
    assert out["trade_time"].tzinfo is not None
    assert out["realized_pnl"] is None
    assert out["fees"] is None


def test_fetch_trades_for_tenant_uses_postgres_when_bq_disabled(monkeypatch):
    """fetch_trades_for_tenant must consult Postgres before falling back to CSV
    when BigQuery is not configured."""
    monkeypatch.delenv("BQ_TRADES_TABLE", raising=False)
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    pg_rows = [
        {
            "trade_id": "T-1",
            "broker_account_id": "A1",
            "tenant_id": "tenant-1",
            "strategy_id": "ema20_strategy",
            "symbol": "NG_CE",
            "side": "BUY",
            "qty": 1250,
            "price": 14.15,
            "trade_time": "2026-05-07T13:10:54+00:00",
            "execution_mode": "LIVE",
            "virtual": False,
        }
    ]
    fetch_calls = []

    def _fake_pg(**kwargs):
        fetch_calls.append(kwargs)
        return list(pg_rows)

    csv_calls = []

    def _fake_csv(**kwargs):
        csv_calls.append(kwargs)
        return []

    with patch("app.data.trade_store_postgres.fetch_trades_postgres", _fake_pg):
        with patch("app.data.bq_persister._load_trade_rows_from_csv", _fake_csv):
            from app.data.bq_persister import fetch_trades_for_tenant
            rows = fetch_trades_for_tenant(
                tenant_id=TenantId("tenant-1"),
                broker_account_id=BrokerAccountId("A1"),
                start_time=datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc),
                end_time=datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc),
                limit=500,
            )

    assert rows == pg_rows
    assert len(fetch_calls) == 1
    assert csv_calls == [], "CSV fallback should not run when Postgres returns rows"


def test_fetch_trades_for_tenant_falls_back_to_csv_when_postgres_unreachable(monkeypatch):
    """If the Postgres helper returns None (signaling outage / connection
    failure), the legacy CSV path is consulted as a defense-in-depth."""
    monkeypatch.delenv("BQ_TRADES_TABLE", raising=False)
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    csv_rows = [{"trade_id": "csv-1", "tenant_id": "tenant-1"}]

    with patch("app.data.trade_store_postgres.fetch_trades_postgres", lambda **_: None):
        with patch("app.data.bq_persister._load_trade_rows_from_csv", lambda **_: list(csv_rows)):
            from app.data.bq_persister import fetch_trades_for_tenant
            rows = fetch_trades_for_tenant(
                tenant_id=TenantId("tenant-1"),
                broker_account_id=BrokerAccountId("A1"),
                start_time=None,
                end_time=None,
                limit=500,
            )
    assert rows == csv_rows
