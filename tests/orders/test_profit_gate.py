from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.dashboard_bus import dashboard_bus
from app.orders.router import OrderRouter


@pytest.mark.asyncio
async def test_profit_gate_blocks_entry_order(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID1",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=100.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        profit_engine = MagicMock()
        profit_engine.check_order_allowed.return_value = SimpleNamespace(
            allowed=False, reason="daily profit target reached"
        )

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=False,
            enable_risk_checks=False,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=profit_engine,
            state_store=state_store,
        )

        order_req = OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert "daily profit target reached" in response.message
        profit_engine.check_order_allowed.assert_called_once()
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_profit_gate_skipped_for_exit_orders(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID2",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=100.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        profit_engine = MagicMock()
        profit_engine.check_order_allowed.return_value = SimpleNamespace(
            allowed=False, reason="daily profit target reached"
        )

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=False,
            enable_risk_checks=False,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=profit_engine,
            state_store=state_store,
        )

        order_req = OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.EXIT,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "FILLED"
        profit_engine.check_order_allowed.assert_not_called()
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)
