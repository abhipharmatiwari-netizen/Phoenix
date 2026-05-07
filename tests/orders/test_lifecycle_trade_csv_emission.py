"""Regression tests for the LIVE-path per-fill trades.csv emission.

Without this writer, the OCI-only deployment (where BigQuery is not
configured) leaves /me/trades permanently empty even though pnl_snapshots
accumulates realized_pnl correctly. The trades page on the dashboard
reads CSV via _load_trade_rows_from_csv when BQ is unavailable.

These tests pin:
  - OrderLifecycleService initializes a per-fill CSV writer.
  - _emit_trade writes a row matching the schema _load_trade_rows_from_csv
    expects (tenant_id, broker_account_id, trade_time, trade_id ...).
  - The row is filterable via the same path the /me/trades endpoint takes.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.brokers.base import OrderPurpose
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.order_lifecycle import OrderLifecycleService, SubmissionContext
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


def _mk_ctx(*, side: str, qty: int) -> SubmissionContext:
    return SubmissionContext(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("ema20_strategy"),
        hub_order_id="hub-1",
        broker_order_id="260507001433140",
        symbol="NATURALGAS22MAY26255CE",
        side=side,
        fallback_qty=qty,
        fallback_price=None,
        product_type="INTRADAY",
        purpose="ENTRY",
    )


def test_writer_is_initialized_when_TRADE_CSV_ENABLED_set(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADE_CSV_ENABLED", "true")
    monkeypatch.setenv("TRADE_CSV_PATH", str(tmp_path / "trades.csv"))
    svc = _mk_service()
    assert svc._trade_csv_writer is not None
    assert svc._trade_csv_writer._trades_enabled is True


def test_emit_trade_writes_per_fill_row_to_dated_csv(monkeypatch, tmp_path):
    """The row must land in <tmp_path>/<today>/trades.csv with the schema
    _load_trade_rows_from_csv expects (tenant_id + broker_account_id present,
    side/qty/price set)."""
    monkeypatch.setenv("TRADE_CSV_ENABLED", "true")
    monkeypatch.setenv("TRADE_CSV_PATH", str(tmp_path / "trades.csv"))
    svc = _mk_service()

    ctx = _mk_ctx(side="SELL", qty=1250)
    svc._contexts[svc._order_key(ctx.broker_account_id, ctx.broker_order_id)] = ctx

    svc._emit_trade(
        ctx,
        filled_qty=1250,
        price=9.75,
        trade_time=datetime(2026, 5, 7, 13, 10, 54, tzinfo=timezone.utc),
        transition_source="terminal_emission_test",
        terminal_status="FILLED",
        lifecycle_state=__import__(
            "app.orders.order_state", fromlist=["OrderLifecycleState"]
        ).OrderLifecycleState.FILLED,
    )

    # Find the daily-partitioned CSV.
    daily_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(daily_dirs) == 1, f"expected exactly one dated dir, got {daily_dirs}"
    csv_path = daily_dirs[0] / "trades.csv"
    assert csv_path.is_file(), f"expected {csv_path} to exist"

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == "tenant-1"
    assert row["broker_account_id"] == "A1"
    assert row["symbol"] == "NATURALGAS22MAY26255CE"
    assert row["side"] == "SELL"
    assert int(row["qty"]) == 1250
    assert float(row["price"]) == pytest.approx(9.75)
    assert row["execution_mode"] == "LIVE"
    assert row["trade_id"] == "260507001433140"


def test_load_trade_rows_picks_up_emitted_row(monkeypatch, tmp_path):
    """End-to-end: emit_trade writes -> fetch_trades_for_tenant reads it back.
    Mirrors the /me/trades endpoint's actual code path when BQ is disabled."""
    monkeypatch.setenv("TRADE_CSV_ENABLED", "true")
    monkeypatch.setenv("TRADE_CSV_PATH", str(tmp_path / "trades.csv"))
    # Ensure no BQ env so the read falls through to CSV.
    monkeypatch.delenv("BQ_TRADES_TABLE", raising=False)
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    svc = _mk_service()
    ctx = _mk_ctx(side="BUY", qty=1250)
    svc._contexts[svc._order_key(ctx.broker_account_id, ctx.broker_order_id)] = ctx
    svc._emit_trade(
        ctx,
        filled_qty=1250,
        price=14.15,
        trade_time=datetime(2026, 5, 7, 14, 30, 0, tzinfo=timezone.utc),
        transition_source="terminal_emission_test",
        terminal_status="FILLED",
        lifecycle_state=__import__(
            "app.orders.order_state", fromlist=["OrderLifecycleState"]
        ).OrderLifecycleState.FILLED,
    )

    from app.data.bq_persister import fetch_trades_for_tenant

    rows = fetch_trades_for_tenant(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        start_time=datetime(2026, 5, 7, 0, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc),
        limit=500,
    )
    # Other rows from the legacy schema may live in this base_dir but lack
    # tenant_id, so the reader filters them out — only our row remains.
    matching = [r for r in rows if r.get("trade_id") == "260507001433140"]
    assert len(matching) == 1, f"expected our row to be readable; rows={rows}"
    assert matching[0]["broker_account_id"] == "A1"
    assert matching[0]["side"] == "BUY"
    assert int(matching[0]["qty"]) == 1250


def test_writer_disabled_when_TRADE_CSV_ENABLED_unset(monkeypatch, tmp_path):
    """Defensive: if TRADE_CSV_ENABLED is not on, write_trade is a no-op
    (writer is initialized but its enabled flag is False)."""
    monkeypatch.delenv("TRADE_CSV_ENABLED", raising=False)
    monkeypatch.setenv("TRADE_CSV_PATH", str(tmp_path / "trades.csv"))
    svc = _mk_service()
    assert svc._trade_csv_writer is not None
    assert svc._trade_csv_writer._trades_enabled is False

    ctx = _mk_ctx(side="SELL", qty=1250)
    svc._contexts[svc._order_key(ctx.broker_account_id, ctx.broker_order_id)] = ctx
    svc._emit_trade(
        ctx,
        filled_qty=1250,
        price=9.75,
        trade_time=datetime(2026, 5, 7, 13, 10, 54, tzinfo=timezone.utc),
        transition_source="terminal_emission_test",
        terminal_status="FILLED",
        lifecycle_state=__import__(
            "app.orders.order_state", fromlist=["OrderLifecycleState"]
        ).OrderLifecycleState.FILLED,
    )

    daily_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    if daily_dirs:
        csv_path = daily_dirs[0] / "trades.csv"
        assert not csv_path.exists(), "writer should not have created file when disabled"
