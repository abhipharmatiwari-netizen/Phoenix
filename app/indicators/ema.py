"""
Exponential moving average (EMA) indicator helpers.
Used by indicator engines and strategies.
"""

from __future__ import annotations

import pandas as pd


# Compute EMA for a price series.
def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """
    Compute exponential moving average (EMA) of a price series.

    Parameters
    ----------
    series : pd.Series
        Typically the 'close' price.
    period : int
        EMA lookback period.

    Returns
    -------
    pd.Series
        EMA values aligned with `series`.
    """
    return series.ewm(span=period, adjust=False).mean()


__all__ = ["compute_ema"]
