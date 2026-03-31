"""
Compute indicators (ATR, RSI, MACD, ADX/DMI, EMA) from live ticks across instruments.
Build candles per timeframe and trigger callbacks on bar close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Deque, Dict, Optional, Tuple, Iterable, Callable, Any, List, Mapping

import logging
import os
import threading
import pandas as pd

from app.indicators.ema import compute_ema
from app.core.daily_levels import DailyLevelsCache

logger = logging.getLogger("indicators_engine")

MAX_BARS_PER_INSTRUMENT = 300
MAX_STALE_BAR_DURATION_MULTIPLIER = 3
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _parse_env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


@dataclass
class Candle:
    start_ts: datetime
    end_ts: Optional[datetime]
    o: float
    h: float
    low: float
    c: float


@dataclass(frozen=True)
class GapBackfillRequest:
    label: str
    timeframe_seconds: int
    reason: str
    current_candle: Candle
    gap_start_ts: datetime
    next_bucket_start_ts: datetime
    gap_seconds: float
    max_expected_gap_seconds: float


@dataclass
class TimeframeState:
    timeframe_seconds: int
    candles: Deque[Candle]
    current_candle: Optional[Candle]
    atr: Optional[float] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    ema: Dict[int, Optional[float]] = field(default_factory=dict)


class InstrumentState:
    # Initialize per-instrument candle and timeframe state.
    def __init__(self, label: str, timeframes: Iterable[int], max_candles: int):
        self.label = label
        self._tick_count = 0  # Initialize tick count
        self.timeframes: Dict[int, TimeframeState] = {}
        for tf in timeframes:
            self.timeframes[tf] = TimeframeState(
                timeframe_seconds=tf,
                candles=deque(maxlen=max_candles),
                current_candle=None,
            )


# ---------- Indicator helpers ----------


# Compute ATR from recent candles.
def _compute_atr(candles: Deque[Candle], period: int) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    recent = list(candles)[-(period + 1) :]
    trs = []
    for i in range(1, len(recent)):
        high = recent[i].h
        low = recent[i].low
        prev_close = recent[i - 1].c
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


# Compute RSI from close prices.
def _compute_rsi_from_closes(closes, period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# Compute an EMA series for a list of values.
def _ema_series(values, period: int):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    ema_vals = []
    ema_prev = values[0]
    for i, price in enumerate(values):
        if i == 0:
            ema_prev = price
        else:
            ema_prev = price * k + ema_prev * (1.0 - k)
        ema_vals.append(ema_prev)
    return ema_vals


# Compute MACD, signal, and histogram from close prices.
def _compute_macd_from_closes(
    closes,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> Optional[Tuple[float, float, float]]:
    # Warm up MACD sooner: require only max(fast, signal) closes instead of full slow+signal.
    # This avoids long stretches of empty MACD on higher timeframes (e.g. 5m/15m) and for
    # thinner symbols like MIDCPNIFTY.
    if len(closes) < max(fast_period, signal_period):
        return None
    fast_ema = _ema_series(closes, fast_period)
    slow_ema = _ema_series(closes, slow_period)
    macd_vals = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_vals = _ema_series(macd_vals, signal_period)
    macd = macd_vals[-1]
    signal = signal_vals[-1]
    hist = macd - signal
    return macd, signal, hist


# Compute ADX and DMI (+DI/-DI) from candles using Wilder smoothing.
def _compute_adx_dmi(
    candles: Deque[Candle],
    period: int,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if period < 2 or len(candles) < period + 1:
        return None, None, None

    recent = list(candles)
    tr_values: list[float] = []
    plus_dm_values: list[float] = []
    minus_dm_values: list[float] = []

    for i in range(1, len(recent)):
        prev = recent[i - 1]
        curr = recent[i]
        up_move = curr.h - prev.h
        down_move = prev.low - curr.low
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = max(
            curr.h - curr.low,
            abs(curr.h - prev.c),
            abs(curr.low - prev.c),
        )
        tr_values.append(float(tr))
        plus_dm_values.append(float(plus_dm))
        minus_dm_values.append(float(minus_dm))

    if len(tr_values) < period:
        return None, None, None

    smoothed_tr = sum(tr_values[:period])
    smoothed_plus_dm = sum(plus_dm_values[:period])
    smoothed_minus_dm = sum(minus_dm_values[:period])
    plus_di = 0.0
    minus_di = 0.0
    dx_values: list[float] = []

    def _di_and_dx(
        tr_val: float,
        plus_dm_val: float,
        minus_dm_val: float,
    ) -> tuple[float, float, float]:
        if tr_val <= 0.0:
            return 0.0, 0.0, 0.0
        local_plus_di = 100.0 * (plus_dm_val / tr_val)
        local_minus_di = 100.0 * (minus_dm_val / tr_val)
        di_sum = local_plus_di + local_minus_di
        if di_sum <= 0.0:
            dx = 0.0
        else:
            dx = 100.0 * abs(local_plus_di - local_minus_di) / di_sum
        return local_plus_di, local_minus_di, dx

    plus_di, minus_di, dx = _di_and_dx(
        smoothed_tr,
        smoothed_plus_dm,
        smoothed_minus_dm,
    )
    dx_values.append(dx)

    for i in range(period, len(tr_values)):
        smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_values[i]
        smoothed_plus_dm = (
            smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm_values[i]
        )
        smoothed_minus_dm = (
            smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm_values[i]
        )
        plus_di, minus_di, dx = _di_and_dx(
            smoothed_tr,
            smoothed_plus_dm,
            smoothed_minus_dm,
        )
        dx_values.append(dx)

    if len(dx_values) < period:
        return None, plus_di, minus_di

    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return adx, plus_di, minus_di


# ---------- Engine ----------


class MultiInstrumentIndicatorEngine:
    """
    - Supports multiple instruments AND multiple timeframes.
    - Builds time-based candles per instrument & timeframe.
    - Computes ATR / RSI / MACD / ADX-DMI (and optional EMA(s)) per timeframe on bar close.
    - Exposes `on_bar(label, timeframe_seconds, candle, indicators)` callback.
    """

    # Initialize indicator settings and per-instrument state.
    def __init__(
        self,
        timeframes: Iterable[int] = (60, 300, 900),  # 1m, 5m, 15m
        atr_period: int = 5,  # 5 bars -> ~5 minutes on 1m
        rsi_period: int = 5,  # 5 bars -> ~5 minutes on 1m
        adx_period: int = 14,  # default DMI/ADX smoothing period
        macd_fast: int = 5,  # MACD tuned to need ~15 bars total
        macd_slow: int = 10,
        macd_signal: int = 5,
        ema_periods_by_timeframe: Optional[Mapping[int, Iterable[int]]] = None,
        max_candles: int = 1000,
        daily_levels_cache: Optional[DailyLevelsCache] = None,
    ) -> None:
        self.timeframes = sorted(set(int(tf) for tf in timeframes))
        self.atr_period = atr_period
        self.rsi_period = rsi_period
        self.adx_period = max(2, int(adx_period))
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        # Clamp candle history to avoid unbounded growth even if misconfigured.
        self.max_candles = min(max_candles, MAX_BARS_PER_INSTRUMENT)
        # Map timeframe -> EMA periods to compute (e.g. {300: [20]})
        self.ema_periods_by_timeframe: Dict[int, List[int]] = {}
        if ema_periods_by_timeframe:
            for tf, periods in ema_periods_by_timeframe.items():
                period_list = [int(p) for p in periods if p is not None]
                if not period_list:
                    continue
                self.ema_periods_by_timeframe[int(tf)] = sorted(set(period_list))

        # label -> InstrumentState
        self.states: Dict[str, InstrumentState] = {}
        self._lock = threading.RLock()

        # Optional callback: on_bar(label, timeframe_seconds, candle, indicators_dict)
        self.on_bar: Optional[Callable[[str, int, Candle, Dict[str, Any]], None]] = None
        self.on_gap_backfill: Optional[
            Callable[[GapBackfillRequest], Iterable[Mapping[str, Any]] | None]
        ] = None
        self._daily_levels_cache = daily_levels_cache
        self._stale_warning_window_seconds = _parse_env_float(
            "INDICATOR_STALE_WARNING_WINDOW_SECONDS",
            default=300.0,
            minimum=1.0,
        )
        self._stale_warning_burst = _parse_env_int(
            "INDICATOR_STALE_WARNING_BURST",
            default=1,
            minimum=1,
        )
        # (label, timeframe, reason) -> {"window_start": float, "emitted": float, "suppressed": float}
        self._stale_warning_state: Dict[Tuple[str, int, str], Dict[str, float]] = {}

    def _log_reset_warning(
        self,
        *,
        label: str,
        timeframe_seconds: int,
        reason: str,
        bar_duration_seconds: float,
        max_duration_seconds: float,
        event_ts: datetime,
    ) -> None:
        if reason != "stale_gap":
            logger.warning(
                "Resetting indicator state for %s tf=%ss due to %s: duration=%.0fs (max=%.0fs)",
                label,
                timeframe_seconds,
                reason,
                bar_duration_seconds,
                max_duration_seconds,
            )
            return

        key = (label, timeframe_seconds, reason)
        event_epoch = event_ts.timestamp()
        state = self._stale_warning_state.get(key)
        if state is None:
            state = {"window_start": event_epoch, "emitted": 0.0, "suppressed": 0.0}
            self._stale_warning_state[key] = state

        elapsed = event_epoch - float(state["window_start"])
        if elapsed >= self._stale_warning_window_seconds:
            suppressed_count = int(state["suppressed"])
            if suppressed_count > 0:
                logger.warning(
                    "Resetting indicator state for %s tf=%ss due to %s: suppressed=%d similar warnings in previous %.0fs window",
                    label,
                    timeframe_seconds,
                    reason,
                    suppressed_count,
                    self._stale_warning_window_seconds,
                )
            state["window_start"] = event_epoch
            state["emitted"] = 0.0
            state["suppressed"] = 0.0

        emitted_count = int(state["emitted"])
        if emitted_count < self._stale_warning_burst:
            logger.warning(
                "Resetting indicator state for %s tf=%ss due to %s: duration=%.0fs (max=%.0fs)",
                label,
                timeframe_seconds,
                reason,
                bar_duration_seconds,
                max_duration_seconds,
            )
            state["emitted"] = float(emitted_count + 1)
            return

        suppressed_count = int(state["suppressed"]) + 1
        state["suppressed"] = float(suppressed_count)
        if suppressed_count == 1:
            logger.warning(
                "Resetting indicator state warnings throttled for %s tf=%ss due to %s; additional warnings suppressed for %.0fs",
                label,
                timeframe_seconds,
                reason,
                self._stale_warning_window_seconds,
            )

    # Update indicator state from a new tick.
    def update_from_tick(
        self,
        label: str,
        price: float,
        timestamp: Optional[datetime] = None,
    ) -> None:
        with self._lock:
            if timestamp is None:
                timestamp = datetime.now(timezone.utc)

            if label not in self.states:
                logger.info("[INDICATOR] New instrument: %s", label)
                self.states[label] = InstrumentState(
                    label, self.timeframes, self.max_candles
                )
                logger.info(
                    "Initialized %s with timeframes: %s", label, self.timeframes
                )

            inst_state = self.states[label]

            # Log tick update (sampled to avoid spam - every 50th tick)
            if inst_state._tick_count % 50 == 0:
                logger.debug(
                    "[INDICATOR] Tick #%s: %s @ %s",
                    inst_state._tick_count,
                    label,
                    price,
                )
            inst_state._tick_count += 1

            for tf_state in inst_state.timeframes.values():
                self._update_timeframe(tf_state, price, timestamp, label)
    # Seed candle history from pre-closed bars (no on_bar callbacks).
    def seed_from_closed_bars(self, bars: Iterable[Mapping[str, Any]]) -> int:
        with self._lock:
            added = 0
            for bar in bars:
                try:
                    label = str(bar["label"])
                    timeframe_seconds = int(bar["timeframe_seconds"])
                    if timeframe_seconds not in self.timeframes:
                        continue
                    ts_start = bar["ts_start"]
                    ts_end = bar.get("ts_end")
                    o = float(bar["o"])
                    h = float(bar["h"])
                    low = float(bar["l"])
                    c = float(bar["c"])
                except Exception:
                    continue
                if label not in self.states:
                    self.states[label] = InstrumentState(
                        label, self.timeframes, self.max_candles
                    )
                tf_state = self.states[label].timeframes.get(timeframe_seconds)
                if tf_state is None:
                    continue
                tf_state.candles.append(
                    Candle(
                        start_ts=ts_start,
                        end_ts=ts_end,
                        o=o,
                        h=h,
                        low=low,
                        c=c,
                    )
                )
                added += 1

            for inst_state in self.states.values():
                for tf_state in inst_state.timeframes.values():
                    if tf_state.candles:
                        self._recompute_indicators(tf_state)
                        tf_state.current_candle = None
            return added

    # Drop cached state for labels that are no longer actively mapped.
    def prune_to_active_labels(self, active_labels: Iterable[str]) -> tuple[int, int]:
        active = {str(label) for label in active_labels if label is not None}
        with self._lock:
            removed_labels = 0
            for label in list(self.states.keys()):
                if label in active:
                    continue
                self.states.pop(label, None)
                removed_labels += 1

            removed_warning_windows = 0
            for key in list(self._stale_warning_state.keys()):
                if key[0] in active:
                    continue
                self._stale_warning_state.pop(key, None)
                removed_warning_windows += 1

            if removed_labels or removed_warning_windows:
                logger.info(
                    "Pruned indicator state: removed_labels=%d removed_warning_windows=%d active_labels=%d",
                    removed_labels,
                    removed_warning_windows,
                    len(active),
                )
            return removed_labels, removed_warning_windows
    # Align a timestamp to the start of its timeframe bucket.
    def _align_time_to_bucket(self, ts: datetime, timeframe_seconds: int) -> datetime:
        epoch = int(ts.timestamp())
        bucket_start_epoch = epoch - (epoch % timeframe_seconds)
        return datetime.fromtimestamp(bucket_start_epoch, tz=ts.tzinfo)

    # Update a timeframe with a tick, closing bars when a bucket ends.
    def _update_timeframe(
        self,
        tf_state: TimeframeState,
        price: float,
        ts: datetime,
        label: str,
    ) -> None:
        bucket_start = self._align_time_to_bucket(ts, tf_state.timeframe_seconds)
        c = tf_state.current_candle

        # New bucket => close prior candle, start new one
        if c is None or bucket_start > c.start_ts:
            if c is not None:
                c.end_ts = bucket_start
                bar_duration_seconds = max(
                    0.0, (bucket_start - c.start_ts).total_seconds()
                )
                max_duration_seconds = (
                    float(tf_state.timeframe_seconds)
                    * float(MAX_STALE_BAR_DURATION_MULTIPLIER)
                )
                is_stale_carry_over = bar_duration_seconds > max_duration_seconds
                session_rollover = (
                    c.start_ts.astimezone(IST).date()
                    != bucket_start.astimezone(IST).date()
                )

                if session_rollover or is_stale_carry_over:
                    reason = "session_rollover" if session_rollover else "stale_gap"
                    handled = False
                    if reason == "stale_gap":
                        handled = self._try_backfill_gap(
                            tf_state=tf_state,
                            label=label,
                            current_candle=c,
                            next_bucket_start=bucket_start,
                            gap_seconds=bar_duration_seconds,
                            max_expected_gap_seconds=max_duration_seconds,
                        )
                    if not handled:
                        self._log_reset_warning(
                            label=label,
                            timeframe_seconds=tf_state.timeframe_seconds,
                            reason=reason,
                            bar_duration_seconds=bar_duration_seconds,
                            max_duration_seconds=max_duration_seconds,
                            event_ts=bucket_start,
                        )
                        self._reset_timeframe_state(tf_state)
                else:
                    tf_state.candles.append(c)
                    self._recompute_indicators(tf_state)

                    logger.info(
                        f"📊 [INDICATOR] Bar closed: {label} {tf_state.timeframe_seconds}s | "
                        f"OHLC: {c.o:.2f}/{c.h:.2f}/{c.low:.2f}/{c.c:.2f} | "
                        f"ATR: {tf_state.atr} | RSI: {tf_state.rsi} | MACD: {tf_state.macd} | "
                        f"ADX: {tf_state.adx} | +DI: {tf_state.plus_di} | -DI: {tf_state.minus_di}"
                    )

                    if self.on_bar is not None:
                        indicators = {
                            "atr": tf_state.atr,
                            "rsi": tf_state.rsi,
                            "macd": tf_state.macd,
                            "macd_signal": tf_state.macd_signal,
                            "macd_hist": tf_state.macd_hist,
                            "adx": tf_state.adx,
                            "plus_di": tf_state.plus_di,
                            "minus_di": tf_state.minus_di,
                        }
                        for period, ema_val in tf_state.ema.items():
                            indicators[f"ema_{period}"] = ema_val
                        if self._daily_levels_cache is not None:
                            levels = self._daily_levels_cache.get_for_label(label)
                            if levels is not None:
                                indicators["pdh"] = levels.pdh
                                indicators["pdl"] = levels.pdl
                                indicators["pclose"] = levels.pclose
                                indicators["popen"] = levels.popen
                        try:
                            self.on_bar(label, tf_state.timeframe_seconds, c, indicators)
                        except Exception as e:
                            logger.exception(
                                "Error in on_bar callback for %s tf=%s: %s",
                                label,
                                tf_state.timeframe_seconds,
                                e,
                            )

            tf_state.current_candle = Candle(
                start_ts=bucket_start,
                end_ts=None,
                o=price,
                h=price,
                low=price,
                c=price,
            )
        else:
            # Update existing candle
            c.c = price
            if price > c.h:
                c.h = price
            if price < c.low:
                c.low = price

    def _try_backfill_gap(
        self,
        *,
        tf_state: TimeframeState,
        label: str,
        current_candle: Candle,
        next_bucket_start: datetime,
        gap_seconds: float,
        max_expected_gap_seconds: float,
    ) -> bool:
        if self.on_gap_backfill is None:
            return False
        current_bar_end = current_candle.start_ts + timedelta(
            seconds=tf_state.timeframe_seconds
        )
        if current_bar_end >= next_bucket_start:
            return False
        request = GapBackfillRequest(
            label=label,
            timeframe_seconds=tf_state.timeframe_seconds,
            reason="stale_gap",
            current_candle=Candle(
                start_ts=current_candle.start_ts,
                end_ts=current_bar_end,
                o=current_candle.o,
                h=current_candle.h,
                low=current_candle.low,
                c=current_candle.c,
            ),
            gap_start_ts=current_bar_end,
            next_bucket_start_ts=next_bucket_start,
            gap_seconds=gap_seconds,
            max_expected_gap_seconds=max_expected_gap_seconds,
        )
        try:
            raw_bars = list(self.on_gap_backfill(request) or [])
        except Exception:
            logger.exception(
                "Gap backfill callback failed for %s tf=%s",
                label,
                tf_state.timeframe_seconds,
            )
            return False
        normalized = self._normalize_backfill_bars(
            raw_bars=raw_bars,
            label=label,
            timeframe_seconds=tf_state.timeframe_seconds,
        )
        expected_starts: list[datetime] = []
        cursor = current_bar_end
        while cursor < next_bucket_start:
            expected_starts.append(cursor)
            cursor = cursor + timedelta(seconds=tf_state.timeframe_seconds)
        if not expected_starts:
            return False
        by_start = {bar.start_ts: bar for bar in normalized}
        if any(start not in by_start for start in expected_starts):
            return False
        tf_state.candles.append(request.current_candle)
        for start in expected_starts:
            tf_state.candles.append(by_start[start])
        self._recompute_indicators(tf_state)
        logger.info(
            "Applied gap backfill for %s tf=%s bars=%d gap_seconds=%.0f",
            label,
            tf_state.timeframe_seconds,
            len(expected_starts),
            gap_seconds,
        )
        return True

    @staticmethod
    def _normalize_backfill_bars(
        *,
        raw_bars: Iterable[Mapping[str, Any]],
        label: str,
        timeframe_seconds: int,
    ) -> list[Candle]:
        normalized: list[Candle] = []
        for bar in raw_bars:
            try:
                bar_label = str(bar["label"])
                bar_tf = int(bar["timeframe_seconds"])
                ts_start = bar["ts_start"]
                ts_end = bar.get("ts_end")
                o = float(bar["o"])
                h = float(bar["h"])
                low = float(bar["l"])
                c = float(bar["c"])
            except Exception:
                continue
            if bar_label != label or bar_tf != timeframe_seconds:
                continue
            if not isinstance(ts_start, datetime):
                continue
            if ts_start.tzinfo is None:
                ts_start = ts_start.replace(tzinfo=timezone.utc)
            else:
                ts_start = ts_start.astimezone(timezone.utc)
            if isinstance(ts_end, datetime):
                if ts_end.tzinfo is None:
                    ts_end = ts_end.replace(tzinfo=timezone.utc)
                else:
                    ts_end = ts_end.astimezone(timezone.utc)
            else:
                ts_end = ts_start + timedelta(seconds=timeframe_seconds)
            normalized.append(
                Candle(
                    start_ts=ts_start,
                    end_ts=ts_end,
                    o=o,
                    h=h,
                    low=low,
                    c=c,
                )
            )
        normalized.sort(key=lambda candle: candle.start_ts)
        return normalized

    # Reset one timeframe state to enforce strict intraday/session isolation.
    def _reset_timeframe_state(self, tf_state: TimeframeState) -> None:
        tf_state.candles.clear()
        tf_state.current_candle = None
        tf_state.atr = None
        tf_state.rsi = None
        tf_state.macd = None
        tf_state.macd_signal = None
        tf_state.macd_hist = None
        tf_state.adx = None
        tf_state.plus_di = None
        tf_state.minus_di = None
        tf_state.ema = {}

    # Recompute indicators after a candle closes.
    def _recompute_indicators(self, tf_state: TimeframeState) -> None:
        candles = tf_state.candles
        if len(candles) < 2:
            return

        # ATR
        tf_state.atr = _compute_atr(candles, self.atr_period)

        # RSI
        closes = [c.c for c in candles]
        tf_state.rsi = _compute_rsi_from_closes(closes, self.rsi_period)

        # MACD
        macd_tuple = _compute_macd_from_closes(
            closes,
            self.macd_fast,
            self.macd_slow,
            self.macd_signal,
        )
        if macd_tuple is not None:
            tf_state.macd, tf_state.macd_signal, tf_state.macd_hist = macd_tuple
        tf_state.adx, tf_state.plus_di, tf_state.minus_di = _compute_adx_dmi(
            candles,
            self.adx_period,
        )

        # EMA(s) per timeframe (e.g. 20 EMA on 5m)
        tf_state.ema = {}
        ema_periods = self.ema_periods_by_timeframe.get(tf_state.timeframe_seconds, [])
        for period in ema_periods:
            if len(closes) < period:
                tf_state.ema[period] = None
                continue
            try:
                ema_series = compute_ema(pd.Series(closes), period=period)
                tf_state.ema[period] = (
                    float(ema_series.iloc[-1]) if not ema_series.empty else None
                )
            except Exception:
                tf_state.ema[period] = None

    # Return the latest indicator snapshot for a label and timeframe.
    def get_latest_indicators(self, label: str, timeframe_seconds: int):
        with self._lock:
            inst_state = self.states.get(label)
            if not inst_state:
                return None
            tf_state = inst_state.timeframes.get(timeframe_seconds)
            if not tf_state or not tf_state.candles:
                return None
            last_candle = tf_state.candles[-1]
            result = {
                "atr": tf_state.atr,
                "rsi": tf_state.rsi,
                "macd": tf_state.macd,
                "macd_signal": tf_state.macd_signal,
                "macd_hist": tf_state.macd_hist,
                "adx": tf_state.adx,
                "plus_di": tf_state.plus_di,
                "minus_di": tf_state.minus_di,
                "last_candle": last_candle,
            }
            for period, ema_val in tf_state.ema.items():
                result[f"ema_{period}"] = ema_val
            if self._daily_levels_cache is not None:
                levels = self._daily_levels_cache.get_for_label(label)
                if levels is not None:
                    result["pdh"] = levels.pdh
                    result["pdl"] = levels.pdl
                    result["pclose"] = levels.pclose
                    result["popen"] = levels.popen
            return result
