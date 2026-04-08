import importlib
import types
from datetime import time
import pytest

from app.brokers.base import OrderResponse

MODULE_PATH = "app.strategies.put_reversion_writer"
CALLABLE_NAMES = ['PutReversionWriterConfig', 'ReversionInstrumentState', 'PutReversionWriterStrategy']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


def _make_strategy(monkeypatch, *, otm_steps=1, return_module=False):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_options=False,
            use_hub_router_for_banknifty_options=False,
            use_hub_router_for_finnifty_options=False,
            use_hub_router_for_sensex_options=False,
            use_hub_router_for_midcpnifty_options=False,
        ),
    )

    instrument_meta = {
        "NIFTY_ATM_PE_20000": {"symbol": "NIFTY26FEB20000PE", "kind": "PE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "301"},
        "NIFTY_OTM_PE_19950": {"symbol": "NIFTY26FEB19950PE", "kind": "PE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "302"},
        "NIFTY_OTM_PE_19900": {"symbol": "NIFTY26FEB19900PE", "kind": "PE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "303"},
    }
    strategy = mod.PutReversionWriterStrategy(
        instrument_meta,
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={"otm_steps": otm_steps},
    )
    if return_module:
        return mod, strategy
    return strategy


def test_put_reversion_select_otm(monkeypatch):
    strategy = _make_strategy(monkeypatch, otm_steps=1)
    assert strategy._select_put_option(20000) == "NIFTY_OTM_PE_19950"


def test_put_reversion_select_deeper_otm(monkeypatch):
    strategy = _make_strategy(monkeypatch, otm_steps=2)
    assert strategy._select_put_option(20000) == "NIFTY_OTM_PE_19900"


def test_put_reversion_entry_window(monkeypatch):
    strategy = _make_strategy(monkeypatch)
    assert strategy._within_entry_window(time(10, 15)) is True
    assert strategy._within_entry_window(time(15, 0)) is False


def test_put_reversion_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch, return_module=True)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    option_label = "NIFTY_OTM_PE_19950"
    strategy.last_price[option_label] = 100.0
    strategy._enter_position(types.SimpleNamespace(c=20000.0))
    assert strategy.state.position is True
    assert len(calls) == 1
    entry_symbol = calls[0].symbol
    assert entry_symbol != option_label

    strategy.instrument_meta[option_label] = {
        "exchange": "NFO",
        "token": "302",
        "underlying": "NIFTY",
        "kind": "PE",
    }
    strategy._exit_position("EOD")
    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_put_reversion_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_PUT_REV_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_PUT_REV_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_PUT_REV_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    mod, strategy = _make_strategy(monkeypatch, return_module=True)
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
    strategy.last_price["NIFTY_OTM_PE_19950"] = 100.0
    strategy._enter_position(types.SimpleNamespace(c=20000.0))
    assert strategy.state.position is True
    assert len(calls) == 1

    strategy._exit_position("EOD")
    assert len(calls) == 2
    clock["t"] += 0.6
    strategy._exit_position("EOD")
    assert len(calls) == 3
    assert strategy._exit_failure_count == 2
    assert strategy._exit_circuit_open_until_mono > clock["t"]
    clock["t"] += 0.1
    strategy._exit_position("EOD")
    assert len(calls) == 3
