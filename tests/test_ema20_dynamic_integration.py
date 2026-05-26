from __future__ import annotations

import importlib
import types
from datetime import datetime, timedelta, timezone

from app.brokers.base import OrderResponse
from app.core.signal_metrics import get_labeled_metrics

MODULE_PATH = "app.strategies.ema20_strategy"


def _make_candle(ts: datetime, close: float, open_: float | None = None):
    return types.SimpleNamespace(start_ts=ts, c=close, o=open_ if open_ is not None else close)


def _make_strategy(
    monkeypatch,
    *,
    dynamic_policy: dict | None = None,
    require_rsi_falling: bool = False,
):
    monkeypatch.setenv("NG_EMA20_REQUIRE_RSI_FALLING", "1" if require_rsi_falling else "0")
    mod = importlib.import_module(MODULE_PATH)
    instrument_meta = {
        "NG_FUT": {"symbol": "NG_FUT"},
        "NG_ATM_CE_100": {
            "symbol": "NATURALGAS20FEB26100CE",
            "token": "123",
            "lot_size": 1,
            "exchange": "MCX",
        },
    }
    strategy = mod.Ema20Strategy(
        instrument_meta,
        env_prefix="NG_",
        underlying_label="NG_FUT",
        signal_timeframe=300,
        ema_period=20,
        dynamic_policy=dynamic_policy,
    )
    return mod, strategy


def _patch_bridge(monkeypatch, mod):
    calls = []

    def fake_place_order_via_bridge(**kwargs):
        calls.append(kwargs)
        return OrderResponse(
            broker_order_id=f"order-{len(calls)}",
            status="SUCCESS",
            message="ok",
        )

    monkeypatch.setattr(mod, "place_order_via_bridge", fake_place_order_via_bridge)
    return calls


def _bar_indicators(*, ema: float = 100.0, atr: float = 1.0, rsi: float = 50.0, adx: float = 30.0, plus_di: float = 15.0, minus_di: float = 25.0):
    return {
        "ema_20": ema,
        "atr": atr,
        "rsi": rsi,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }


def test_dynamic_mode_off_is_behavior_identical(monkeypatch):
    mod_a, strategy_a = _make_strategy(monkeypatch, dynamic_policy=None, require_rsi_falling=False)
    calls_a = _patch_bridge(monkeypatch, mod_a)

    base_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    candle = _make_candle(base_ts, 90.0)
    strategy_a.on_bar("NG_FUT", 300, candle, _bar_indicators())

    mod_b, strategy_b = _make_strategy(
        monkeypatch,
        dynamic_policy={"enabled": False, "policy_id": ""},
        require_rsi_falling=False,
    )
    calls_b = _patch_bridge(monkeypatch, mod_b)
    strategy_b.on_bar("NG_FUT", 300, candle, _bar_indicators())

    assert len(calls_a) == 1
    assert len(calls_b) == 1
    assert strategy_a.position is not None
    assert strategy_b.position is not None
    assert strategy_a.position.sl_price == strategy_b.position.sl_price
    assert strategy_a.position.tp_price == strategy_b.position.tp_price


def test_dynamic_mode_on_changes_effective_params_by_regime(monkeypatch):
    dynamic_policy = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "freeze_on_entry": True,
        "profiles": {
            "TRENDING": {
                "sl_pct": 0.5,
                "tp_pct": 0.7,
            }
        },
    }
    mod, strategy = _make_strategy(
        monkeypatch,
        dynamic_policy=dynamic_policy,
        require_rsi_falling=False,
    )
    calls = _patch_bridge(monkeypatch, mod)
    base_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)

    # Warm up ATR median, then allow entry under directional TRENDING regime.
    for i in range(60):
        candle = _make_candle(base_ts + timedelta(minutes=5 * i), 90.0, open_=90.0)
        strategy.on_bar("NG_FUT", 300, candle, _bar_indicators(ema=100.0 - (i * 0.05)))
        if strategy.position:
            break

    assert len(calls) == 1
    assert strategy.position is not None
    assert strategy._current_regime.value == "TRENDING_DOWN"
    # Dynamic profile modifies entry SL/TP away from defaults 0.30/0.30.
    assert round(strategy.position.sl_price / strategy.position.entry_price - 1.0, 6) == 0.5
    assert round(1.0 - strategy.position.tp_price / strategy.position.entry_price, 6) == 0.7


