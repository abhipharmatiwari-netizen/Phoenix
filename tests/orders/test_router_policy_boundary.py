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
from app.orders.interceptors import OrderInterceptor
from app.orders.router import OrderRouter


@pytest.mark.asyncio
async def test_router_evaluates_policy_engines_once_for_entry(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-10",
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

        capital_engine = MagicMock()
        capital_engine.can_afford_order.return_value = (True, "capital_ok")
        capital_engine.suggest_order_quantity.return_value = (1, "ok")

        risk_engine = MagicMock()
        risk_engine.check_order_allowed.return_value = (True, "risk_ok")

        profit_engine = MagicMock()
        profit_engine.check_order_allowed.return_value = SimpleNamespace(
            allowed=True, reason="profit_ok"
        )

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=capital_engine,
            risk_engine=risk_engine,
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
            purpose=OrderPurpose.ENTRY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "FILLED"
        assert capital_engine.can_afford_order.call_count == 1
        assert risk_engine.check_order_allowed.call_count == 1
        assert profit_engine.check_order_allowed.call_count == 1
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_prefers_explicit_capital_evaluate_order(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-10A",
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

        class _CapitalEngine:
            def __init__(self):
                self.evaluate_calls = 0
                self.afford_calls = 0

            def evaluate_order(self, **_kwargs):
                self.evaluate_calls += 1
                return SimpleNamespace(allowed=True, reason="capital_ok")

            def can_afford_order(self, **_kwargs):
                self.afford_calls += 1
                return True, "capital_ok"

        capital_engine = _CapitalEngine()

        risk_engine = MagicMock()
        risk_engine.check_order_allowed.return_value = (True, "risk_ok")

        profit_engine = MagicMock()
        profit_engine.check_order_allowed.return_value = SimpleNamespace(
            allowed=True, reason="profit_ok"
        )

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=capital_engine,
            risk_engine=risk_engine,
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
            purpose=OrderPurpose.ENTRY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "FILLED"
        assert capital_engine.evaluate_calls == 1
        assert capital_engine.afford_calls == 0
        assert risk_engine.check_order_allowed.call_count == 1
        assert profit_engine.check_order_allowed.call_count == 1
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_skips_policy_engines_for_exit_order(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-11",
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

        capital_engine = MagicMock()
        capital_engine.can_afford_order.return_value = (True, "capital_ok")
        capital_engine.suggest_order_quantity.return_value = (1, "ok")

        risk_engine = MagicMock()
        risk_engine.check_order_allowed.return_value = (True, "risk_ok")

        profit_engine = MagicMock()
        profit_engine.check_order_allowed.return_value = SimpleNamespace(
            allowed=True, reason="profit_ok"
        )

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=capital_engine,
            risk_engine=risk_engine,
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
        assert capital_engine.can_afford_order.call_count == 0
        assert risk_engine.check_order_allowed.call_count == 0
        assert profit_engine.check_order_allowed.call_count == 0
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_supports_pluggable_interceptor_block(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-12",
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

        settings = SimpleNamespace(
            enable_profit_checks=True,
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )

        class _BlockAllInterceptor(OrderInterceptor):
            def evaluate(self, ctx):
                _ = ctx
                return OrderResponse(
                    broker_order_id="",
                    status="REJECTED",
                    message="blocked_by_custom_interceptor",
                    filled_quantity=0,
                    average_price=None,
                )

        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
            interceptors=[_BlockAllInterceptor()],
        )

        order_req = OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert response.message == "blocked_by_custom_interceptor"
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_global_kill_switch_blocks_entry_orders(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.setenv("GLOBAL_KILL", "1")
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-13",
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

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        order_req = OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert response.message == "global_kill_switch_enabled"
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_global_kill_switch_allows_exit_orders(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.setenv("GLOBAL_KILL", "1")
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-14",
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

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
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
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_position_ownership_blocks_cross_strategy_entry(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-PO-1",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=120.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {
                "NIFTY_OPT": {
                    "symbol": "NIFTY17FEB2625750CE",
                    "token": "12345",
                    "lot_size": 1,
                    "underlying": "NIFTY",
                    "kind": "ATM_CE",
                    "expiry": "2026-02-17",
                    "strike": 25750,
                }
            }
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        first_req = OrderRequest(
            symbol="NIFTY17FEB2625750CE",
            symbol_token="12345",
            quantity=1,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
        )
        _, first = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=first_req,
        )
        assert first.status == "FILLED"

        second_req = OrderRequest(
            symbol="NIFTY17FEB2625750CE",
            symbol_token="12345",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
        )
        _, second = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-b"),
            order_req=second_req,
        )
        assert second.status == "REJECTED"
        assert "CONTRACT_LOCKED" in str(second.message)
        assert "owner=strategy-a" in str(second.message)
        assert "requested=strategy-b" in str(second.message)
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_position_ownership_same_owner_exit_releases_lock(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-PO-2",
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

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {
                "NIFTY_OPT": {
                    "symbol": "NIFTY17FEB2625750CE",
                    "token": "12345",
                    "lot_size": 1,
                    "underlying": "NIFTY",
                    "kind": "ATM_CE",
                    "expiry": "2026-02-17",
                    "strike": 25750,
                }
            }
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        _, entry_resp = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert entry_resp.status == "FILLED"

        _, exit_resp = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
            ),
        )
        assert exit_resp.status == "FILLED"

        _, next_entry_resp = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-b"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert next_entry_resp.status == "FILLED"
        assert runner.place_order.call_count == 3
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_position_ownership_unknown_contract_blocks_entry(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-PO-3",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=101.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {"BROKEN_OPT": {"symbol": "BROKEN100CE", "token": "999", "lot_size": 1}}
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="BROKEN100CE",
                symbol_token="999",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )

        assert response.status == "REJECTED"
        assert "CONTRACT_LOCKED owner=UNKNOWN" in str(response.message)
        assert "detail=missing_expiry" in str(response.message)
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_rejects_live_entry_without_broker_token(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.setenv("TRADE_MODE", "LIVE")

        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-PO-LIVE-1",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=101.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {
                "NIFTY_OPT": {
                    "symbol": "NIFTY17FEB2625750CE",
                    "lot_size": 1,
                    "underlying": "NIFTY",
                    "kind": "ATM_CE",
                    "expiry": "2026-02-17",
                    "strike": "25750",
                }
            }
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
            submission_outbox=MagicMock(),
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )

        assert response.status == "REJECTED"
        assert "MISSING_BROKER_TOKEN" in str(response.message)
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_position_ownership_system_exit_bypass_uses_owner_lock(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-PO-4",
                status="FILLED",
                message="ok",
                filled_quantity=1,
                average_price=102.0,
            )
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {
                "NIFTY_OPT": {
                    "symbol": "NIFTY17FEB2625750CE",
                    "token": "12345",
                    "lot_size": 1,
                    "underlying": "NIFTY",
                    "kind": "ATM_CE",
                    "expiry": "2026-02-17",
                    "strike": 25750,
                }
            }
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        _, entry_resp = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert entry_resp.status == "FILLED"

        _, blocked_exit = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("eod_exit"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
                position_ownership_bypass=False,
            ),
        )
        assert blocked_exit.status == "REJECTED"

        _, bypass_exit = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("eod_exit"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
                position_ownership_bypass=True,
            ),
        )
        assert bypass_exit.status == "FILLED"

        _, stale_bypass_exit = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("eod_exit"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.EXIT,
                position_ownership_bypass=True,
            ),
        )
        assert stale_bypass_exit.status == "REJECTED"
        assert "no_owned_position_to_exit" in str(stale_bypass_exit.message)

        _, next_entry = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-b"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert next_entry.status == "FILLED"
        assert runner.place_order.call_count == 3
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_position_ownership_rejected_submission_releases_pending_lock(monkeypatch):
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            side_effect=[
                OrderResponse(
                    broker_order_id="OID-PO-RJ",
                    status="REJECTED",
                    message="broker_reject",
                    filled_quantity=0,
                    average_price=None,
                ),
                OrderResponse(
                    broker_order_id="OID-PO-OK",
                    status="FILLED",
                    message="ok",
                    filled_quantity=1,
                    average_price=99.5,
                ),
            ]
        )

        hub = MagicMock()
        hub.get_runner.return_value = runner

        state_store = MagicMock()
        state_store.get_balance.return_value = None
        state_store.get_positions.return_value = []

        settings = SimpleNamespace(
            enable_profit_checks=False,
            enable_capital_checks=False,
            capital_auto_resize_enabled=False,
            enable_risk_checks=False,
            order_router_enforce_global_kill_switch=False,
            position_ownership_enabled=True,
            position_ownership_unknown_mode="block_entries",
            position_ownership_allow_system_exit=True,
        )
        monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
        monkeypatch.setattr("app.orders.router.get_runtime_settings", lambda **_kwargs: settings)

        dashboard_bus.set_instrument_meta(
            {
                "NIFTY_OPT": {
                    "symbol": "NIFTY17FEB2625750CE",
                    "token": "12345",
                    "lot_size": 1,
                    "underlying": "NIFTY",
                    "kind": "ATM_CE",
                    "expiry": "2026-02-17",
                    "strike": 25750,
                }
            }
        )
        router = OrderRouter(
            hub=hub,
            capital_engine=None,
            risk_engine=None,
            profit_engine=None,
            state_store=state_store,
        )

        _, rejected = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-a"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert rejected.status == "REJECTED"

        _, next_resp = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-b"),
            order_req=OrderRequest(
                symbol="NIFTY17FEB2625750CE",
                symbol_token="12345",
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                purpose=OrderPurpose.ENTRY,
            ),
        )
        assert next_resp.status == "FILLED"
        assert runner.place_order.call_count == 2
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


