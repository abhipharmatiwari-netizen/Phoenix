from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Deque, Optional


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _safe_median(values: Deque[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


@dataclass(frozen=True)
class MarketContext:
    ts: Optional[datetime]
    last_price: Optional[float]
    atr14: Optional[float]
    atr_norm: Optional[float]
    adx14: Optional[float]
    di_spread: Optional[float]
    ema_slope: Optional[float]
    gap_ratio: float
    chop_index: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    ema_value: Optional[float] = None

    def validate(self) -> bool:
        numeric_fields = (
            self.last_price,
            self.atr14,
            self.atr_norm,
            self.adx14,
            self.di_spread,
            self.ema_slope,
            self.gap_ratio,
            self.chop_index,
            self.plus_di,
            self.minus_di,
            self.ema_value,
        )
        for value in numeric_fields:
            if value is None:
                continue
            if not math.isfinite(float(value)):
                return False
        non_negative_fields = (
            self.atr14,
            self.atr_norm,
            self.adx14,
            self.di_spread,
            self.gap_ratio,
            self.chop_index,
            self.plus_di,
            self.minus_di,
        )
        for value in non_negative_fields:
            if value is None:
                continue
            if float(value) < 0.0:
                return False
        return True


class MarketContextBuilder:
    """
    Deterministic feature builder.

    atr_norm:
      atr14 / median(ATR history up to lookback window)
      - requires at least min_median_samples
      - uses available history during warmup

    ema_slope:
      (ema_t - ema_t-k) / max(epsilon, k*last_price)
      - optional deterministic EMA smoothing over slope
    """

    def __init__(
        self,
        *,
        atr_median_lookback: int = 200,
        min_median_samples: int = 50,
        epsilon: float = 1e-9,
        ema_period: int = 20,
        atr_norm_min: float = 0.5,
        atr_norm_max: float = 2.5,
        slope_shift_bars: int = 3,
        slope_smoothing_span: int = 3,
        enable_chop_index: bool = False,
        chop_lookback: int = 14,
    ) -> None:
        self.atr_median_lookback = max(2, int(atr_median_lookback))
        self.min_median_samples = max(2, int(min_median_samples))
        self.epsilon = max(1e-12, float(epsilon))
        self.ema_period = max(2, int(ema_period))
        self.atr_norm_min = float(min(atr_norm_min, atr_norm_max))
        self.atr_norm_max = float(max(atr_norm_min, atr_norm_max))
        self.slope_shift_bars = max(1, int(slope_shift_bars))
        self.slope_smoothing_span = max(0, int(slope_smoothing_span))
        self.enable_chop_index = bool(enable_chop_index)
        self.chop_lookback = max(2, int(chop_lookback))
        self._atr_history: Deque[float] = deque(maxlen=self.atr_median_lookback)
        self._ema_history: Deque[float] = deque(maxlen=max(8, self.slope_shift_bars + 2))
        self._tr_history: Deque[float] = deque(maxlen=self.chop_lookback)
        self._high_history: Deque[float] = deque(maxlen=self.chop_lookback)
        self._low_history: Deque[float] = deque(maxlen=self.chop_lookback)
        self._prev_close: Optional[float] = None
        self._smoothed_slope: Optional[float] = None
        self._last_session_date: Optional[date] = None

    def reset(self) -> None:
        self._atr_history.clear()
        self._ema_history.clear()
        self._tr_history.clear()
        self._high_history.clear()
        self._low_history.clear()
        self._prev_close = None
        self._smoothed_slope = None
        self._last_session_date = None

    def build(self, *, candle: Any, indicators: dict[str, Any]) -> MarketContext:
        last_price = _to_float(getattr(candle, "c", None))
        if last_price is None:
            last_price = _to_float(indicators.get("close"))

        atr14 = _to_float(indicators.get("atr"))
        if atr14 is not None and atr14 >= 0.0:
            self._atr_history.append(atr14)

        atr_norm: Optional[float] = None
        if atr14 is not None and len(self._atr_history) >= self.min_median_samples:
            atr_median = _safe_median(self._atr_history)
            if atr_median is not None:
                atr_norm_raw = atr14 / max(self.epsilon, atr_median)
                atr_norm = max(self.atr_norm_min, min(self.atr_norm_max, atr_norm_raw))

        adx14 = _to_float(indicators.get("adx"))
        plus_di = _to_float(indicators.get("plus_di"))
        minus_di = _to_float(indicators.get("minus_di"))
        di_spread = _to_float(indicators.get("di_spread"))
        if plus_di is not None and minus_di is not None:
            di_spread = abs(minus_di - plus_di)

        ema_key = f"ema_{self.ema_period}"
        ema_now = _to_float(indicators.get(ema_key))
        ema_slope: Optional[float] = None
        if ema_now is not None:
            self._ema_history.append(ema_now)
        if (
            ema_now is not None
            and len(self._ema_history) > self.slope_shift_bars
            and last_price is not None
        ):
            ema_shift = self._ema_history[-(self.slope_shift_bars + 1)]
            denom = max(self.epsilon, float(self.slope_shift_bars) * last_price)
            raw_slope = (ema_now - ema_shift) / denom
            if self.slope_smoothing_span > 1:
                alpha = 2.0 / (self.slope_smoothing_span + 1.0)
                if self._smoothed_slope is None:
                    self._smoothed_slope = raw_slope
                else:
                    self._smoothed_slope = (alpha * raw_slope) + (
                        (1.0 - alpha) * self._smoothed_slope
                    )
                ema_slope = self._smoothed_slope
            else:
                ema_slope = raw_slope

        ts = getattr(candle, "start_ts", None)
        open_price = _to_float(getattr(candle, "o", None))
        gap_ratio = 0.0
        session_date = ts.date() if isinstance(ts, datetime) else None
        is_session_open = (
            session_date is not None
            and self._last_session_date is not None
            and session_date != self._last_session_date
        )
        if (
            is_session_open
            and open_price is not None
            and self._prev_close is not None
            and atr14 is not None
        ):
            gap_ratio = abs(open_price - self._prev_close) / max(self.epsilon, atr14)
        if session_date is not None:
            self._last_session_date = session_date

        chop_index: Optional[float] = None
        if self.enable_chop_index:
            high_price = _to_float(getattr(candle, "h", None))
            if high_price is None:
                high_price = _to_float(indicators.get("high"))
            low_price = _to_float(getattr(candle, "l", None))
            if low_price is None:
                low_price = _to_float(indicators.get("low"))
            if high_price is not None and low_price is not None:
                tr_component = high_price - low_price
                if self._prev_close is not None:
                    tr_component = max(
                        tr_component,
                        abs(high_price - self._prev_close),
                        abs(low_price - self._prev_close),
                    )
                if tr_component >= 0.0:
                    self._tr_history.append(float(tr_component))
                    self._high_history.append(float(high_price))
                    self._low_history.append(float(low_price))
            if (
                len(self._tr_history) >= self.chop_lookback
                and len(self._high_history) >= self.chop_lookback
                and len(self._low_history) >= self.chop_lookback
            ):
                tr_sum = float(sum(self._tr_history))
                high_n = float(max(self._high_history))
                low_n = float(min(self._low_history))
                range_n = high_n - low_n
                if tr_sum > self.epsilon and range_n > self.epsilon:
                    chop_index = 100.0 * (
                        math.log10(tr_sum / range_n) / math.log10(float(self.chop_lookback))
                    )

        if last_price is not None:
            self._prev_close = last_price

        return MarketContext(
            ts=ts,
            last_price=last_price,
            atr14=atr14,
            atr_norm=atr_norm,
            adx14=adx14,
            di_spread=di_spread,
            ema_slope=ema_slope,
            gap_ratio=gap_ratio,
            chop_index=chop_index,
            plus_di=plus_di,
            minus_di=minus_di,
            ema_value=ema_now,
        )


__all__ = [
    "MarketContext",
    "MarketContextBuilder",
]
