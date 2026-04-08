from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import TradeEvent
from app.risk.account_loss_guard import AccountLossGuard
from app.risk.risk_engine import RiskEngine


def _order_req() -> OrderRequest:
    return OrderRequest(
        symbol="SBIN",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )


def _settings(
    *,
    enabled: bool = True,
    max_daily_loss: float | None = 500.0,
    fail_open_on_missing_pnl: bool = False,
):
    return SimpleNamespace(
        risk_enable_daily_loss=enabled,
        risk_max_daily_loss=max_daily_loss,
        risk_fail_open_on_missing_pnl=fail_open_on_missing_pnl,
    )


def test_risk_engine_fails_closed_when_pnl_engine_missing(monkeypatch):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(enabled=True, max_daily_loss=500.0),
    )
    engine = RiskEngine(pnl_engine=None)

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-1",
        order=_order_req(),
    )

    assert allowed is False
    assert reason == "pnl_engine_unavailable"


def test_risk_engine_can_fail_open_when_pnl_engine_missing(monkeypatch):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(
            enabled=True,
            max_daily_loss=500.0,
            fail_open_on_missing_pnl=True,
        ),
    )
    engine = RiskEngine(pnl_engine=None)

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-1",
        order=_order_req(),
    )

    assert allowed is True
    assert reason == "pnl_engine_unavailable_fail_open"


def test_risk_engine_fails_closed_when_snapshot_missing(monkeypatch):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(enabled=True, max_daily_loss=500.0),
    )
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )
    engine = RiskEngine(pnl_engine=PnLEngine())

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-1",
        order=_order_req(),
    )

    assert allowed is False
    assert reason == "pnl_snapshot_unavailable"


def test_risk_engine_blocks_when_account_daily_loss_reached(monkeypatch):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(enabled=True, max_daily_loss=500.0),
    )
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )
    pnl_engine = PnLEngine()
    now = datetime.now(timezone.utc)
    pnl_engine.on_trade(
        TradeEvent(
            tenant_id="tenant-1",
            broker_account_id="acc-1",
            strategy_id="strat-a",
            symbol="SBIN",
            qty=1,
            price=300.0,
            trade_time=now,
            fees=0.0,
        )
    )
    pnl_engine.on_trade(
        TradeEvent(
            tenant_id="tenant-1",
            broker_account_id="acc-1",
            strategy_id="strat-b",
            symbol="SBIN",
            qty=1,
            price=250.0,
            trade_time=now,
            fees=0.0,
        )
    )
    engine = RiskEngine(pnl_engine=pnl_engine)

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-check",
        order=_order_req(),
    )

    assert allowed is False
    assert reason.startswith("max_daily_loss_reached_current_-550.00")


def test_risk_engine_blocks_from_account_loss_guard_without_pnl_snapshot(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(enabled=True, max_daily_loss=500.0),
    )
    guard = AccountLossGuard()
    guard.update_snapshot(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        realized_pnl=-550.0,
        daily_realized_pnl=-550.0,
        max_daily_loss=500.0,
        kill_switch_activated=False,
        source="test",
    )
    engine = RiskEngine(
        pnl_engine=None,
        account_loss_guard_store=guard,
    )

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-check",
        order=_order_req(),
    )

    assert allowed is False
    assert reason.startswith("max_daily_loss_reached_current_-550.00")


def test_risk_engine_allows_when_account_within_daily_loss_limit(monkeypatch):
    monkeypatch.setattr(
        "app.risk.risk_engine.get_settings",
        lambda: _settings(enabled=True, max_daily_loss=500.0),
    )
    monkeypatch.setattr(
        "app.pnl.pnl_engine.get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )
    pnl_engine = PnLEngine()
    pnl_engine.on_trade(
        TradeEvent(
            tenant_id="tenant-1",
            broker_account_id="acc-1",
            strategy_id="strat-a",
            symbol="SBIN",
            qty=1,
            price=450.0,
            trade_time=datetime.now(timezone.utc),
            fees=0.0,
        )
    )
    engine = RiskEngine(pnl_engine=pnl_engine)

    allowed, reason = engine.check_order_allowed(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="strategy-check",
        order=_order_req(),
    )

    assert allowed is True
    assert reason == "risk_ok"
