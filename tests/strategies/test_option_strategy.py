import importlib
import types
import pytest

from app.brokers.base import OrderResponse

MODULE_PATH = "app.strategies.option_strategy"
CALLABLE_NAMES = ['LegTemplate', 'SpreadTemplate', 'OpenLegPosition', 'OptionStrategy']


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


def _make_strategy(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_ng_options=False,
            use_hub_router_for_nifty_options=False,
            use_hub_router_for_banknifty_options=False,
            use_hub_router_for_finnifty_options=False,
            use_hub_router_for_sensex_options=False,
            use_hub_router_for_midcpnifty_options=False,
        ),
    )
    strategy = mod.OptionStrategy(
        instrument_meta={
            "NG_FUT": {"kind": "UNDERLYING", "underlying": "NG"},
            "NG_ATM_CE_100": {
                "symbol": "NATURALGAS20FEB26100CE",
                "underlying": "NG",
                "kind": "CE",
                "lot_size": 1,
                "exchange": "MCX",
                "token": "601",
            },
            "NG_ATM_PE_100": {
                "symbol": "NATURALGAS20FEB26100PE",
                "underlying": "NG",
                "kind": "PE",
                "lot_size": 1,
                "exchange": "MCX",
                "token": "602",
            },
        },
        order_client=None,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        ce_label_prefix="NG_ATM_CE",
        pe_label_prefix="NG_ATM_PE",
        risk_manager=None,
    )
    return mod, strategy


def test_option_strategy_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    label = "NG_ATM_CE_100"
    strategy.last_price[label] = 100.0
    strategy._fire_template("SHORT_CE")

    assert len(calls) == 1
    assert label in strategy.open_legs
    entry_symbol = calls[0].symbol
    assert entry_symbol != label

    strategy.instrument_meta[label] = {
        "underlying": "NG",
        "kind": "CE",
        "lot_size": 1,
        "exchange": "MCX",
        "token": "601",
    }
    strategy._update_open_leg_risk(label, ltp=200.0)

    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_option_strategy_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NG_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NG_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NG_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    mod, strategy = _make_strategy(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "monotonic", lambda: clock["t"])
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        if len(calls) == 1:
            return OrderResponse(
                broker_order_id="entry-1",
                status="ACCEPTED",
                message="ok",
            )
        return OrderResponse(
            broker_order_id=f"exit-{len(calls)}",
            status="REJECTED",
            message="insufficient funds",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    label = "NG_ATM_CE_100"
    strategy.last_price[label] = 100.0
    strategy._fire_template("SHORT_CE")
    assert label in strategy.open_legs
    assert len(calls) == 1

    strategy._update_open_leg_risk(label, ltp=200.0)
    assert len(calls) == 2
    clock["t"] += 0.6
    strategy._update_open_leg_risk(label, ltp=200.0)
    assert len(calls) == 3
    assert strategy._exit_failure_count_by_label[label] == 2
    assert strategy._exit_circuit_open_until_mono_by_label[label] > clock["t"]
    clock["t"] += 0.1
    strategy._update_open_leg_risk(label, ltp=200.0)
    assert len(calls) == 3


def test_option_strategy_reconciles_open_legs_across_atm_refresh(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    old_label = "NG_ATM_CE_100"
    strategy.last_price[old_label] = 100.0
    strategy._fire_template("SHORT_CE")

    old_meta = dict(strategy.instrument_meta)
    new_label = "NG_ATM_CE_100_ALT"
    new_meta = {
        "NG_FUT": {"kind": "UNDERLYING", "underlying": "NG"},
        new_label: {
            "symbol": "NATURALGAS20FEB26100CE",
            "underlying": "NG",
            "kind": "CE",
            "lot_size": 1,
            "exchange": "MCX",
            "token": "601",
        },
        "NG_ATM_PE_100": {
            "symbol": "NATURALGAS20FEB26100PE",
            "underlying": "NG",
            "kind": "PE",
            "lot_size": 1,
            "exchange": "MCX",
            "token": "602",
        },
    }

    strategy.refresh_atm_labels(new_meta)
    result = strategy.reconcile_open_legs(
        new_meta,
        previous_instrument_meta=old_meta,
    )

    assert result["open_legs_before"] == 1
    assert result["open_legs_after"] == 1
    assert result["rebound"] == 1
    assert old_label not in strategy.open_legs
    assert new_label in strategy.open_legs

    strategy.last_price[new_label] = 100.0
    strategy._fire_template("SHORT_CE")
    assert len(calls) == 1

    strategy._update_open_leg_risk(new_label, ltp=200.0)
    assert len(calls) == 2
    assert calls[1].symbol == "NATURALGAS20FEB26100CE"
