from __future__ import annotations

from types import SimpleNamespace

from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.risk.capital_engine import CapitalEngine


def _make_order() -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=75,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        limit_price=100.0,
        stop_price=None,
    )


def test_missing_state_allows_order_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(
        "app.risk.capital_engine.get_settings",
        lambda: SimpleNamespace(capital_fail_closed_on_missing_state=False),
    )
    engine = CapitalEngine()

    allowed, reason = engine.can_afford_order(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        balance=None,
        positions=None,
        order=_make_order(),
    )

    assert allowed is True
    assert reason == "balance_or_positions_missing_allowing_order"


def test_missing_state_rejects_order_when_flag_enabled(monkeypatch):
    monkeypatch.setattr(
        "app.risk.capital_engine.get_settings",
        lambda: SimpleNamespace(capital_fail_closed_on_missing_state=True),
    )
    engine = CapitalEngine()

    allowed, reason = engine.can_afford_order(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        balance=None,
        positions=None,
        order=_make_order(),
    )

    assert allowed is False
    assert reason == "missing_balance_or_positions_fail_closed"
