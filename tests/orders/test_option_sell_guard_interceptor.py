from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.base import (
    Balance,
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.dashboard_bus import dashboard_bus
from app.orders.router import OrderRouter
from app.risk.kill_switch import KillSwitchManager
from app.strategies.identifiers import OI_ML_CE_SELLER_ID


IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 5, 19, 10, 0, tzinfo=IST)


def _quote() -> dict:
    return {
        "snapshot_ts": NOW,
        "source_ts": NOW - timedelta(seconds=15),
        "underlying": "NIFTY",
        "expiry": date(2026, 5, 21),
        "strike": 25200,
        "option_type": "CE",
        "trading_symbol": "NIFTY21MAY2625200CE",
        "exchange": "NFO",
        "provider": "angel",
        "symbol_token": "12345",
        "oi": 120000,
        "volume": 1500,
        "iv": "11.25",
        "bid": "42.5",
        "ask": "43.0",
        "ltp": "42.8",
        "underlying_ltp": "25140",
        "vix": "16.5",
    }


def _strategy_context(**overrides) -> dict:
    base = {
        "decision_ts": NOW,
        "structure": "BEAR_CALL_SPREAD",
        "quote": _quote(),
        "ml_score": 0.64,
        "predicted_mae_premium": 40.0,
        "premium_received": 50.0,
        "max_loss_rupees": 4200.0,
        "vix": 16.5,
        "pnl_fresh": True,
        "pnl_age_seconds": 5,
        "max_pnl_age_seconds": 120,
        "data_fresh": True,
        "data_age_seconds": 5,
        "max_data_age_seconds": 120,
        "current_open_risk_rupees": 0.0,
        "max_open_risk_rupees": 10000.0,
        "daily_loss_rupees": 0.0,
        "daily_loss_limit_rupees": 8000.0,
        "weekly_loss_rupees": 0.0,
        "weekly_loss_limit_rupees": 16000.0,
        "max_spread_loss_limit_rupees": 5000.0,
    }
    base.update(overrides)
    return base


def _order_request(*, purpose=OrderPurpose.ENTRY, strategy_context=None) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY21MAY2625200CE",
        symbol_token="12345",
        quantity=1,
        side=OrderSide.SELL if purpose == OrderPurpose.ENTRY else OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=purpose,
        exchange="NFO",
        strategy_context=strategy_context,
    )


def _router(monkeypatch, *, kill_switch_manager: KillSwitchManager | None = None):
    runner = MagicMock()
    runner.is_running = True
    runner.place_order = AsyncMock(
        return_value=OrderResponse(
            broker_order_id="OID-OPTION-GUARD",
            status="FILLED",
            message="ok",
            filled_quantity=1,
            average_price=42.8,
        )
    )
    hub = MagicMock()
    hub.get_runner.return_value = runner

    state_store = MagicMock()
    state_store.get_balance.return_value = Balance(
        available=1_000_000.0,
        utilized=0.0,
        total=1_000_000.0,
    )
    state_store.get_positions.return_value = []
    state_store.set_last_order_response.return_value = None

    settings = SimpleNamespace(
        trade_mode="LIVE",
        default_time_zone="UTC",
        enable_profit_checks=False,
        enable_capital_checks=False,
        capital_auto_resize_enabled=False,
        enable_risk_checks=False,
        order_router_enforce_global_kill_switch=True,
        position_ownership_enabled=False,
        order_router_enforce_idempotency=False,
    )
    monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.orders.router.get_runtime_settings",
        lambda **_kwargs: settings,
    )

    manager = kill_switch_manager or KillSwitchManager(audit_fn=lambda **_: None)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: SimpleNamespace(kill_switch_manager=manager),
    )
    return (
        OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        ),
        runner,
        manager,
    )


@pytest.mark.asyncio
async def test_oi_ml_entry_missing_strategy_context_rejects_before_broker(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {"NIFTY_OPT": {"symbol": "NIFTY21MAY2625200CE", "token": "12345", "lot_size": 1}}
        )
        router, runner, _manager = _router(monkeypatch)

        _hub_order_id, response = await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
            order_req=_order_request(strategy_context=None),
        )

        assert response.status == "REJECTED"
        assert "missing_strategy_context" in response.message
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_oi_ml_valid_spread_context_routes_to_broker(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {"NIFTY_OPT": {"symbol": "NIFTY21MAY2625200CE", "token": "12345", "lot_size": 1}}
        )
        router, runner, _manager = _router(monkeypatch)

        _hub_order_id, response = await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
            order_req=_order_request(strategy_context=_strategy_context()),
        )

        assert response.status == "FILLED"
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_oi_ml_naked_entry_rejected_by_router_guard(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {"NIFTY_OPT": {"symbol": "NIFTY21MAY2625200CE", "token": "12345", "lot_size": 1}}
        )
        router, runner, _manager = _router(monkeypatch)

        _hub_order_id, response = await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
            order_req=_order_request(
                strategy_context=_strategy_context(structure="NAKED_SHORT_CE")
            ),
        )

        assert response.status == "REJECTED"
        assert "spread_only_v1_required" in response.message
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_oi_ml_daily_loss_breach_trips_soft_strategy_kill_and_allows_exit(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        dashboard_bus.set_instrument_meta(
            {"NIFTY_OPT": {"symbol": "NIFTY21MAY2625200CE", "token": "12345", "lot_size": 1}}
        )
        manager = KillSwitchManager(audit_fn=lambda **_: None)
        router, runner, _manager = _router(monkeypatch, kill_switch_manager=manager)

        _hub_order_id, entry_response = await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
            order_req=_order_request(
                strategy_context=_strategy_context(daily_loss_rupees=8000.0)
            ),
        )

        assert entry_response.status == "REJECTED"
        assert "daily_loss_limit_breached" in entry_response.message
        tripped, block_exits = manager.is_tripped_for_scope_with_block_exits(
            tenant_id="tenant-1",
            account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
        )
        assert tripped is True
        assert block_exits is False

        _hub_order_id, exit_response = await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id=OI_ML_CE_SELLER_ID,
            order_req=_order_request(purpose=OrderPurpose.EXIT, strategy_context=None),
        )

        assert exit_response.status == "FILLED"
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)
