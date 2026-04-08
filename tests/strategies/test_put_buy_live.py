import importlib
import types
from datetime import datetime, timezone

from app.brokers.base import OrderResponse


MODULE_PATH = "app.strategies.put_buy_live"


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
            use_hub_router_for_banknifty_options=False,
            use_hub_router_for_finnifty_options=False,
            use_hub_router_for_sensex_options=False,
            use_hub_router_for_midcpnifty_options=False,
        ),
    )
    strategy = mod.PutBuyLiveStrategy(
        instrument_meta={
            "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
            "NIFTY_ATM_PE_20000": {
                "symbol": "NIFTY26FEB20000PE",
                "underlying": "NIFTY",
                "kind": "PE",
                "lot_size": 50,
                "exchange": "NFO",
                "token": "701",
                "expiry": "2026-02-26",
            },
        },
        order_client=None,
        risk_manager=risk_manager,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "lots_per_trade": 1,
            "entry_delay_bars": 1,
            "strict_rsi_cross": False,
            "use_atr_filter": False,
        },
    )
    return mod, strategy


def test_put_buy_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
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
    label = "NIFTY_ATM_PE_20000"
    strategy.last_price[label] = 120.0
    strategy._enter_position(
        entry_underlying_price=20000.0,
        ts=datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc),
        pullback_high=20050.0,
        atr=20.0,
    )

    assert strategy.position is not None
    assert len(calls) == 1
    entry_symbol = calls[0].symbol
    assert entry_symbol != label

    strategy.instrument_meta[label] = {
        "underlying": "NIFTY",
        "kind": "PE",
        "lot_size": 50,
        "exchange": "NFO",
        "token": "701",
        "expiry": "2026-02-26",
    }
    strategy._exit_position(reason="EOD", exit_underlying_price=19900.0)

    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_put_buy_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_PUT_BUY_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_PUT_BUY_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_PUT_BUY_EXIT_CIRCUIT_OPEN_SECONDS", "30")
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
    label = "NIFTY_ATM_PE_20000"
    strategy.last_price[label] = 120.0
    strategy._enter_position(
        entry_underlying_price=20000.0,
        ts=datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc),
        pullback_high=20050.0,
        atr=20.0,
    )
    assert strategy.position is not None
    assert len(calls) == 1

    strategy._exit_position(reason="EOD", exit_underlying_price=19900.0)
    assert len(calls) == 2
    clock["t"] += 0.6
    strategy._exit_position(reason="EOD", exit_underlying_price=19900.0)
    assert len(calls) == 3
    assert strategy._exit_failure_count == 2
    assert strategy._exit_circuit_open_until_mono > clock["t"]
    clock["t"] += 0.1
    strategy._exit_position(reason="EOD", exit_underlying_price=19900.0)
    assert len(calls) == 3


def test_put_buy_rehydrates_trailing_state_after_restart(monkeypatch):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_ATM_PE_20000": {
                "side": "BUY",
                "qty": 1,
                "entry_price": 120.0,
                "template_name": "put-buy",
                "strategy_name": "put-buy",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "701",
                "tradingsymbol": "NIFTY26FEB20000PE",
                "strategy_context": {
                    "entry_underlying_price": 20000.0,
                    "entry_option_price": 120.0,
                    "initial_stop": 20050.0,
                    "stop_price": 20000.0,
                    "R": 50.0,
                    "target1": 19950.0,
                    "target2": 19900.0,
                    "position_qty": 2,
                    "remaining_qty": 1,
                    "tp1_done": True,
                    "trail_active": True,
                    "realized_R": 0.5,
                    "prev_high": 19960.0,
                },
            }
        }
    )
    _mod, strategy = _make_strategy(monkeypatch, risk_manager=risk_manager)

    assert strategy.position is not None
    assert strategy.position.remaining_qty == 1
    assert strategy.position.tp1_done is True
    assert strategy.position.trail_active is True
    assert strategy.prev_high == 19960.0

    strategy._process_exits(
        candle=types.SimpleNamespace(h=19980.0, low=19970.0),
        ema9=19950.0,
    )

    assert strategy.position is not None
    assert strategy.position.stop_price == 19960.0
