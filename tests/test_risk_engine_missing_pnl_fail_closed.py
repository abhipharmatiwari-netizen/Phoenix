from types import SimpleNamespace

from app.brokers.base import OrderPurpose, OrderRequest, OrderSide, OrderType, ProductType, TimeInForce
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.risk.account_loss_guard import AccountLossGuard
from app.risk import risk_engine as risk_engine_module
from app.risk.risk_engine import RiskEngine


def _settings(*, fail_closed: bool, fail_open: bool):
    return SimpleNamespace(
        risk_enable_daily_loss=True,
        risk_max_daily_loss=1000.0,
        risk_include_unrealized_in_daily_loss=True,
        risk_fail_open_on_missing_pnl=fail_open,
        risk_fail_closed_on_missing_pnl=fail_closed,
    )


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr(risk_engine_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        risk_engine_module,
        "get_runtime_settings",
        lambda **kwargs: settings,
    )


def _order(*, purpose: OrderPurpose | None) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY24APR22000CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=purpose,
    )


def test_missing_pnl_entry_blocked_when_fail_closed_enabled(monkeypatch):
    _patch_settings(monkeypatch, _settings(fail_closed=True, fail_open=True))
    engine = RiskEngine(account_loss_guard_store=AccountLossGuard())

    allowed, reason = engine.check_order_allowed(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("strategy-a"),
        order=_order(purpose=OrderPurpose.ENTRY),
    )

    assert allowed is False
    assert reason == "missing_pnl_fail_closed"


def test_missing_pnl_exit_allowed_when_fail_closed_enabled(monkeypatch):
    _patch_settings(monkeypatch, _settings(fail_closed=True, fail_open=False))
    engine = RiskEngine(account_loss_guard_store=AccountLossGuard())

    allowed, reason = engine.check_order_allowed(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("strategy-a"),
        order=_order(purpose=OrderPurpose.EXIT),
    )

    assert allowed is True
    assert reason == "missing_pnl_fail_closed_exit_allowed"


def test_missing_pnl_flag_off_preserves_fail_open_behavior(monkeypatch):
    _patch_settings(monkeypatch, _settings(fail_closed=False, fail_open=True))
    engine = RiskEngine(account_loss_guard_store=AccountLossGuard())

    allowed, reason = engine.check_order_allowed(
        tenant_id=TenantId("tenant-1"),
        broker_account_id=BrokerAccountId("A1"),
        strategy_id=StrategyId("strategy-a"),
        order=_order(purpose=OrderPurpose.ENTRY),
    )

    assert allowed is True
    assert reason == "pnl_engine_unavailable_fail_open"
