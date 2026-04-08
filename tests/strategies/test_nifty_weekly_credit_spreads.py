import importlib
import types
from datetime import date, datetime, timezone

import pytest

from app.brokers.base import OrderResponse


MODULE_PATH = "app.strategies.nifty_weekly_credit_spreads"


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


def _make_strategy(monkeypatch, *, risk_manager=None):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(
            use_hub_router_for_nifty_options=False,
        ),
    )
    strategy = mod.NiftyWeeklyCreditSpreadStrategy(
        instrument_meta={
            "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
            "NIFTY_PE_19800": {
                "symbol": "NIFTY26FEB19800PE",
                "underlying": "NIFTY",
                "kind": "PE",
                "exchange": "NFO",
                "token": "801",
                "expiry": "2026-02-26",
            },
            "NIFTY_PE_19700": {
                "symbol": "NIFTY26FEB19700PE",
                "underlying": "NIFTY",
                "kind": "PE",
                "exchange": "NFO",
                "token": "802",
                "expiry": "2026-02-26",
            },
        },
        order_client=None,
        risk_manager=risk_manager,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "account_equity": 1000000,
            "lot_size": 1,
            "risk_per_trade_pct": 0.5,
            "max_total_risk_pct": 1.0,
        },
    )
    return mod, strategy


def test_nifty_spread_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
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
    short_label = "NIFTY_PE_19800"
    long_label = "NIFTY_PE_19700"
    strategy.last_price[short_label] = 100.0
    strategy.last_price[long_label] = 80.0

    ok = strategy._enter_spread(
        spread_type="PUT_SPREAD",
        short_label=short_label,
        long_label=long_label,
        credit=20.0,
        width_pts=100.0,
        expiry=date(2026, 2, 26),
        extra=None,
    )
    assert ok is True
    assert len(calls) == 2
    spread_id = next(iter(strategy.open_spreads))
    entry_short_symbol = calls[0].symbol
    assert entry_short_symbol != short_label

    strategy.instrument_meta[short_label] = {
        "underlying": "NIFTY",
        "kind": "PE",
        "exchange": "NFO",
        "token": "801",
        "expiry": "2026-02-26",
    }
    strategy._exit_spread(spread_id)

    assert len(calls) == 4
    assert calls[2].symbol == entry_short_symbol


