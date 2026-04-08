import importlib
import types
import pytest
from app.brokers.base import (
    Balance,
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)

MODULE_PATH = "app.risk.capital_engine"
CALLABLE_NAMES = ['CapitalConfig', 'CapitalState', 'CapitalEngine']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_callable_behaviour_placeholder(name):
    mod = importlib.import_module(MODULE_PATH)
    obj = getattr(mod, name)
    assert obj is not None


def _make_order(qty: int, *, limit_price=None):
    return OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=qty,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        stop_price=None,
    )


def _balance(*, available: float) -> Balance:
    return Balance(available=available, utilized=0.0, total=available)


def test_suggest_order_quantity_reduces_to_notional_limit():
    mod = importlib.import_module(MODULE_PATH)
    engine = mod.CapitalEngine()
    order = _make_order(150)

    qty, reason = engine.suggest_order_quantity(
        tenant_id="tenant-1",
        broker_account_id="A1",
        order=order,
        lot_size=75,
        reference_price=6000.0,
    )

    assert qty == 75
    assert "resized_for_max_notional_per_order" in reason


def test_suggest_order_quantity_keeps_valid_order():
    mod = importlib.import_module(MODULE_PATH)
    engine = mod.CapitalEngine()
    order = _make_order(75)

    qty, reason = engine.suggest_order_quantity(
        tenant_id="tenant-1",
        broker_account_id="A1",
        order=order,
        lot_size=75,
        reference_price=6000.0,
    )

    assert qty == 75
    assert reason == "within_max_notional_per_order"


def test_capital_engine_enforce_blocks_short_option_margin_breach(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    mod.get_settings.cache_clear()
    monkeypatch.setenv("CAPITAL_MARGIN_CHECK_MODE", "ENFORCE")
    engine = mod.CapitalEngine()
    order = OrderRequest(
        symbol="NATURALGAS24MAR26315CE",
        quantity=1250,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    allowed, reason = engine.can_afford_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        balance=_balance(available=250000.35),
        positions=[],
        order=order,
        reference_price=26.60,
        instrument_meta={"kind": "CE", "lot_size": 1250, "exchange": "MCX"},
        lot_size=1250,
    )

    assert allowed is False
    assert "MARGIN_SHORT_OPTION" in reason
    assert "required=" in reason
    assert "available=" in reason


def test_capital_engine_shadow_logs_margin_breach_without_blocking(
    monkeypatch,
    caplog,
):
    mod = importlib.import_module(MODULE_PATH)
    mod.get_settings.cache_clear()
    monkeypatch.setenv("CAPITAL_MARGIN_CHECK_MODE", "SHADOW")
    engine = mod.CapitalEngine()
    order = OrderRequest(
        symbol="NATURALGAS24MAR26315CE",
        quantity=1250,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    with caplog.at_level("INFO"):
        allowed, reason = engine.can_afford_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id="ema20-trend",
            balance=_balance(available=250000.35),
            positions=[],
            order=order,
            reference_price=26.60,
            instrument_meta={"kind": "CE", "lot_size": 1250, "exchange": "MCX"},
            lot_size=1250,
        )

    assert allowed is True
    assert reason == "capital_ok"
    text = "\n".join(rec.message for rec in caplog.records)
    assert "event_type=CAPITAL_MARGIN_SHADOW" in text
    assert "reason_code=MARGIN_SHORT_OPTION" in text
    assert "mode=SHADOW" in text


def test_capital_engine_enforce_uses_futures_requirement(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    mod.get_settings.cache_clear()
    monkeypatch.setenv("CAPITAL_MARGIN_CHECK_MODE", "ENFORCE")
    engine = mod.CapitalEngine()
    order = OrderRequest(
        symbol="NATURALGAS26MAR26FUT",
        quantity=1250,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.CARRY_FORWARD,
        time_in_force=TimeInForce.DAY,
    )

    allowed, reason = engine.can_afford_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        balance=_balance(available=30000.0),
        positions=[],
        order=order,
        reference_price=315.90,
        instrument_meta={"kind": "FUTURE", "lot_size": 1250, "exchange": "MCX"},
        lot_size=1250,
    )

    assert allowed is False
    assert "MARGIN_FUTURES" in reason


def test_capital_engine_enforce_keeps_long_option_buy_premium_based(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    mod.get_settings.cache_clear()
    monkeypatch.setenv("CAPITAL_MARGIN_CHECK_MODE", "ENFORCE")
    engine = mod.CapitalEngine()
    order = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=50,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    allowed, reason = engine.can_afford_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="exclusive_nifty_ce_buy",
        balance=_balance(available=10000.0),
        positions=[],
        order=order,
        reference_price=100.0,
        instrument_meta={"kind": "CE", "lot_size": 50, "exchange": "NFO"},
        lot_size=50,
    )

    assert allowed is True
    assert reason == "capital_ok"


def test_capital_engine_off_mode_keeps_legacy_short_option_behavior(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    mod.get_settings.cache_clear()
    monkeypatch.setenv("CAPITAL_MARGIN_CHECK_MODE", "OFF")
    engine = mod.CapitalEngine()
    order = OrderRequest(
        symbol="NATURALGAS24MAR26315CE",
        quantity=1250,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    allowed, reason = engine.can_afford_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        balance=_balance(available=250000.35),
        positions=[],
        order=order,
        reference_price=26.60,
        instrument_meta={"kind": "CE", "lot_size": 1250, "exchange": "MCX"},
        lot_size=1250,
    )

    assert allowed is True
    assert reason == "capital_ok"
