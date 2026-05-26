from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from app.strategies.adaptive.market_context import MarketContextBuilder


def _candle(ts: datetime, *, o: float, c: float):
    return types.SimpleNamespace(start_ts=ts, o=o, c=c)


def test_atr_norm_is_clamped_to_workbook_range():
    builder = MarketContextBuilder(
        atr_median_lookback=10,
        min_median_samples=3,
        ema_period=20,
        slope_shift_bars=3,
        slope_smoothing_span=1,
    )
    base_ts = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)

    out = None
    for i, atr in enumerate((1.0, 1.0, 1.0, 100.0)):
        out = builder.build(
            candle=_candle(base_ts + timedelta(minutes=5 * i), o=100.0, c=100.0),
            indicators={"atr": atr, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
        )
    assert out is not None
    assert out.atr_norm == 2.5

    builder.reset()
    for i, atr in enumerate((100.0, 100.0, 100.0, 1.0)):
        out = builder.build(
            candle=_candle(base_ts + timedelta(minutes=5 * i), o=100.0, c=100.0),
            indicators={"atr": atr, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
        )
    assert out is not None
    assert out.atr_norm == 0.5


def test_default_atr_norm_warmup_is_ten_bars():
    builder = MarketContextBuilder(
        ema_period=20,
        slope_shift_bars=3,
        slope_smoothing_span=1,
    )
    base_ts = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)

    out = None
    for i in range(9):
        out = builder.build(
            candle=_candle(base_ts + timedelta(minutes=5 * i), o=100.0, c=100.0),
            indicators={"atr": 1.0, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
        )

    assert out is not None
    assert out.atr_norm is None

    out = builder.build(
        candle=_candle(base_ts + timedelta(minutes=45), o=100.0, c=100.0),
        indicators={"atr": 1.0, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
    )
    assert out.atr_norm == 1.0


def test_ema_slope_uses_k_shift_formula():
    builder = MarketContextBuilder(
        min_median_samples=2,
        ema_period=20,
        slope_shift_bars=3,
        slope_smoothing_span=1,
    )
    base_ts = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)

    out = None
    for i, ema in enumerate((100.0, 100.0, 100.0, 106.0)):
        out = builder.build(
            candle=_candle(base_ts + timedelta(minutes=5 * i), o=100.0, c=100.0),
            indicators={"atr": 1.0, "ema_20": ema, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
        )
    assert out is not None
    assert out.ema_slope is not None
    assert round(out.ema_slope, 6) == 0.02


def test_gap_ratio_computes_only_on_new_session_open():
    builder = MarketContextBuilder(
        min_median_samples=2,
        ema_period=20,
        slope_shift_bars=3,
        slope_smoothing_span=1,
    )
    day1 = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)
    day2 = datetime(2025, 1, 2, 9, 15, tzinfo=timezone.utc)

    bar1 = builder.build(
        candle=_candle(day1, o=100.0, c=100.0),
        indicators={"atr": 10.0, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
    )
    bar2 = builder.build(
        candle=_candle(day1 + timedelta(minutes=5), o=120.0, c=110.0),
        indicators={"atr": 10.0, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
    )
    bar3 = builder.build(
        candle=_candle(day2, o=130.0, c=120.0),
        indicators={"atr": 10.0, "ema_20": 100.0, "adx": 20.0, "plus_di": 10.0, "minus_di": 15.0},
    )

    assert bar1.gap_ratio == 0.0
    assert bar2.gap_ratio == 0.0
    assert round(bar3.gap_ratio, 6) == 2.0


def test_di_spread_can_be_consumed_directly_when_di_components_absent():
    builder = MarketContextBuilder(
        min_median_samples=2,
        ema_period=20,
        slope_shift_bars=3,
        slope_smoothing_span=1,
    )
    ts = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)
    ctx = builder.build(
        candle=_candle(ts, o=100.0, c=100.0),
        indicators={
            "atr": 1.0,
            "ema_20": 100.0,
            "adx": 20.0,
            "di_spread": 7.5,
        },
    )
    assert ctx.di_spread == 7.5
