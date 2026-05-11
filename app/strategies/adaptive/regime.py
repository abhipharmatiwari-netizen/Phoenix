from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .market_context import MarketContext

CLASSIFIER_VERSION = "dynamic_policy_v2"


class Regime(str, Enum):
    TRENDING = "TRENDING"
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    NORMAL = "NORMAL"
    CHOPPY = "CHOPPY"
    HIGH_VOL = "HIGH_VOL"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class RegimeThresholds:
    adx_trend: float = 25.0
    di_spread_trend: float = 5.0
    ema_slope_trend_min: float = 0.0
    atr_norm_high: float = 1.6
    atr_norm_secondary: float = 1.3
    gap_ratio_spike: float = 1.0
    atr_norm_low: float = 0.8
    adx_normal_low: float = 18.0
    adx_normal_high: float = 25.0
    chop_adx_max: float = 17.0
    chop_di_spread_max: float = 3.0
    use_chop_index: bool = False
    chop_index_high: float = 61.8
    hold_bars: int = 3
    min_regime_hold_minutes: int = 0


class RegimeClassifier:
    def __init__(self, thresholds: Optional[RegimeThresholds] = None) -> None:
        self.thresholds = thresholds or RegimeThresholds()
        self._current_regime: Optional[Regime] = None
        self._candidate_regime: Optional[Regime] = None
        self._candidate_count: int = 0
        self._last_switch_ts: Optional[datetime] = None

    @property
    def current_regime(self) -> Regime:
        return self._current_regime or Regime.NO_TRADE

    @property
    def last_switch_ts(self) -> Optional[datetime]:
        return self._last_switch_ts

    def reset(self) -> None:
        self._current_regime = None
        self._candidate_regime = None
        self._candidate_count = 0
        self._last_switch_ts = None

    def classify_once(self, ctx: Optional[MarketContext]) -> Regime:
        if ctx is None:
            return Regime.NO_TRADE
        if not ctx.validate():
            return Regime.NO_TRADE
        if (
            ctx.atr_norm is None
            or ctx.adx14 is None
            or ctx.di_spread is None
            or ctx.ema_slope is None
        ):
            return Regime.NO_TRADE

        high_vol = bool(
            ctx.atr_norm >= self.thresholds.atr_norm_high
            or (
                ctx.atr_norm >= self.thresholds.atr_norm_secondary
                and ctx.gap_ratio >= self.thresholds.gap_ratio_spike
            )
        )
        if high_vol:
            return Regime.HIGH_VOL

        if (
            ctx.adx14 >= self.thresholds.adx_trend
            and ctx.di_spread >= self.thresholds.di_spread_trend
            and abs(ctx.ema_slope) >= self.thresholds.ema_slope_trend_min
        ):
            directional = self._directional_trending_regime(ctx)
            if directional is not None:
                return directional
            return Regime.TRENDING

        use_chop_index = bool(self.thresholds.use_chop_index)
        chop_by_chop_index = bool(
            use_chop_index
            and ctx.chop_index is not None
            and ctx.chop_index >= self.thresholds.chop_index_high
        )
        if (
            ctx.adx14 <= self.thresholds.chop_adx_max
            or ctx.di_spread < self.thresholds.chop_di_spread_max
            or chop_by_chop_index
        ):
            return Regime.CHOPPY

        if (
            self.thresholds.adx_normal_low <= ctx.adx14 < self.thresholds.adx_normal_high
            and self.thresholds.atr_norm_low <= ctx.atr_norm <= self.thresholds.atr_norm_high
        ):
            return Regime.NORMAL

        return Regime.NORMAL

    def _directional_trending_regime(self, ctx: MarketContext) -> Optional[Regime]:
        """Return an opt-in directional trend when DI and EMA agree.

        Older callers only supplied ``di_spread`` and ``ema_slope``. In that
        case we keep emitting legacy TRENDING so existing dynamic-policy and
        selector configs retain their exact behaviour.
        """

        if (
            ctx.plus_di is None
            or ctx.minus_di is None
            or ctx.last_price is None
            or ctx.ema_value is None
            or ctx.ema_slope is None
        ):
            return None
        if ctx.last_price > ctx.ema_value and ctx.plus_di > ctx.minus_di and ctx.ema_slope > 0:
            return Regime.TRENDING_UP
        if ctx.last_price < ctx.ema_value and ctx.minus_di > ctx.plus_di and ctx.ema_slope < 0:
            return Regime.TRENDING_DOWN
        return None

    def update(self, ctx: Optional[MarketContext]) -> Regime:
        target = self.classify_once(ctx)
        now_ts = ctx.ts if ctx is not None else None

        if self._current_regime is None:
            self._current_regime = target
            if isinstance(now_ts, datetime):
                self._last_switch_ts = now_ts
            return target

        # Safety first: NO_TRADE should take effect immediately.
        if target == Regime.NO_TRADE:
            if self._current_regime != Regime.NO_TRADE:
                self._current_regime = Regime.NO_TRADE
                if isinstance(now_ts, datetime):
                    self._last_switch_ts = now_ts
            self._candidate_regime = None
            self._candidate_count = 0
            return self._current_regime

        if target == self._current_regime:
            self._candidate_regime = None
            self._candidate_count = 0
            return self._current_regime

        if self._candidate_regime == target:
            self._candidate_count += 1
        else:
            self._candidate_regime = target
            self._candidate_count = 1

        if self._candidate_count < max(1, int(self.thresholds.hold_bars)):
            return self._current_regime

        if not self._can_switch(now_ts):
            return self._current_regime

        self._current_regime = target
        self._candidate_regime = None
        self._candidate_count = 0
        if isinstance(now_ts, datetime):
            self._last_switch_ts = now_ts
        return self._current_regime

    def _can_switch(self, now_ts: Optional[datetime]) -> bool:
        hold_min = max(0, int(self.thresholds.min_regime_hold_minutes))
        if hold_min <= 0:
            return True
        if self._last_switch_ts is None:
            return True
        if not isinstance(now_ts, datetime):
            return False
        return now_ts >= self._last_switch_ts + timedelta(minutes=hold_min)


__all__ = [
    "CLASSIFIER_VERSION",
    "Regime",
    "RegimeThresholds",
    "RegimeClassifier",
]
