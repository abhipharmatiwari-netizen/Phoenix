import importlib
import types
from datetime import datetime, timedelta, timezone
import pytest

from app.brokers.base import OrderResponse
from app.observability.prometheus_metrics import render_metrics, reset_metrics

MODULE_PATH = "app.strategies.put_momentum_scalper"
CALLABLE_NAMES = ['PutMomentumScalperConfig', 'OptionPosition', 'InstrumentState', 'PutMomentumScalperStrategy']


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


def _make_strategy(monkeypatch, meta_override=None, *, risk_manager=None):
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

    instrument_meta = meta_override or {
        "NIFTY_ATM_PE_20000": {
            "symbol": "NIFTY26FEB20000PE",
            "token": "111",
            "exchange": "NFO",
            "lot_size": 50,
            "underlying": "NIFTY",
            "kind": "PE",
        }
    }

    strategy = mod.PutMomentumScalperStrategy(
        instrument_meta,
        order_client=None,
        risk_manager=risk_manager,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params={
            "rsi_min": 20.0,
            "rsi_max": 80.0,
            "min_atr_ratio": 0.0,
            "rsi_falling_bars_required": 2,
            "lookback_breakdown_bars": 3,
            "max_bars_in_trade": 6,
            "timeframe_seconds_5m": 300,
            "timeframe_seconds_15m": 900,
            "lots_per_trade": 1,
            "morning_start": "09:00",
            "morning_end": "11:30",
        },
    )
    return mod, strategy


def _make_candle(ts: datetime, *, h: float, low: float, c: float):
    return types.SimpleNamespace(start_ts=ts, end_ts=ts + timedelta(minutes=5), h=h, low=low, c=c)


def _seed_downtrend(strategy, ts: datetime):
    candle_15m = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(minutes=15),
        h=100.0,
        low=90.0,
        c=90.0,
    )
    strategy.on_bar("NIFTY_IDX", 900, candle_15m, {"ema_20": 100.0})


def test_put_momentum_enters_and_exits(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append(
            {
                "label": label,
                "side": side,
                "qty": qty,
                "price": price,
                "tag": tag,
                "purpose": purpose,
            }
        )
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    base_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(strategy, "_now_ist", lambda: (base_ts + timedelta(minutes=1)).astimezone(mod.IST))

    # Seed last price for option
    strategy.on_tick("NIFTY_ATM_PE_20000", 100.0)

    _seed_downtrend(strategy, base_ts)

    # Build 5m bars to satisfy entry conditions
    bars = [
        (base_ts + timedelta(minutes=5), 120.0, 110.0, 120.0, 50.0, 0.3),
        (base_ts + timedelta(minutes=10), 118.0, 105.0, 118.0, 40.0, 0.1),
        (base_ts + timedelta(minutes=15), 95.0, 80.0, 82.0, 30.0, -0.7),
    ]

    for ts, h, low, c, rsi, macd_hist in bars:
        candle = _make_candle(ts, h=h, low=low, c=c)
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            candle,
            {
                "ema_20": 110.0,
                "ema_50": 115.0,
                "rsi": rsi,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": macd_hist,
                "atr": 1.0,
            },
        )

    assert any(call["side"] == "BUY" for call in calls)

    # Trigger exit via TP
    strategy.on_tick("NIFTY_ATM_PE_20000", 150.0)
    assert any(call["side"] == "SELL" for call in calls)


