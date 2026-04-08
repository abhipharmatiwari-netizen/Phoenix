from __future__ import annotations

import math
import types
from datetime import datetime, timedelta, timezone

from app.strategies.adaptive.dynamic_policy import DynamicPolicyEngine, parse_dynamic_policy_config
from app.strategies.adaptive.market_context import MarketContextBuilder
from app.strategies.adaptive.regime import RegimeClassifier, RegimeThresholds


def _run_sequence():
    cfg = parse_dynamic_policy_config(
        {
            "enabled": True,
            "policy_id": "ema20_ng_v1",
            "hold_bars": 3,
            "thresholds": {
                "adx_trend": 22,
                "di_spread_trend": 8,
                "atr_norm_high": 1.5,
                "chop_adx_max": 18,
            },
            "profiles": {
                "TRENDING": {"sl_pct": 0.45, "tp_pct": 0.8, "qty_mult": 0.9},
                "CHOPPY": {"sl_pct": 0.30, "tp_pct": 0.35, "qty_mult": 0.6},
                "HIGH_VOL": {"sl_pct": 0.60, "qty_mult": 0.5},
                "NO_TRADE": {"disable_entries": True},
            },
        }
    )
    builder = MarketContextBuilder(
        atr_median_lookback=200,
        min_median_samples=50,
        ema_period=20,
        slope_smoothing_span=3,
    )
    classifier = RegimeClassifier(
        RegimeThresholds(
            adx_trend=cfg.thresholds.adx_trend,
            di_spread_trend=cfg.thresholds.di_spread_trend,
            atr_norm_high=cfg.thresholds.atr_norm_high,
            atr_norm_low=cfg.thresholds.atr_norm_low,
            chop_adx_max=cfg.thresholds.chop_adx_max,
            hold_bars=cfg.hold_bars,
            min_regime_hold_minutes=cfg.min_regime_hold_minutes,
        )
    )
    engine = DynamicPolicyEngine(config=cfg)

    base_params = {
        "sl_pct": 0.3,
        "tp_pct": 0.3,
        "trail_buffer_pct": 0.1,
        "trail_trigger_pct": 0.2,
        "qty_mult": 1.0,
        "disable_entries": False,
    }
    base_ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    regimes = []
    features = []
    effective_params = []
    prev_close = 100.0
    for i in range(260):
        ts = base_ts + timedelta(minutes=5 * i)
        close = 100.0 + (0.12 * i) + math.sin(i / 5.0)
        open_ = prev_close + math.cos(i / 8.0) * 0.2
        prev_close = close
        candle = types.SimpleNamespace(start_ts=ts, c=close, o=open_)
        indicators = {
            "ema_20": 100.0 + (0.10 * i),
            "atr": 1.0 + ((i % 20) * 0.04),
            "adx": 15.0 + (i % 20),
            "plus_di": 12.0 + (i % 11),
            "minus_di": 16.0 + (i % 13),
        }
        ctx = builder.build(candle=candle, indicators=indicators)
        regime = classifier.update(ctx)
        effective = engine.apply(
            base_params=base_params,
            regime=regime,
            context=ctx,
        )
        regimes.append(regime.value)
        features.append((ctx.atr_norm, ctx.ema_slope))
        effective_params.append(dict(effective))
    return regimes, features, effective_params


def test_dynamic_policy_golden_parity_is_deterministic():
    regimes_a, features_a, effective_a = _run_sequence()
    regimes_b, features_b, effective_b = _run_sequence()

    assert regimes_a == regimes_b
    assert len(features_a) == len(features_b) == 260
    for (atr_norm_a, ema_slope_a), (atr_norm_b, ema_slope_b) in zip(features_a, features_b):
        if atr_norm_a is None or atr_norm_b is None:
            assert atr_norm_a is atr_norm_b
        else:
            assert abs(atr_norm_a - atr_norm_b) <= 1e-12
        if ema_slope_a is None or ema_slope_b is None:
            assert ema_slope_a is ema_slope_b
        else:
            assert abs(ema_slope_a - ema_slope_b) <= 1e-12
    assert effective_a == effective_b
