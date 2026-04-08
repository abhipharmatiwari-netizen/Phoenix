from collections import deque
from datetime import datetime, timedelta, timezone

from app.core.indicators_engine import Candle, MultiInstrumentIndicatorEngine, _compute_adx_dmi


def _downtrend_candles(count: int) -> deque[Candle]:
    candles: deque[Candle] = deque(maxlen=max(100, count + 5))
    start = datetime(2026, 1, 1, 9, 15, tzinfo=timezone.utc)
    for i in range(count):
        price = 100.0 - (i * 0.8)
        candles.append(
            Candle(
                start_ts=start + timedelta(minutes=i),
                end_ts=start + timedelta(minutes=i + 1),
                o=price,
                h=price + 0.3,
                low=price - 0.6,
                c=price - 0.4,
            )
        )
    return candles


def test_compute_adx_dmi_returns_values_for_downtrend():
    candles = _downtrend_candles(40)
    adx, plus_di, minus_di = _compute_adx_dmi(candles, period=14)
    assert adx is not None
    assert plus_di is not None
    assert minus_di is not None
    assert adx >= 0.0
    assert minus_di > plus_di


def test_get_latest_indicators_includes_adx_dmi():
    engine = MultiInstrumentIndicatorEngine(
        timeframes=[60],
        adx_period=3,
        max_candles=100,
    )
    bars = []
    ts = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)
    for i in range(12):
        price = 200.0 - (i * 1.2)
        bars.append(
            {
                "label": "NIFTY_IDX",
                "timeframe_seconds": 60,
                "ts_start": ts + timedelta(minutes=i),
                "ts_end": ts + timedelta(minutes=i + 1),
                "o": price,
                "h": price + 0.5,
                "l": price - 0.8,
                "c": price - 0.4,
            }
        )
    seeded = engine.seed_from_closed_bars(bars)
    assert seeded == 12

    latest = engine.get_latest_indicators("NIFTY_IDX", 60)
    assert latest is not None
    assert "adx" in latest
    assert "plus_di" in latest
    assert "minus_di" in latest
    assert latest["adx"] is not None
    assert latest["plus_di"] is not None
    assert latest["minus_di"] is not None