def test_nifty_spread_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_NIFTY_SPREAD_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_NIFTY_SPREAD_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_NIFTY_SPREAD_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    mod, strategy = _make_strategy(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(mod, "monotonic", lambda: clock["t"])
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        if len(calls) <= 2:
            return OrderResponse(
                broker_order_id=f"entry-{len(calls)}",
                status="ACCEPTED",
                message="ok",
            )
        return OrderResponse(
            broker_order_id=f"exit-{len(calls)}",
            status="REJECTED",
            message="insufficient funds",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    short_label = "NIFTY_PE_19800"
    long_label = "NIFTY_PE_19700"
    strategy.last_price[short_label] = 100.0
    strategy.last_price[long_label] = 80.0
    ok = strategy._enter_spread(
        spread_type="PUT_SPREAD",
        short_label=short_label,
        long_label=long_label,
        credit=20.0,
        width_pts=100.0,
        expiry=date(2026, 2, 26),
        extra=None,
    )
    assert ok is True
    spread_id = next(iter(strategy.open_spreads))

    strategy._exit_spread(spread_id)
    assert len(calls) == 4
    clock["t"] += 0.6
    strategy._exit_spread(spread_id)
    assert len(calls) == 6
    assert strategy._exit_failure_count_by_spread[spread_id] == 2
    assert strategy._exit_circuit_open_until_mono_by_spread[spread_id] > clock["t"]
    clock["t"] += 0.1
    strategy._exit_spread(spread_id)
    assert len(calls) == 6


def test_nifty_spread_entry_carries_strategy_context_and_syncs_risk_manager(monkeypatch):
    risk_manager = DummyRiskManager()
    mod, strategy = _make_strategy(monkeypatch, risk_manager=risk_manager)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    short_label = "NIFTY_PE_19800"
    long_label = "NIFTY_PE_19700"
    strategy.last_price[short_label] = 100.0
    strategy.last_price[long_label] = 80.0

    ok = strategy._enter_spread(
        spread_type="PUT_SPREAD",
        short_label=short_label,
        long_label=long_label,
        credit=20.0,
        width_pts=100.0,
        expiry=date(2026, 2, 26),
        extra=None,
    )

    assert ok is True
    assert len(calls) == 2
    short_ctx = calls[0].strategy_context
    long_ctx = calls[1].strategy_context
    assert calls[0].position_label == short_label
    assert short_ctx["spread_leg_role"] == "short"
    assert long_ctx["spread_leg_role"] == "long"
    assert short_ctx["spread_id"] == long_ctx["spread_id"]
    assert short_ctx["managed_exit_mode"] == "strategy"
    assert short_ctx["broker_brackets_enabled"] is False
    assert risk_manager.open_positions[short_label]["strategy_context"]["spread_id"] == short_ctx["spread_id"]
    assert risk_manager.open_positions[long_label]["strategy_context"]["spread_id"] == short_ctx["spread_id"]


def test_nifty_spread_restore_reconciles_qty_per_leg_on_exit(monkeypatch):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_PE_19800": {
                "side": "SELL",
                "qty": 3,
                "entry_price": 100.0,
                "template_name": "nifty_weekly_credit_spreads",
                "strategy_name": "nifty-weekly-credit-spreads",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "801",
                "tradingsymbol": "NIFTY26FEB19800PE",
                "strategy_context": {
                    "spread_id": "spread-1",
                    "spread_type": "PUT_SPREAD",
                    "spread_leg_role": "short",
                    "entry_credit": 20.0,
                    "width_pts": 100.0,
                    "max_loss_rupees": 240.0,
                    "entry_time": entry_ts,
                    "expiry": "2026-02-26",
                    "short_leg": {
                        "label": "NIFTY_PE_19800",
                        "side": "SELL",
                        "strike": 19800.0,
                        "qty": 1,
                        "entry_price": 100.0,
                        "broker_symbol": "NIFTY26FEB19800PE",
                        "exchange": "NFO",
                        "symbol_token": "801",
                    },
                    "long_leg": {
                        "label": "NIFTY_PE_19700",
                        "side": "BUY",
                        "strike": 19700.0,
                        "qty": 2,
                        "entry_price": 80.0,
                        "broker_symbol": "NIFTY26FEB19700PE",
                        "exchange": "NFO",
                        "symbol_token": "802",
                    },
                    "short_exit_done": False,
                    "long_exit_done": False,
                },
            },
            "NIFTY_PE_19700": {
                "side": "BUY",
                "qty": 2,
                "entry_price": 80.0,
                "template_name": "nifty_weekly_credit_spreads",
                "strategy_name": "nifty-weekly-credit-spreads",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "802",
                "tradingsymbol": "NIFTY26FEB19700PE",
                "strategy_context": {
                    "spread_id": "spread-1",
                    "spread_type": "PUT_SPREAD",
                    "spread_leg_role": "long",
                    "entry_credit": 20.0,
                    "width_pts": 100.0,
                    "max_loss_rupees": 240.0,
                    "entry_time": entry_ts,
                    "expiry": "2026-02-26",
                    "short_leg": {
                        "label": "NIFTY_PE_19800",
                        "side": "SELL",
                        "strike": 19800.0,
                        "qty": 1,
                        "entry_price": 100.0,
                        "broker_symbol": "NIFTY26FEB19800PE",
                        "exchange": "NFO",
                        "symbol_token": "801",
                    },
                    "long_leg": {
                        "label": "NIFTY_PE_19700",
                        "side": "BUY",
                        "strike": 19700.0,
                        "qty": 1,
                        "entry_price": 80.0,
                        "broker_symbol": "NIFTY26FEB19700PE",
                        "exchange": "NFO",
                        "symbol_token": "802",
                    },
                    "short_exit_done": False,
                    "long_exit_done": False,
                },
            },
        }
    )
    mod, strategy = _make_strategy(monkeypatch, risk_manager=risk_manager)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)

    spread = strategy.open_spreads["spread-1"]
    assert spread.short_leg.qty == 3
    assert spread.long_leg.qty == 2

    strategy._exit_spread("spread-1")

    assert len(calls) == 2
    assert calls[0].quantity == 3
    assert calls[1].quantity == 2


def test_nifty_spread_restore_partial_spread_keeps_force_exit_for_remaining_leg(monkeypatch):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_PE_19800": {
                "side": "SELL",
                "qty": 3,
                "entry_price": 100.0,
                "template_name": "nifty_weekly_credit_spreads",
                "strategy_name": "nifty-weekly-credit-spreads",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "801",
                "tradingsymbol": "NIFTY26FEB19800PE",
                "strategy_context": {
                    "spread_id": "spread-partial",
                    "spread_type": "PUT_SPREAD",
                    "spread_leg_role": "short",
                    "entry_credit": 20.0,
                    "width_pts": 100.0,
                    "max_loss_rupees": 240.0,
                    "entry_time": entry_ts,
                    "expiry": "2026-02-26",
                    "short_leg": {
                        "label": "NIFTY_PE_19800",
                        "side": "SELL",
                        "strike": 19800.0,
                        "qty": 3,
                        "entry_price": 100.0,
                        "broker_symbol": "NIFTY26FEB19800PE",
                        "exchange": "NFO",
                        "symbol_token": "801",
                    },
                    "long_leg": {
                        "label": "NIFTY_PE_19700",
                        "side": "BUY",
                        "strike": 19700.0,
                        "qty": 1,
                        "entry_price": 80.0,
                        "broker_symbol": "NIFTY26FEB19700PE",
                        "exchange": "NFO",
                        "symbol_token": "802",
                    },
                    "short_exit_done": False,
                    "long_exit_done": False,
                },
            },
        }
    )
    mod, strategy = _make_strategy(monkeypatch, risk_manager=risk_manager)
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)

    spread = strategy.open_spreads["spread-partial"]
    assert spread.short_exit_done is False
    assert spread.long_exit_done is True

    strategy.force_exit_all()

    assert len(calls) == 1
    assert calls[0].position_label == "NIFTY_PE_19800"
    assert calls[0].quantity == 3