def test_missing_context_falls_back_safely(monkeypatch):
    dynamic_policy = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
    }
    mod, strategy = _make_strategy(
        monkeypatch,
        dynamic_policy=dynamic_policy,
        require_rsi_falling=False,
    )
    calls = _patch_bridge(monkeypatch, mod)
    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)

    # Missing ATR/ADX/DI keeps regime NO_TRADE under dynamic mode.
    strategy.on_bar(
        "NG_FUT",
        300,
        candle,
        {"ema_20": 100.0, "rsi": 50.0},
    )
    assert calls == []
    assert strategy.position is None


def test_dynamic_policy_regime_warmup_allows_entry_after_ten_bars(monkeypatch):
    dynamic_policy = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "hold_bars": 1,
        "profiles": {
            "NO_TRADE": {"disable_entries": True},
        },
    }
    mod, strategy = _make_strategy(
        monkeypatch,
        dynamic_policy=dynamic_policy,
        require_rsi_falling=False,
    )
    calls = _patch_bridge(monkeypatch, mod)
    base_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)

    for i in range(12):
        candle = _make_candle(base_ts + timedelta(minutes=5 * i), 90.0)
        strategy.on_bar(
            "NG_FUT",
            300,
            candle,
            _bar_indicators(
                ema=100.0,
                atr=1.0,
                adx=20.0,
                plus_di=15.0,
                minus_di=25.0,
            ),
        )

    assert len(calls) == 1
    assert strategy.position is not None
    assert strategy._current_regime.value == "NORMAL"


def test_kill_switch_disables_dynamic_mode(monkeypatch):
    monkeypatch.setenv("EMA20_DYNAMIC_POLICY_ENABLED", "false")
    dynamic_policy = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "profiles": {
            "TRENDING": {"disable_entries": True},
            "NORMAL": {"disable_entries": True},
            "CHOPPY": {"disable_entries": True},
            "HIGH_VOL": {"disable_entries": True},
            "NO_TRADE": {"disable_entries": True},
        },
    }
    mod, strategy = _make_strategy(
        monkeypatch,
        dynamic_policy=dynamic_policy,
        require_rsi_falling=False,
    )
    calls = _patch_bridge(monkeypatch, mod)

    candle = _make_candle(datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc), 90.0)
    strategy.on_bar("NG_FUT", 300, candle, _bar_indicators())

    assert strategy._dynamic_enabled is False
    assert len(calls) == 1
    assert strategy.position is not None


def test_dynamic_metrics_emit_with_labels(monkeypatch):
    metrics = get_labeled_metrics()
    metrics.reset_for_tests()
    dynamic_policy = {
        "enabled": True,
        "policy_id": "ema20_ng_v1",
        "profiles": {
            "TRENDING": {"sl_pct": 0.5},
            "NO_TRADE": {"disable_entries": True},
        },
    }
    mod, strategy = _make_strategy(
        monkeypatch,
        dynamic_policy=dynamic_policy,
        require_rsi_falling=False,
    )
    _calls = _patch_bridge(monkeypatch, mod)
    base_ts = datetime(2025, 1, 1, 4, 0, tzinfo=timezone.utc)
    for i in range(60):
        candle = _make_candle(base_ts + timedelta(minutes=5 * i), 90.0, open_=90.0)
        strategy.on_bar("NG_FUT", 300, candle, _bar_indicators(ema=100.0 - (i * 0.05)))
        if strategy.position:
            break

    assert metrics.gauge_value(
        "dynamic_enabled_gauge",
        labels={"underlying": "NG_FUT", "tenant": "default"},
    ) == 1.0
    assert metrics.counter_value(
        "policy_apply_total",
        labels={"underlying": "NG_FUT", "tenant": "default", "regime": "TRENDING_DOWN"},
    ) >= 1.0
    assert metrics.gauge_value(
        "regime_current_gauge",
        labels={"underlying": "NG_FUT", "tenant": "default", "regime": "TRENDING_DOWN"},
    ) == 1.0
