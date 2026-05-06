import importlib
import types
from datetime import datetime, time, timezone
import pytest

from app.brokers.base import OrderResponse

MODULE_PATH = "app.strategies.ce_orb"
CALLABLE_NAMES = ['PositionState', 'SymbolState', 'CeOrbStrategy']


class DummyRiskManager:
    def __init__(self, restored_positions=None):
        self.restored_positions = restored_positions or {}
        self.open_positions = {}

    def update_position_from_broker(
        self,
        *,
        label,
        side,
        qty,
        entry_price,
        exchange=None,
        symboltoken=None,
        tradingsymbol=None,
        template_name=None,
        strategy_name=None,
        strategy_context=None,
    ):
        existing = dict(self.open_positions.get(label, {}))
        existing.update(
            {
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "template_name": template_name,
                "strategy_name": strategy_name,
            }
        )
        if strategy_context is not None:
            existing["strategy_context"] = dict(strategy_context)
        self.open_positions[label] = existing


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


def _make_strategy(
    monkeypatch,
    *,
    moneyness="ATM",
    itm_offset=0,
    return_module=False,
    risk_manager=None,
):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_options=False,
            use_hub_router_for_banknifty_options=False,
        ),
    )
    instrument_meta = {
        "NIFTY_ITM_CE_19950": {"symbol": "NIFTY26FEB19950CE", "kind": "CE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "101"},
        "NIFTY_ATM_CE_20000": {"symbol": "NIFTY26FEB20000CE", "kind": "CE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "102"},
        "NIFTY_OTM_CE_20050": {"symbol": "NIFTY26FEB20050CE", "kind": "CE", "underlying": "NIFTY", "lot_size": 50, "exchange": "NFO", "token": "103"},
    }
    strategy = mod.CeOrbStrategy(
        instrument_meta,
        order_client=None,
        risk_manager=risk_manager,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "moneyness": moneyness,
            "itm_strike_offset": itm_offset,
        },
    )
    if return_module:
        return mod, strategy
    return strategy


def test_ce_orb_select_call_option_atm(monkeypatch):
    strategy = _make_strategy(monkeypatch, moneyness="ATM", itm_offset=0)
    assert strategy._select_call_option(20010) == "NIFTY_ATM_CE_20000"


def test_ce_orb_select_call_option_itm(monkeypatch):
    strategy = _make_strategy(monkeypatch, moneyness="ITM", itm_offset=1)
    assert strategy._select_call_option(20010) == "NIFTY_ITM_CE_19950"


def test_ce_orb_entry_window(monkeypatch):
    strategy = _make_strategy(monkeypatch)
    assert strategy._within_entry_window(time(9, 45)) is True
    assert strategy._within_entry_window(time(15, 30)) is False


def test_ce_orb_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
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

    option_label = "NIFTY_ATM_CE_20000"
    strategy.last_price[option_label] = 100.0
    strategy._enter_position(types.SimpleNamespace(c=20005.0, h=20010.0, low=19990.0), vwap=20000.0)

    assert strategy.state.position is not None
    assert len(calls) == 1
    entry_symbol = calls[0].symbol
    assert entry_symbol != option_label

    strategy.instrument_meta[option_label] = {
        "exchange": "NFO",
        "token": "102",
        "underlying": "NIFTY",
        "kind": "CE",
    }
    strategy._exit_position("EOD")

    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_ce_orb_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_CE_ORB_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_CE_ORB_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_CE_ORB_EXIT_CIRCUIT_OPEN_SECONDS", "30")
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
    strategy.last_price["NIFTY_ATM_CE_20000"] = 100.0
    strategy._enter_position(types.SimpleNamespace(c=20005.0, h=20010.0, low=19990.0), vwap=20000.0)
    assert strategy.state.position is not None
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


def test_ce_orb_rehydrates_position_and_uses_restored_vwap_for_exit(monkeypatch):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_ATM_CE_20000": {
                "side": "BUY",
                "qty": 1,
                "entry_price": 100.0,
                "template_name": "ce-orb",
                "strategy_name": "ce-orb",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "102",
                "tradingsymbol": "NIFTY26FEB20000CE",
                "strategy_context": {
                    "sl_price": 75.0,
                    "target_price": 140.0,
                    "position_qty": 1,
                    "last_vwap": 20000.0,
                },
            }
        }
    )
    mod, strategy = _make_strategy(monkeypatch, return_module=True, risk_manager=risk_manager)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)

    assert strategy.state.position is not None
    assert strategy.state.last_vwap == 20000.0

    candle = types.SimpleNamespace(
        start_ts=datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc),
        end_ts=datetime(2025, 1, 1, 5, 1, tzinfo=timezone.utc),
        o=20010.0,
        h=20015.0,
        low=19985.0,
        c=19990.0,
    )
    strategy.on_bar("NIFTY_IDX", strategy.timeframe_seconds, candle, {})

    assert strategy.state.position is None
    assert len(calls) == 1
    assert calls[0].tag == "CE_ORB_EXIT_VWAP_EXIT"
