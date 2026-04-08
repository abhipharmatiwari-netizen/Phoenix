import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import SimulatedClock
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent


def _build_engine(monkeypatch, *, base_time: datetime) -> tuple[PnLEngine, SimulatedClock]:
    monkeypatch.setattr("app.pnl.pnl_engine.insert_daily_pnl_snapshot", lambda record: None)
    monkeypatch.setenv("PNL_FRESHNESS_THRESHOLD_SECONDS", "300")
    clock = SimulatedClock(base_time)
    return PnLEngine(clock=clock), clock


def test_realized_pnl_read_does_not_log_stale_mark_warning(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="NIFTY",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    clock.advance(timedelta(minutes=10))

    with caplog.at_level(logging.WARNING):
        realized = engine.get_current_realized_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    assert realized == pytest.approx(-105.0)
    assert "realized PnL read" not in caplog.text


def test_total_pnl_read_still_warns_when_mark_snapshot_is_stale(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.update_unrealized_and_exposure(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        unrealized_pnl=125.0,
        gross_exposure=5000.0,
        as_of=base_time,
    )
    clock.advance(timedelta(minutes=10))

    with caplog.at_level(logging.WARNING):
        total = engine.get_current_total_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    assert total == pytest.approx(125.0)
    assert "PnL snapshot stale during total PnL read" in caplog.text
    assert "source=mark_update" in caplog.text


def test_broker_sync_refreshes_total_pnl_freshness_for_existing_strategy(monkeypatch, caplog):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="BANKNIFTY30MAR2653700CE",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    clock.advance(timedelta(minutes=10))
    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=0.0,
        account_gross_exposure=0.0,
        per_strategy_marks={},
        as_of=clock.now_utc(),
        source="broker_sync",
    )

    with caplog.at_level(logging.WARNING):
        total = engine.get_current_total_pnl(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )

    snapshot = engine.get_snapshot(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
    )
    assert total == pytest.approx(-105.0)
    assert snapshot is not None
    assert snapshot.unrealized_pnl == pytest.approx(0.0)
    assert snapshot.freshness_source == "broker_sync"
    assert "PnL snapshot stale during total PnL read" not in caplog.text


def test_account_total_uses_seed_snapshot_without_double_counting_strategy_marks(
    monkeypatch,
):
    base_time = datetime(2026, 3, 26, 9, 15, tzinfo=timezone.utc)
    engine, _clock = _build_engine(monkeypatch, base_time=base_time)
    tenant_id = TenantId("tenant-1")
    broker_account_id = BrokerAccountId("A1")
    strategy_id = StrategyId("ema20-trend")

    engine.on_trade(
        TradeEvent(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            symbol="BANKNIFTY30MAR2653700CE",
            qty=1,
            price=100.0,
            trade_time=base_time,
            fees=5.0,
        )
    )
    engine.sync_account_mark_to_market(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        account_unrealized_pnl=40.0,
        account_gross_exposure=240.0,
        per_strategy_marks={strategy_id: (40.0, 240.0)},
        as_of=base_time,
        source="broker_sync",
    )

    total = engine.get_current_total_pnl(
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
    )

    assert total == pytest.approx(-65.0)