def test_put_momentum_no_entry_outside_window(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append(side)
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    late_ts = datetime(2025, 1, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(strategy, "_now_ist", lambda: late_ts.astimezone(mod.IST))

    candle = _make_candle(late_ts, h=95.0, low=80.0, c=82.0)
    strategy.on_bar(
        "NIFTY_IDX",
        300,
        candle,
        {"ema_20": 110.0, "ema_50": 115.0, "rsi": 30.0, "macd": -1.0, "macd_signal": 0.0, "macd_hist": -0.7, "atr": 1.0},
    )

    assert calls == []


def test_put_momentum_continuation_window_allows_midday_entry(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    strategy.config.entry_start = "09:20"
    strategy.config.entry_end = "14:45"
    strategy.config.morning_end = "11:00"
    strategy.config.afternoon_start = "13:30"
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append({"side": side, "tag": tag})
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    base_ts = datetime(2025, 1, 1, 6, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(strategy, "_now_ist", lambda: base_ts.astimezone(mod.IST))
    _seed_downtrend(strategy, base_ts - timedelta(minutes=15))

    bars = [
        (base_ts, 120.0, 110.0, 119.0, 38.0, 0.4),
        (base_ts + timedelta(minutes=5), 116.0, 104.0, 110.0, 35.0, 0.1),
        (base_ts + timedelta(minutes=10), 95.0, 80.0, 82.0, 30.0, -0.4),
    ]
    for ts, h, low, c, rsi, macd_hist in bars:
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            _make_candle(ts, h=h, low=low, c=c),
            {
                "ema_20": 110.0,
                "ema_50": 115.0,
                "rsi": rsi,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": macd_hist,
                "atr": 1.0,
            },
        )

    assert any(call["side"] == "BUY" for call in calls)


def test_put_momentum_rsi_falling_bars_required_one_allows_single_decline(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    strategy.config.rsi_falling_bars_required = 1
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    _seed_downtrend(strategy, datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc))
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append(side)
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    bars = [
        (datetime(2025, 1, 1, 4, 5, tzinfo=timezone.utc), 120.0, 110.0, 120.0, 39.0, 0.3),
        (datetime(2025, 1, 1, 4, 10, tzinfo=timezone.utc), 118.0, 105.0, 118.0, 42.0, 0.1),
        (datetime(2025, 1, 1, 4, 15, tzinfo=timezone.utc), 95.0, 80.0, 82.0, 38.0, -0.6),
    ]
    for ts, h, low, c, rsi, macd_hist in bars:
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            _make_candle(ts, h=h, low=low, c=c),
            {
                "ema_20": 110.0,
                "ema_50": 115.0,
                "rsi": rsi,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": macd_hist,
                "atr": 1.0,
            },
        )

    assert "BUY" in calls


def test_put_momentum_rejects_stale_negative_macd(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    _seed_downtrend(strategy, datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc))
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    gate_decisions = []
    strategy.set_debug_gate_listener(gate_decisions.append)
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append(side)
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    bars = [
        (datetime(2025, 1, 1, 4, 5, tzinfo=timezone.utc), 120.0, 110.0, 120.0, 50.0, -0.2),
        (datetime(2025, 1, 1, 4, 10, tzinfo=timezone.utc), 118.0, 105.0, 118.0, 40.0, -0.3),
        (datetime(2025, 1, 1, 4, 15, tzinfo=timezone.utc), 95.0, 80.0, 82.0, 30.0, -0.4),
    ]
    for ts, h, low, c, rsi, macd_hist in bars:
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            _make_candle(ts, h=h, low=low, c=c),
            {
                "ema_20": 110.0,
                "ema_50": 115.0,
                "rsi": rsi,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": macd_hist,
                "atr": 1.0,
            },
        )

    assert calls == []
    assert any(decision["reason"] == "macd_no_fresh_negative_cross" for decision in gate_decisions)


def test_put_momentum_logs_missing_indicator_and_records_metric(monkeypatch, caplog):
    _mod, strategy = _make_strategy(monkeypatch)
    reset_metrics()
    _seed_downtrend(strategy, datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc))

    with caplog.at_level("INFO"):
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            _make_candle(datetime(2025, 1, 1, 4, 5, tzinfo=timezone.utc), h=95.0, low=80.0, c=82.0),
            {
                "ema_20": 110.0,
                "rsi": 30.0,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": -0.5,
                "atr": 1.0,
            },
        )

    assert "event_type=STRATEGY_GATE_SKIP" in caplog.text
    assert "reason=missing_indicator" in caplog.text
    assert "indicator=ema_50" in caplog.text
    metrics = render_metrics()
    assert 'phoenix_strategy_gate_decisions_total' in metrics
    assert 'reason="missing_indicator"' in metrics