# ---------------------------------------------------------------------------
# Issue #68: lifecycle persistence mandatory after broker submit in LIVE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_lifecycle_register_failure_blocks_and_raises(monkeypatch):
    """Regression #68: In LIVE mode, a lifecycle registration failure after broker submit
    must block further order flow and re-raise rather than falling back to trade/PnL recording.
    """
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        runner = MagicMock()
        runner.is_running = True
        runner.place_order = AsyncMock(
            return_value=OrderResponse(
                broker_order_id="OID-LIVE-1",
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

        capital_engine = MagicMock()
        capital_engine.can_afford_order.return_value = (True, "capital_ok")
        capital_engine.suggest_order_quantity.return_value = (1, "ok")

        risk_engine = MagicMock()
        risk_engine.check_order_allowed.return_value = (True, "risk_ok")

        class _BrokenLifecycle:
            def register_submission(self, **_kwargs):
                raise RuntimeError("lifecycle DB unavailable")

        dashboard_bus.set_instrument_meta({
            "NIFTY17FEB2625750CE": {"lot_size": 1, "label": "NIFTY_ATM_CE"}
        })

        from app.orders.order_outbox import InMemoryOrderSubmissionOutbox

        router = OrderRouter(
            hub=hub,
            capital_engine=capital_engine,
            risk_engine=risk_engine,
            profit_engine=None,
            state_store=state_store,
            order_lifecycle=_BrokenLifecycle(),
            submission_outbox=InMemoryOrderSubmissionOutbox(),
        )
        assert router._is_live_mode is True, "Router must detect LIVE mode from env"

        with pytest.raises(RuntimeError, match="lifecycle DB unavailable"):
            await router.submit_order(
                tenant_id=TenantId("tenant-1"),
                broker_account_id=BrokerAccountId("account-1"),
                strategy_id=StrategyId("ema20-strategy"),
                order_req=OrderRequest(
                    symbol="NIFTY17FEB2625750CE",
                    symbol_token="12345",
                    quantity=1,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    product_type=ProductType.INTRADAY,
                    time_in_force=TimeInForce.DAY,
                    purpose=OrderPurpose.ENTRY,
                ),
            )

        # After lifecycle failure, new order entries must be blocked.
        assert router._startup_recovery_block_new_entries is True, (
            "Router must block new entries after lifecycle persist failure in LIVE"
        )
    finally:
        monkeypatch.delenv("TRADE_MODE", raising=False)
        dashboard_bus.set_instrument_meta(original_meta)


# ---------------------------------------------------------------------------
# Issue #220 — block_exits HARD-trip policy in GlobalKillSwitchInterceptor.
# Soft trip (block_exits=False, default): block entries, allow exits — used
# by the legacy daily-loss / drawdown auto-trip bridge. Hard trip
# (block_exits=True): block ALL orders including exits — operator panic stop.
# ---------------------------------------------------------------------------


def _build_router_for_killswitch_test(monkeypatch, *, runner_response):
    """Common fixture: build an OrderRouter with all gates disabled except
    the GlobalKillSwitchInterceptor."""
    runner = MagicMock()
    runner.is_running = True
    runner.place_order = AsyncMock(return_value=runner_response)

    hub = MagicMock()
    hub.get_runner.return_value = runner

    state_store = MagicMock()
    state_store.get_balance.return_value = None
    state_store.get_positions.return_value = []

    settings = SimpleNamespace(
        enable_profit_checks=False,
        enable_capital_checks=False,
        capital_auto_resize_enabled=False,
        enable_risk_checks=False,
        order_router_enforce_global_kill_switch=True,
    )
    monkeypatch.setattr("app.orders.router.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.orders.router.get_runtime_settings", lambda **_kwargs: settings,
    )
    dashboard_bus.set_instrument_meta(
        {"SBIN": {"symbol": "SBIN", "token": "SBIN", "lot_size": 1}}
    )
    return OrderRouter(
        hub=hub,
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
    ), runner


@pytest.mark.asyncio
async def test_router_global_kill_env_with_block_exits_rejects_exit(monkeypatch):
    """Issue #220: env-var hard trip via GLOBAL_KILL_BLOCK_EXITS=1 must
    reject EXIT orders too, with a distinct rejection message."""
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.setenv("GLOBAL_KILL", "1")
        monkeypatch.setenv("GLOBAL_KILL_BLOCK_EXITS", "1")
        router, runner = _build_router_for_killswitch_test(
            monkeypatch,
            runner_response=OrderResponse(
                broker_order_id="OID-HARD-EXIT", status="FILLED",
                message="should-not-fill", filled_quantity=1, average_price=100.0,
            ),
        )

        order_req = OrderRequest(
            symbol="SBIN", quantity=1, side=OrderSide.SELL,
            order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY, purpose=OrderPurpose.EXIT,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert response.message == "global_kill_switch_enabled_block_exits"
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_ksm_soft_trip_allows_exit(monkeypatch):
    """Issue #220: when the durable KSM has a SOFT trip (block_exits=False),
    EXIT orders must still flow — preserves the current default and is
    required for trailing-lock to flatten exposure during legacy auto-trip."""
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        # No env trip — exercising the durable-KSM path.
        monkeypatch.delenv("GLOBAL_KILL", raising=False)
        monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)

        ksm_stub = MagicMock()
        ksm_stub.is_tripped_for_scope_with_block_exits.return_value = (True, False)
        runtime_stub = SimpleNamespace(kill_switch_manager=ksm_stub)
        monkeypatch.setattr(
            "app.hub.runtime.get_hub_runtime", lambda: runtime_stub,
        )

        router, runner = _build_router_for_killswitch_test(
            monkeypatch,
            runner_response=OrderResponse(
                broker_order_id="OID-SOFT-EXIT", status="FILLED",
                message="ok", filled_quantity=1, average_price=100.0,
            ),
        )

        order_req = OrderRequest(
            symbol="SBIN", quantity=1, side=OrderSide.SELL,
            order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY, purpose=OrderPurpose.EXIT,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "FILLED", (
            "SOFT KSM trip must allow exit orders to route (#220 default)"
        )
        runner.place_order.assert_called_once()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_ksm_hard_trip_rejects_exit(monkeypatch):
    """Issue #220: when the durable KSM has a HARD trip (block_exits=True),
    EXIT orders must be rejected with a distinct message and event."""
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.delenv("GLOBAL_KILL", raising=False)
        monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)

        ksm_stub = MagicMock()
        ksm_stub.is_tripped_for_scope_with_block_exits.return_value = (True, True)
        runtime_stub = SimpleNamespace(kill_switch_manager=ksm_stub)
        monkeypatch.setattr(
            "app.hub.runtime.get_hub_runtime", lambda: runtime_stub,
        )

        router, runner = _build_router_for_killswitch_test(
            monkeypatch,
            runner_response=OrderResponse(
                broker_order_id="OID-HARD-KSM-EXIT", status="FILLED",
                message="should-not-fill", filled_quantity=1, average_price=100.0,
            ),
        )

        order_req = OrderRequest(
            symbol="SBIN", quantity=1, side=OrderSide.SELL,
            order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY, purpose=OrderPurpose.EXIT,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert response.message == "kill_switch_manager_tripped_block_exits"
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)


@pytest.mark.asyncio
async def test_router_ksm_hard_trip_rejects_entry(monkeypatch):
    """Issue #220 regression: HARD trip must continue to reject entries
    (existing behaviour preserved)."""
    original_meta = dict(getattr(dashboard_bus, "_instrument_meta", {}))
    try:
        monkeypatch.delenv("GLOBAL_KILL", raising=False)
        monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)

        ksm_stub = MagicMock()
        ksm_stub.is_tripped_for_scope_with_block_exits.return_value = (True, True)
        runtime_stub = SimpleNamespace(kill_switch_manager=ksm_stub)
        monkeypatch.setattr(
            "app.hub.runtime.get_hub_runtime", lambda: runtime_stub,
        )

        router, runner = _build_router_for_killswitch_test(
            monkeypatch,
            runner_response=OrderResponse(
                broker_order_id="OID-HARD-KSM-ENTRY", status="FILLED",
                message="should-not-fill", filled_quantity=1, average_price=100.0,
            ),
        )

        order_req = OrderRequest(
            symbol="SBIN", quantity=1, side=OrderSide.BUY,
            order_type=OrderType.MARKET, product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY, purpose=OrderPurpose.ENTRY,
        )

        _, response = await router.submit_order(
            tenant_id=TenantId("tenant-1"),
            broker_account_id=BrokerAccountId("account-1"),
            strategy_id=StrategyId("strategy-1"),
            order_req=order_req,
        )

        assert response.status == "REJECTED"
        assert response.message == "kill_switch_manager_tripped"
        runner.place_order.assert_not_called()
    finally:
        dashboard_bus.set_instrument_meta(original_meta)
