from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.orders.interceptors import (
    GlobalKillSwitchInterceptor,
    OrderInterceptionContext,
)


def _ctx(*, is_exit_order: bool) -> OrderInterceptionContext:
    return OrderInterceptionContext(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("account-1"),
        strategy_id=StrategyId("strategy-1"),
        correlation_id="corr-1",
        request_id="req-1",
        order_req=OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.SELL if is_exit_order else OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.EXIT if is_exit_order else OrderPurpose.ENTRY,
        ),
        settings=SimpleNamespace(order_router_enforce_global_kill_switch=True),
        is_exit_order=is_exit_order,
        lot_size=1,
        instrument_meta=None,
        reference_price=None,
        balance=None,
        positions=[],
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
    )


def _runtime(*, divergence: dict, ksm_tripped: bool = False, block_exits: bool = False):
    ksm = MagicMock()
    ksm.is_tripped_for_scope_with_block_exits.return_value = (
        ksm_tripped,
        block_exits,
    )
    return SimpleNamespace(
        kill_switch_manager=ksm,
        compute_kill_switch_divergence=lambda: dict(divergence),
    )


def test_live_legacy_durable_divergence_rejects_entry(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("GLOBAL_KILL", raising=False)
    monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: _runtime(
            divergence={
                "divergent": True,
                "legacy_active": True,
                "durable_global_active": False,
                "legacy_reason": "floating_drawdown",
            },
        ),
    )

    response = GlobalKillSwitchInterceptor().evaluate(
        _ctx(is_exit_order=False),
    )

    assert response is not None
    assert response.status == "REJECTED"
    assert response.message == "kill_switch_divergence_legacy_active"


def test_live_legacy_durable_divergence_allows_soft_exit(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("GLOBAL_KILL", raising=False)
    monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: _runtime(
            divergence={
                "divergent": True,
                "legacy_active": True,
                "durable_global_active": False,
                "legacy_reason": "floating_drawdown",
            },
            ksm_tripped=False,
            block_exits=False,
        ),
    )

    response = GlobalKillSwitchInterceptor().evaluate(
        _ctx(is_exit_order=True),
    )

    assert response is None


def test_live_durable_hard_trip_blocks_exit_during_divergence(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("GLOBAL_KILL", raising=False)
    monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: _runtime(
            divergence={
                "divergent": True,
                "legacy_active": True,
                "durable_global_active": False,
                "legacy_reason": "floating_drawdown",
            },
            ksm_tripped=True,
            block_exits=True,
        ),
    )

    response = GlobalKillSwitchInterceptor().evaluate(
        _ctx(is_exit_order=True),
    )

    assert response is not None
    assert response.status == "REJECTED"
    assert response.message == "kill_switch_manager_tripped_block_exits"


def test_paper_divergence_does_not_reject(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "PAPER")
    monkeypatch.delenv("GLOBAL_KILL", raising=False)
    monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)

    def _unexpected_divergence_call():
        raise AssertionError("PAPER must not compute kill-switch divergence")

    ksm = MagicMock()
    ksm.is_tripped_for_scope_with_block_exits.return_value = (False, False)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: SimpleNamespace(
            kill_switch_manager=ksm,
            compute_kill_switch_divergence=_unexpected_divergence_call,
        ),
    )

    response = GlobalKillSwitchInterceptor().evaluate(
        _ctx(is_exit_order=False),
    )

    assert response is None


def test_live_divergence_unavailable_rejects_entry(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.delenv("GLOBAL_KILL", raising=False)
    monkeypatch.delenv("GLOBAL_KILL_BLOCK_EXITS", raising=False)
    monkeypatch.setattr(
        "app.hub.runtime.get_hub_runtime",
        lambda: SimpleNamespace(
            kill_switch_manager=None,
            compute_kill_switch_divergence=lambda: (_ for _ in ()).throw(
                RuntimeError("hub unavailable")
            ),
        ),
    )

    response = GlobalKillSwitchInterceptor().evaluate(
        _ctx(is_exit_order=False),
    )

    assert response is not None
    assert response.status == "REJECTED"
    assert response.message == "kill_switch_divergence_unavailable"