def test_put_momentum_rejects_reversal_like_high_rsi(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    _seed_downtrend(strategy, datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc))
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    gate_decisions = []
    strategy.set_debug_gate_listener(gate_decisions.append)
    strategy.config.rsi_max = 40.0
    calls = []

    def fake_place_order(
        *,
        label,
        side,
        qty,
        price,
        tag,
        purpose=None,
        strategy_context=None,
    ):
        calls.append(side)
        return OrderResponse(
            broker_order_id="order-1",
            status="ACCEPTED",
            message="ok",
            filled_quantity=qty,
            average_price=price,
        )

    monkeypatch.setattr(strategy, "_place_order", fake_place_order)
    bars = [
        (datetime(2025, 1, 1, 4, 5, tzinfo=timezone.utc), 120.0, 110.0, 120.0, 60.0, 0.5),
        (datetime(2025, 1, 1, 4, 10, tzinfo=timezone.utc), 118.0, 105.0, 118.0, 58.0, 0.2),
        (datetime(2025, 1, 1, 4, 15, tzinfo=timezone.utc), 95.0, 80.0, 82.0, 55.0, -0.6),
    ]
    for ts, h, low, c, rsi, macd_hist in bars:
        strategy.on_bar(
            "NIFTY_IDX",
            300,
            _make_candle(ts, h=h, low=low, c=c),
            {
                "ema_20": 110.0,
                "ema_50": 115.0,
                "rsi": rsi,
                "macd": -1.0,
                "macd_signal": 0.0,
                "macd_hist": macd_hist,
                "atr": 1.0,
            },
        )

    assert calls == []
    assert any(decision["reason"] == "rsi_out_of_range" for decision in gate_decisions)


