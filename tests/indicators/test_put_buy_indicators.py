import numpy as np
import pandas as pd
import pytest

from app.strategies.put_buy_indicators import atr, ema, macd, rsi


def test_ema_matches_pandas():
    series = pd.Series([1, 2, 3, 4, 5], dtype=float)
    expected = series.ewm(span=3, adjust=False, min_periods=3).mean()
    result = ema(series, 3)
    assert np.allclose(result.fillna(0), expected.fillna(0))


def test_rsi_trending_up():
    series = pd.Series(range(1, 30), dtype=float)
    result = rsi(series, 14)
    assert result.iloc[-1] > 70


def test_atr_constant_range():
    df = pd.DataFrame(
        {
            "high": [10.0] * 30,
            "low": [9.0] * 30,
            "close": [9.5] * 30,
        }
    )
    result = atr(df, 14)
    assert result.iloc[-1] == pytest.approx(1.0, rel=1e-3)


def test_macd_flat_series_zero_hist():
    series = pd.Series([100.0] * 60)
    _, _, hist = macd(series, 12, 26, 9)
    last = hist.dropna().iloc[-1]
    assert abs(last) < 1e-6
