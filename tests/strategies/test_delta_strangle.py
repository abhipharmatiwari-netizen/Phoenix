import importlib
import types
from datetime import datetime, timezone
import pytest

MODULE_PATH = "app.strategies.delta_strangle"
CALLABLE_NAMES = ['_to_ist', 'DeltaStrangleStrategy']


class DummyRiskManager:
    def __init__(self, restored_positions=None):
        self.current_trade_mode = "PAPER"
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


def test_to_ist_converts_timezone():
    mod = importlib.import_module(MODULE_PATH)
    dt = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    ist = mod._to_ist(dt)
    assert ist.tzinfo is not None
    assert ist.utcoffset() is not None
    assert ist.utcoffset().total_seconds() == 19800


def test_resolve_leg_qty_uses_lot_size(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_delta=False,
            use_hub_router_for_banknifty_delta=False,
        ),
    )

    instrument_meta = {
        "NIFTY_ATM_CE_20000": {"lot_size": 50},
    }
    strategy = mod.DeltaStrangleStrategy(
        instrument_meta,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
    )
    assert strategy._resolve_leg_qty("NIFTY_ATM_CE_20000", 2) == 100


def test_delta_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_delta=False,
            use_hub_router_for_banknifty_delta=False,
        ),
    )
    instrument_meta = {
        "NIFTY_OTM_CE_20100": {
            "underlying": "NIFTY",
            "kind": "OTM_CE",
            "strike": 20100,
            "lot_size": 50,
            "symbol": "NIFTY26FEB20100CE",
            "exchange": "NFO",
            "token": "501",
        },
        "NIFTY_OTM_PE_19900": {
            "underlying": "NIFTY",
            "kind": "OTM_PE",
            "strike": 19900,
            "lot_size": 50,
            "symbol": "NIFTY26FEB19900PE",
            "exchange": "NFO",
            "token": "502",
        },
    }
    strategy = mod.DeltaStrangleStrategy(
        instrument_meta,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        entry_start_ist="00:00",
        entry_end_ist="23:59",
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return types.SimpleNamespace(status="ACCEPTED", message="ok")

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    strategy.last_price["NIFTY_OTM_CE_20100"] = 10.0
    strategy.last_price["NIFTY_OTM_PE_19900"] = 11.0
    strategy.last_price["NIFTY_IDX"] = 20000.0
    strategy._maybe_fire_strangle()

    assert len(calls) == 2
    leg = strategy.open_legs["NIFTY_OTM_CE_20100"]
    entry_symbol = calls[0].symbol
    assert entry_symbol != leg.label

    strategy.instrument_meta[leg.label] = {
        "underlying": "NIFTY",
        "kind": "OTM_CE",
        "strike": 20100,
        "lot_size": 50,
        "exchange": "NFO",
        "token": "501",
    }
    ok = strategy._exit_leg(leg, reason="TP", ltp=9.0)
    assert ok is True
    assert len(calls) == 3
    assert calls[2].symbol == entry_symbol


def test_delta_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_DELTA_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_DELTA_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_DELTA_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_delta=False,
            use_hub_router_for_banknifty_delta=False,
        ),
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "monotonic", lambda: clock["t"])
    instrument_meta = {
        "NIFTY_OTM_CE_20100": {
            "underlying": "NIFTY",
            "kind": "OTM_CE",
            "strike": 20100,
            "lot_size": 50,
            "symbol": "NIFTY26FEB20100CE",
            "exchange": "NFO",
            "token": "501",
        },
        "NIFTY_OTM_PE_19900": {
            "underlying": "NIFTY",
            "kind": "OTM_PE",
            "strike": 19900,
            "lot_size": 50,
            "symbol": "NIFTY26FEB19900PE",
            "exchange": "NFO",
            "token": "502",
        },
    }
    strategy = mod.DeltaStrangleStrategy(
        instrument_meta,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        entry_start_ist="00:00",
        entry_end_ist="23:59",
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        if len(calls) <= 2:
            return types.SimpleNamespace(status="ACCEPTED", message="ok")
        return types.SimpleNamespace(status="REJECTED", message="insufficient funds")

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    strategy.last_price["NIFTY_OTM_CE_20100"] = 10.0
    strategy.last_price["NIFTY_OTM_PE_19900"] = 11.0
    strategy.last_price["NIFTY_IDX"] = 20000.0
    strategy._maybe_fire_strangle()
    leg = strategy.open_legs["NIFTY_OTM_CE_20100"]

    assert strategy._exit_leg(leg, reason="SL", ltp=12.0) is False
    assert len(calls) == 3
    clock["t"] += 0.6
    assert strategy._exit_leg(leg, reason="SL", ltp=12.0) is False
    assert len(calls) == 4
    assert strategy._exit_failure_count_by_label[leg.label] == 2
    assert strategy._exit_circuit_open_until_mono_by_label[leg.label] > clock["t"]
    clock["t"] += 0.1
    assert strategy._exit_leg(leg, reason="SL", ltp=12.0) is False
    assert len(calls) == 4


def test_delta_entry_carries_strategy_context_and_syncs_risk_manager(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_delta=False,
            use_hub_router_for_banknifty_delta=False,
        ),
    )
    risk_manager = DummyRiskManager()
    instrument_meta = {
        "NIFTY_OTM_CE_20100": {
            "underlying": "NIFTY",
            "kind": "OTM_CE",
            "strike": 20100,
            "lot_size": 50,
            "symbol": "NIFTY26FEB20100CE",
            "exchange": "NFO",
            "token": "501",
        },
        "NIFTY_OTM_PE_19900": {
            "underlying": "NIFTY",
            "kind": "OTM_PE",
            "strike": 19900,
            "lot_size": 50,
            "symbol": "NIFTY26FEB19900PE",
            "exchange": "NFO",
            "token": "502",
        },
    }
    strategy = mod.DeltaStrangleStrategy(
        instrument_meta,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        entry_start_ist="00:00",
        entry_end_ist="23:59",
        risk_manager=risk_manager,
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return types.SimpleNamespace(status="ACCEPTED", message="ok")

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    strategy.last_price["NIFTY_OTM_CE_20100"] = 10.0
    strategy.last_price["NIFTY_OTM_PE_19900"] = 11.0
    strategy.last_price["NIFTY_IDX"] = 20000.0

    strategy._maybe_fire_strangle()

    assert len(calls) == 2
    assert calls[0].position_label == "NIFTY_OTM_CE_20100"
    assert calls[0].strategy_context["leg_role"] == "CE"
    assert calls[0].strategy_context["managed_exit_mode"] == "strategy"
    assert calls[0].strategy_context["broker_brackets_enabled"] is False
    assert risk_manager.open_positions["NIFTY_OTM_CE_20100"]["strategy_context"]["leg_role"] == "CE"
    assert risk_manager.open_positions["NIFTY_OTM_PE_19900"]["strategy_context"]["leg_role"] == "PE"


def test_delta_restore_reconciles_broker_qty_and_uses_restored_identity_on_exit(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_delta=False,
            use_hub_router_for_banknifty_delta=False,
        ),
    )
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_OTM_CE_20100": {
                "side": "SELL",
                "qty": 100,
                "entry_price": 10.0,
                "template_name": "delta_strangle",
                "strategy_name": "nifty-delta-strangle",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "501",
                "tradingsymbol": "NIFTY26FEB20100CE",
                "strategy_context": {
                    "leg_role": "CE",
                    "sl_price": 13.0,
                    "tp_price": 7.0,
                    "entry_time": entry_ts,
                },
            },
            "NIFTY_OTM_PE_19900": {
                "side": "SELL",
                "qty": 50,
                "entry_price": 11.0,
                "template_name": "delta_strangle",
                "strategy_name": "nifty-delta-strangle",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "502",
                "tradingsymbol": "NIFTY26FEB19900PE",
                "strategy_context": {
                    "leg_role": "PE",
                    "qty_in_lots": 1,
                    "position_qty": 50,
                    "sl_price": 14.0,
                    "tp_price": 8.0,
                    "entry_time": entry_ts,
                },
            },
        }
    )
    strategy = mod.DeltaStrangleStrategy(
        {
            "NIFTY_OTM_CE_20100": {
                "underlying": "NIFTY",
                "kind": "OTM_CE",
                "strike": 20100,
                "lot_size": 50,
                "exchange": "NFO",
                "token": "501",
            },
            "NIFTY_OTM_PE_19900": {
                "underlying": "NIFTY",
                "kind": "OTM_PE",
                "strike": 19900,
                "lot_size": 50,
                "exchange": "NFO",
                "token": "502",
            },
        },
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        risk_manager=risk_manager,
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return types.SimpleNamespace(status="ACCEPTED", message="ok")

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)

    assert strategy.open_legs["NIFTY_OTM_CE_20100"].qty == 100
    assert strategy.open_legs["NIFTY_OTM_CE_20100"].qty_in_lots == 2
    assert strategy.last_entry_date.isoformat() == "2025-01-01"

    strategy.force_exit_all()

    assert len(calls) == 2
    ce_exit = next(req for req in calls if req.position_label == "NIFTY_OTM_CE_20100")
    assert ce_exit.symbol == "NIFTY26FEB20100CE"
    assert ce_exit.quantity == 2