def test_put_momentum_time_stop_respects_longer_continuation_horizon(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    exits = []

    monkeypatch.setattr(strategy, "_exit_position", lambda reason: exits.append(reason))
    strategy.state.position = types.SimpleNamespace(entry_bar_index=0)
    strategy.state.bars_5m = [{"h": 1.0, "l": 1.0, "c": 1.0} for _ in range(13)]

    strategy._maybe_time_stop(max_bars_in_trade=14)
    assert exits == []

    strategy.state.bars_5m.append({"h": 1.0, "l": 1.0, "c": 1.0})
    strategy._maybe_time_stop(max_bars_in_trade=14)
    assert exits == ["TIME_STOP"]


def test_put_momentum_trend_tolerance_allows_near_ema15m(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle_15m = types.SimpleNamespace(
        start_ts=ts,
        end_ts=ts + timedelta(minutes=15),
        h=101.0,
        low=99.0,
        c=100.05,
    )

    strategy.config.trend_ema_tolerance_ratio = 0.001
    strategy.on_bar("NIFTY_IDX", 900, candle_15m, {"ema_20": 100.0})
    assert strategy.state.trend_15m_down is True

    strategy.config.trend_ema_tolerance_ratio = 0.0
    strategy.on_bar("NIFTY_IDX", 900, candle_15m, {"ema_20": 100.0})
    assert strategy.state.trend_15m_down is False


def test_parse_expiry_normalizes_date_like_values(monkeypatch):
    mod, strategy = _make_strategy(monkeypatch)
    dt = datetime(2024, 1, 4, 10, 0, tzinfo=timezone.utc)
    raw = types.SimpleNamespace(to_pydatetime=lambda: dt)
    assert strategy._parse_expiry(dt) == dt.date()
    assert strategy._parse_expiry(dt.date()) == dt.date()
    assert strategy._parse_expiry(raw) == dt.date()


def test_select_put_option_skips_missing_expiry_when_current_expiry_set(monkeypatch):
    fixed_now = datetime(2024, 1, 3, 9, 0, tzinfo=timezone.utc)
    expiry = (fixed_now + timedelta(days=1)).date()
    instrument_meta = {
        "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        "NIFTY_ATM_PE_20000": {
            "symbol": "NIFTY_ATM_PE_20000",
            "token": "111",
            "exchange": "NFO",
            "lot_size": 50,
            "underlying": "NIFTY",
            "kind": "PE",
        },
        "NIFTY_ATM_PE_21000": {
            "symbol": "NIFTY_ATM_PE_21000",
            "token": "222",
            "exchange": "NFO",
            "lot_size": 50,
            "underlying": "NIFTY",
            "kind": "PE",
            "expiry": expiry,
        },
    }
    mod, strategy = _make_strategy(monkeypatch, meta_override=instrument_meta)
    monkeypatch.setattr(strategy, "_now_ist", lambda: fixed_now.astimezone(mod.IST))
    chosen = strategy._select_put_option(spot=20005.0)
    assert chosen == "NIFTY_ATM_PE_21000"


def test_put_momentum_exit_uses_entry_broker_symbol_when_meta_symbol_missing(monkeypatch):
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
    option_label = "NIFTY_ATM_PE_20000"
    strategy.last_price[option_label] = 100.0
    strategy._enter_position(
        types.SimpleNamespace(h=100.0, c=90.0),
        ema20=100.0,
        vwap=95.0,
    )
    assert strategy.state.position is not None
    assert len(calls) == 1
    entry_symbol = calls[0].symbol
    assert entry_symbol != option_label

    strategy.instrument_meta[option_label] = {
        "token": "111",
        "exchange": "NFO",
        "underlying": "NIFTY",
        "kind": "PE",
    }
    strategy._exit_position("EOD")

    assert len(calls) == 2
    assert calls[1].symbol == entry_symbol


def test_put_momentum_exit_circuit_breaker_caps_retry_attempts(monkeypatch):
    monkeypatch.setenv("NIFTY_PUT_MOM_EXIT_RETRY_COOLDOWN_SECONDS", "0.5")
    monkeypatch.setenv("NIFTY_PUT_MOM_EXIT_MAX_RETRIES", "2")
    monkeypatch.setenv("NIFTY_PUT_MOM_EXIT_CIRCUIT_OPEN_SECONDS", "30")
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
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    strategy._enter_position(
        types.SimpleNamespace(h=100.0, c=90.0),
        ema20=100.0,
        vwap=95.0,
    )
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


def test_put_momentum_no_position_exit_rejection_retires_local_position(monkeypatch, caplog):
    mod, strategy = _make_strategy(monkeypatch)
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
            broker_order_id="",
            status="REJECTED",
            message="exit_order_missing_position_evidence",
            filled_quantity=0,
            average_price=None,
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    strategy.last_price["NIFTY_ATM_PE_20000"] = 100.0
    strategy._enter_position(
        types.SimpleNamespace(h=100.0, c=90.0),
        ema20=100.0,
        vwap=95.0,
    )
    assert strategy.state.position is not None

    with caplog.at_level("WARNING"):
        strategy._exit_position("TIME_STOP")
    assert strategy.state.position is None
    assert strategy._exit_failure_count == 0
    assert "Retiring stale PUT_MOM position after no-position exit rejection" in caplog.text

    strategy._exit_position("TIME_STOP")
    assert len(calls) == 2


def test_put_momentum_dynamic_policy_no_trade_blocks_entries(monkeypatch, caplog):
    _mod, strategy = _make_strategy(monkeypatch)
    strategy._adaptive_policy = strategy._adaptive_policy.__class__(
        strategy_name="put_momentum_scalper",
        env_prefix=strategy.env_prefix,
        cfg=strategy._cfg,
        dynamic_policy_raw={"enabled": True, "policy_id": "put_mom_v1"},
        base_params=strategy._base_policy_params(),
        extra_rules={
            "min_atr_ratio": strategy._adaptive_policy._extra_rules["min_atr_ratio"],  # type: ignore[attr-defined]
            "rsi_min": strategy._adaptive_policy._extra_rules["rsi_min"],  # type: ignore[attr-defined]
            "rsi_max": strategy._adaptive_policy._extra_rules["rsi_max"],  # type: ignore[attr-defined]
            "lookback_breakdown_bars": strategy._adaptive_policy._extra_rules["lookback_breakdown_bars"],  # type: ignore[attr-defined]
            "max_bars_in_trade": strategy._adaptive_policy._extra_rules["max_bars_in_trade"],  # type: ignore[attr-defined]
            "trend_ema_tolerance_ratio": strategy._adaptive_policy._extra_rules["trend_ema_tolerance_ratio"],  # type: ignore[attr-defined]
        },
        extra_entry_freeze_keys={
            "min_atr_ratio",
            "rsi_min",
            "rsi_max",
            "lookback_breakdown_bars",
        },
        context_builder_kwargs={"ema_period": 20, "min_median_samples": 10},
        dynamic_enabled_env_key="PUT_MOM_DYNAMIC_POLICY_ENABLED",
        dynamic_policy_id_env_key="PUT_MOM_DYNAMIC_POLICY_ID",
    )
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = _make_candle(ts, h=100.0, low=98.0, c=99.0)
    with caplog.at_level("INFO"):
        strategy.on_bar("NIFTY_IDX", 300, candle, {})
    assert strategy._adaptive_policy.enabled is True
    assert strategy._adaptive_policy.get_bool("disable_entries", False) is True
    assert strategy._adaptive_policy.selected_profile() == {"disable_entries": True}
    assert strategy._adaptive_policy.last_context is not None
    assert strategy.state.position is None
    assert "reason=policy_disable_entries" in caplog.text
    assert "regime=NO_TRADE" in caplog.text
    assert "disable_entries=True" in caplog.text
    assert "atr_norm=None" in caplog.text
    assert "adx14=None" in caplog.text
    assert "di_spread=None" in caplog.text
    assert "ema_slope=None" in caplog.text
    assert "selected_profile={'disable_entries': True}" in caplog.text


def test_put_momentum_dynamic_policy_qty_multiplier_override(monkeypatch):
    _mod, strategy = _make_strategy(monkeypatch)
    strategy.lots_per_trade = 2
    strategy._adaptive_policy = strategy._adaptive_policy.__class__(
        strategy_name="put_momentum_scalper",
        env_prefix=strategy.env_prefix,
        cfg=strategy._cfg,
        dynamic_policy_raw={
            "enabled": True,
            "policy_id": "put_mom_v1",
            "profiles": {"NO_TRADE": {"disable_entries": False, "qty_mult": 1.5}},
        },
        base_params=strategy._base_policy_params(),
        extra_rules={
            "min_atr_ratio": strategy._adaptive_policy._extra_rules["min_atr_ratio"],  # type: ignore[attr-defined]
            "rsi_min": strategy._adaptive_policy._extra_rules["rsi_min"],  # type: ignore[attr-defined]
            "rsi_max": strategy._adaptive_policy._extra_rules["rsi_max"],  # type: ignore[attr-defined]
            "lookback_breakdown_bars": strategy._adaptive_policy._extra_rules["lookback_breakdown_bars"],  # type: ignore[attr-defined]
            "max_bars_in_trade": strategy._adaptive_policy._extra_rules["max_bars_in_trade"],  # type: ignore[attr-defined]
            "trend_ema_tolerance_ratio": strategy._adaptive_policy._extra_rules["trend_ema_tolerance_ratio"],  # type: ignore[attr-defined]
        },
        extra_entry_freeze_keys={
            "min_atr_ratio",
            "rsi_min",
            "rsi_max",
            "lookback_breakdown_bars",
        },
        context_builder_kwargs={"ema_period": 20, "min_median_samples": 10},
        dynamic_enabled_env_key="PUT_MOM_DYNAMIC_POLICY_ENABLED",
        dynamic_policy_id_env_key="PUT_MOM_DYNAMIC_POLICY_ID",
    )
    ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = _make_candle(ts, h=100.0, low=98.0, c=99.0)
    strategy.on_bar("NIFTY_IDX", 300, candle, {})
    assert strategy._qty_for_label("NIFTY_ATM_PE_20000") == 3


def test_put_momentum_rehydrates_bars_in_trade_and_time_stops_after_restart(monkeypatch):
    entry_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc).isoformat()
    risk_manager = DummyRiskManager(
        restored_positions={
            "NIFTY_ATM_PE_20000": {
                "side": "BUY",
                "qty": 1,
                "entry_price": 100.0,
                "template_name": "put-momentum-scalper",
                "strategy_name": "put-momentum-scalper",
                "underlying": "NIFTY",
                "entry_ts": entry_ts,
                "exchange": "NFO",
                "symboltoken": "111",
                "tradingsymbol": "NIFTY26FEB20000PE",
                "strategy_context": {
                    "stop_price": 75.0,
                    "partial_tp": 125.0,
                    "final_tp": 150.0,
                    "breakdown_high": 100.0,
                    "position_qty": 1,
                    "bars_in_trade": 5,
                },
            }
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

    assert strategy.state.position is not None
    assert len(strategy.state.bars_5m) == 5

    candle = _make_candle(datetime(2025, 1, 1, 5, 0, tzinfo=timezone.utc), h=95.0, low=80.0, c=82.0)
    strategy.on_bar(
        "NIFTY_IDX",
        300,
        candle,
        {"ema_20": 90.0},
    )

    assert strategy.state.position is None
    assert len(calls) == 1
    assert calls[0].tag == "PUT_MOM_EXIT_TIME_STOP"
