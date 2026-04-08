import types
from datetime import datetime, timedelta, timezone

from app.brokers.base import OrderResponse
from app.strategies.exclusive_nifty_ce_buy import ExclusiveNiftyCeBuyStrategy


class DummyOrderClient:
    pass


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


def _make_strategy(meta_override=None, params=None, risk_manager=None):
    meta = meta_override or {}
    # Minimal underlying entry for completeness
    meta.setdefault("NIFTY_IDX", {"kind": "UNDERLYING", "underlying": "NIFTY"})
    return ExclusiveNiftyCeBuyStrategy(
        instrument_meta=meta,
        order_client=DummyOrderClient(),
        risk_manager=risk_manager or DummyRiskManager(),
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params=params or {},
    )


def test_defaults_are_set_for_30s_and_atm_only():
    strat = _make_strategy(params={"preseed_history": False})
    assert strat.timeframe_seconds == 30
    assert strat.moneyness == "ATM"
    assert strat.itm_strike_offset == 0


def test_select_call_option_picks_nearest_atm():
    meta = {
        "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        "NIFTY_CE_20000": {"underlying": "NIFTY", "kind": "CE", "lot_size": 65},
        "NIFTY_CE_20100": {"underlying": "NIFTY", "kind": "CE", "lot_size": 65},
        "NIFTY_CE_19900": {"underlying": "NIFTY", "kind": "CE", "lot_size": 65},
    }
    strat = _make_strategy(meta_override=meta, params={"preseed_history": False})
    chosen = strat._select_call_option(spot=20020.0)
    assert chosen == "NIFTY_CE_20000"


def test_preseed_can_be_disabled_to_avoid_disk_deps():
    strat = _make_strategy(params={"preseed_history": False})
    assert len(strat.buffers.closes) == 0
    assert strat.state.vol_threshold is None


def test_parse_expiry_normalizes_date_like_values():
    strat = _make_strategy(params={"preseed_history": False})
    dt = datetime(2024, 1, 4, 10, 0, tzinfo=timezone.utc)
    assert strat._parse_expiry(dt) == dt.date()
    assert strat._parse_expiry(dt.date()) == dt.date()
    raw = types.SimpleNamespace(to_pydatetime=lambda: dt)
    assert strat._parse_expiry(raw) == dt.date()


def test_select_call_option_skips_missing_expiry_when_current_expiry_set(monkeypatch):
    fixed_now = datetime(2024, 1, 3, 9, 15, tzinfo=timezone.utc)
    expiry = (fixed_now + timedelta(days=1)).date()
    meta = {
        "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        "NIFTY_CE_20000": {"underlying": "NIFTY", "kind": "CE", "lot_size": 50},
        "NIFTY_CE_21000": {
            "underlying": "NIFTY",
            "kind": "CE",
            "lot_size": 50,
            "expiry": expiry,
        },
    }
    strat = _make_strategy(meta_override=meta, params={"preseed_history": False})
    monkeypatch.setattr(strat, "_now_ist", lambda: fixed_now)
    chosen = strat._select_call_option(spot=20005.0)
    assert chosen == "NIFTY_CE_21000"


def test_order_tags_are_compact_and_within_broker_limit():
    strat = _make_strategy(params={"preseed_history": False})
    entry_tag = strat._build_order_tag(is_entry=True)
    exit_tag = strat._build_order_tag(
        is_entry=False,
        reason="EXTRA_LONG_TRAIL_EMA20_REASON_CODE",
    )

    assert entry_tag == "XNCB_ENTRY"
    assert exit_tag.startswith("XNCB_EXIT_")
    assert len(entry_tag) <= 20
    assert len(exit_tag) <= 20


def test_exclusive_ce_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
    option_label = "NIFTY_CE_20000"
    meta = {
        "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        option_label: {
            "symbol": "NIFTY26FEB20000CE",
            "underlying": "NIFTY",
            "kind": "CE",
            "lot_size": 50,
            "exchange": "NFO",
            "token": "401",
        },
    }
    strat = _make_strategy(meta_override=meta, params={"preseed_history": False})
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(
        "app.strategies.exclusive_nifty_ce_buy.place_order_via_bridge",
        fake_place_order_via_bridge,
    )

    strat.last_price[option_label] = 100.0
    strat.state.pending_entry_atr = 1.0
    strat._enter_position(
        underlying_price=20005.0,
        ts=datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc),
    )
    assert strat.state.position is not None
    assert len(calls) == 1
    entry_symbol = calls[0].symbol
    assert entry_symbol != option_label

    strat.instrument_meta[option_label] = {
        "underlying": "NIFTY",
        "kind": "CE",
        "exchange": "NFO",
        "token": "401",
    }
    strat._exit_position(reason="EOD_SQUAREOFF_1515")

    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_exclusive_ce_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_EXCLUSIVE_CE_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_EXCLUSIVE_CE_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_EXCLUSIVE_CE_EXIT_CIRCUIT_OPEN_SECONDS", "30")
    option_label = "NIFTY_CE_20000"
    meta = {
        "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        option_label: {
            "symbol": "NIFTY26FEB20000CE",
            "underlying": "NIFTY",
            "kind": "CE",
            "lot_size": 50,
            "exchange": "NFO",
            "token": "401",
        },
    }
    strat = _make_strategy(meta_override=meta, params={"preseed_history": False})
    mod = __import__("app.strategies.exclusive_nifty_ce_buy", fromlist=["monotonic"])
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

    monkeypatch.setattr(
        "app.strategies.exclusive_nifty_ce_buy.place_order_via_bridge",
        fake_place_order_via_bridge,
    )

    strat.last_price[option_label] = 100.0
    strat.state.pending_entry_atr = 1.0
    strat._enter_position(
        underlying_price=20005.0,
        ts=datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc),
    )
    assert strat.state.position is not None
    assert len(calls) == 1

    strat._exit_position(reason="EOD_SQUAREOFF_1515")
    assert len(calls) == 2
    clock["t"] += 0.6
    strat._exit_position(reason="EOD_SQUAREOFF_1515")
    assert len(calls) == 3
    assert strat._exit_failure_count == 2
    assert strat._exit_circuit_open_until_mono > clock["t"]
    clock["t"] += 0.1
    strat._exit_position(reason="EOD_SQUAREOFF_1515")
    assert len(calls) == 3


