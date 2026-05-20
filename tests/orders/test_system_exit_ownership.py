from __future__ import annotations

from dataclasses import dataclass, field

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.data.state_store import StateStore
from app.orders.interceptors import OrderInterceptionContext
from app.orders.order_lifecycle import OrderLifecycleService
from app.orders.router import OrderRouter
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


@dataclass
class _PnlSpy:
    trade_events: list[object] = field(default_factory=list)
    close_events: list[object] = field(default_factory=list)
    open_events: list[object] = field(default_factory=list)

    def on_trade(self, event: object) -> None:
        self.trade_events.append(event)

    def on_close_position(self, event: object) -> None:
        self.close_events.append(event)

    def on_open_position(self, event: object) -> None:
        self.open_events.append(event)


def _exit_request(owner: str) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.EXIT,
        position_ownership_bypass=True,
        strategy_id=owner,
    )


def test_router_uses_exit_order_strategy_hint_as_ownership_fallback() -> None:
    order_req = _exit_request("ema20_strategy")
    policy_ctx = OrderInterceptionContext(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="system::position_trailing_lock",
        correlation_id=None,
        request_id=None,
        order_req=order_req,
        settings=object(),
        is_exit_order=True,
        lot_size=None,
        instrument_meta=None,
        reference_price=None,
        balance=None,
        positions=[],
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
    )

    assert (
        OrderRouter._resolve_ownership_strategy_id(
            strategy_id="system::position_trailing_lock",
            order_req=order_req,
            policy_ctx=policy_ctx,
        )
        == "ema20_strategy"
    )


def test_lifecycle_attributes_system_exit_pnl_to_ownership_strategy(monkeypatch) -> None:
    pnl_spy = _PnlSpy()
    lifecycle = OrderLifecycleService(
        state_store=StateStore(),
        pnl_engine=pnl_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="system::position_trailing_lock",
        hub_order_id="t1:ba1:system::position_trailing_lock:exit",
        order_req=_exit_request("ema20_strategy"),
        response=OrderResponse(
            broker_order_id="OID-SYS-EXIT",
            status="COMPLETE",
            message="filled",
            filled_quantity=65,
            average_price=123.45,
        ),
        ownership_strategy_id="ema20_strategy",
    )

    assert len(pnl_spy.trade_events) == 1
    assert pnl_spy.trade_events[0].strategy_id == "ema20_strategy"
    assert len(pnl_spy.close_events) == 1
    assert pnl_spy.close_events[0].strategy_id == "ema20_strategy"