def test_exclusive_dynamic_policy_no_trade_blocks_entries(monkeypatch):
    strat = _make_strategy(
        params={
            "preseed_history": False,
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "xncb_v1",
            },
        }
    )
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(seconds=strat.timeframe_seconds),
        h=100.0,
        low=99.0,
        c=99.5,
    )
    strat.on_bar(
        "NIFTY_IDX",
        strat.timeframe_seconds,
        candle,
        {},
    )
    assert strat._adaptive_policy.enabled is True
    assert strat._adaptive_policy.get_bool("disable_entries", False) is True
    assert strat.state.pending_entry_at is None
    assert strat.state.position is None


def test_exclusive_emits_gate_decision_for_rejected_signal(monkeypatch):
    strat = _make_strategy(params={"preseed_history": False})
    gate_decisions = []
    strat.set_debug_gate_listener(gate_decisions.append)
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(seconds=strat.timeframe_seconds),
        h=100.0,
        low=99.0,
        c=99.5,
    )

    strat.on_bar(
        "NIFTY_IDX",
        strat.timeframe_seconds,
        candle,
        {},
    )

    assert any(decision["reason"] == "missing_indicator" for decision in gate_decisions)
    assert any(decision["gate"] == "indicator" for decision in gate_decisions)


def test_exclusive_readiness_breakdown_emits_specific_missing_prerequisites(monkeypatch):
    strat = _make_strategy(params={"preseed_history": False})
    gate_decisions = []
    strat.set_debug_gate_listener(gate_decisions.append)
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(seconds=strat.timeframe_seconds),
        h=100.0,
        low=99.0,
        c=99.5,
    )

    strat.on_bar(
        "NIFTY_IDX",
        strat.timeframe_seconds,
        candle,
        {"atr": 1.0},
    )

    reasons = {decision["reason"] for decision in gate_decisions}
    assert "missing_rsi" in reasons
    assert "missing_macd" in reasons
    assert "missing_ret_5_history" in reasons
    assert "missing_vol_threshold" in reasons
    assert "signal_not_ready" not in reasons


def test_exclusive_dynamic_policy_qty_multiplier_override(monkeypatch):
    strat = _make_strategy(
        params={
            "preseed_history": False,
            "lots_per_trade": 2,
            "dynamic_policy": {
                "enabled": True,
                "policy_id": "xncb_v1",
                "profiles": {
                    "NO_TRADE": {"disable_entries": False, "qty_mult": 1.5},
                },
            },
        }
    )
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(seconds=strat.timeframe_seconds),
        h=100.0,
        low=99.0,
        c=99.5,
    )
    strat.on_bar(
        "NIFTY_IDX",
        strat.timeframe_seconds,
        candle,
        {},
    )
    assert strat._qty_for_label("NIFTY_CE_20000") == 3


def test_exclusive_rehydrates_position_and_preserves_ema_fail_count(monkeypatch):
    option_label = "NIFTY_CE_20000"
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            option_label: {
                "side": "BUY",
                "qty": 1,
                "entry_price": 100.0,
                "template_name": "exclusive-nifty-ce-buy",
                "strategy_name": "exclusive-nifty-ce-buy",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "401",
                "tradingsymbol": "NIFTY26FEB20000CE",
                "strategy_context": {
                    "entry_underlying_price": 20005.0,
                    "entry_option_price": 100.0,
                    "entry_atr": 1.0,
                    "sl_price": 97.8,
                    "target_price": 102.5,
                    "position_qty": 1,
                    "ema_fail_count": 2,
                },
            }
        }
    )
    strat = _make_strategy(
        meta_override={
            "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
            option_label: {
                "symbol": "NIFTY26FEB20000CE",
                "underlying": "NIFTY",
                "kind": "CE",
                "lot_size": 50,
                "exchange": "NFO",
                "token": "401",
            },
        },
        params={"preseed_history": False},
        risk_manager=risk_manager,
    )
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs["order_req"])
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="ACCEPTED",
            message="ok",
        )

    monkeypatch.setattr(
        "app.strategies.exclusive_nifty_ce_buy.place_order_via_bridge",
        fake_place_order_via_bridge,
    )

    strat.buffers.closes.extend([20004.0] * 20)
    assert strat.state.position is not None
    assert strat.state.ema_fail_count == 2

    ts = datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc)
    candle = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(seconds=strat.timeframe_seconds),
        h=20005.0,
        low=20003.4,
        c=20003.5,
    )
    strat.on_bar("NIFTY_IDX", strat.timeframe_seconds, candle, {})

    assert strat.state.position is None
    assert len(calls) == 1
    assert calls[0].tag == "XNCB_EXIT_EMAFAIL"
