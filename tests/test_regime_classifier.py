from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.strategies.adaptive.market_context import MarketContext
from app.strategies.adaptive.regime import Regime, RegimeClassifier, RegimeThresholds


def _ctx(
    *,
    ts: datetime,
    atr14: float = 1.0,
    atr_norm: float = 1.0,
    adx14: float = 20.0,
    di_spread: float = 5.0,
    ema_slope: float = 0.01,
    gap_ratio: float = 0.0,
) -> MarketContext:
    return MarketContext(
        ts=ts,
        last_price=100.0,
        atr14=atr14,
        atr_norm=atr_norm,
        adx14=adx14,
        di_spread=di_spread,
        ema_slope=ema_slope,
        gap_ratio=gap_ratio,
    )


def test_regime_trending_when_adx_and_di_high():
    clf = RegimeClassifier(RegimeThresholds(hold_bars=1))
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.1, adx14=30.0, di_spread=10.0))
    assert reg == Regime.TRENDING


def test_regime_choppy_when_adx_low():
    clf = RegimeClassifier(RegimeThresholds(hold_bars=1))
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.0, adx14=14.0, di_spread=4.0))
    assert reg == Regime.CHOPPY


def test_regime_high_vol_when_atr_norm_high_and_adx_not_trending():
    clf = RegimeClassifier(RegimeThresholds(hold_bars=1))
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.8, adx14=18.0, di_spread=3.0))
    assert reg == Regime.HIGH_VOL


def test_regime_high_vol_when_gap_spike_and_secondary_atr_norm():
    clf = RegimeClassifier(
        RegimeThresholds(
            hold_bars=1,
            atr_norm_high=1.6,
            atr_norm_secondary=1.3,
            gap_ratio_spike=1.0,
        )
    )
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(
        _ctx(
            ts=ts,
            atr_norm=1.35,
            adx14=30.0,
            di_spread=10.0,
            gap_ratio=1.2,
        )
    )
    assert reg == Regime.HIGH_VOL


def test_regime_normal_when_adx_and_atr_norm_within_band():
    clf = RegimeClassifier(
        RegimeThresholds(
            hold_bars=1,
            adx_trend=25.0,
            adx_normal_low=18.0,
            adx_normal_high=25.0,
            atr_norm_low=0.8,
            atr_norm_high=1.6,
        )
    )
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.2, adx14=22.0, di_spread=5.0))
    assert reg == Regime.NORMAL


def test_regime_choppy_when_di_spread_is_low():
    clf = RegimeClassifier(
        RegimeThresholds(
            hold_bars=1,
            chop_di_spread_max=3.0,
            chop_adx_max=17.0,
        )
    )
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.1, adx14=22.0, di_spread=2.5))
    assert reg == Regime.CHOPPY


def test_regime_trending_requires_slope_threshold():
    clf = RegimeClassifier(
        RegimeThresholds(
            hold_bars=1,
            adx_trend=25.0,
            di_spread_trend=5.0,
            ema_slope_trend_min=0.002,
        )
    )
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    reg = clf.update(_ctx(ts=ts, atr_norm=1.1, adx14=30.0, di_spread=10.0, ema_slope=0.001))
    assert reg == Regime.NORMAL


def test_regime_hysteresis_requires_hold_bars():
    clf = RegimeClassifier(RegimeThresholds(hold_bars=3))
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert clf.update(_ctx(ts=ts, atr_norm=1.0, adx14=20.0, di_spread=3.0)) == Regime.NORMAL

    assert clf.update(_ctx(ts=ts + timedelta(minutes=1), atr_norm=1.0, adx14=30.0, di_spread=12.0)) == Regime.NORMAL
    assert clf.update(_ctx(ts=ts + timedelta(minutes=2), atr_norm=1.0, adx14=30.0, di_spread=12.0)) == Regime.NORMAL
    assert clf.update(_ctx(ts=ts + timedelta(minutes=3), atr_norm=1.0, adx14=30.0, di_spread=12.0)) == Regime.TRENDING


def test_regime_returns_no_trade_when_context_invalid_nan_inf():
    clf = RegimeClassifier(RegimeThresholds(hold_bars=1))
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    invalid_nan = _ctx(ts=ts, adx14=float("nan"))
    invalid_inf = _ctx(ts=ts + timedelta(minutes=1), atr14=float("inf"))
    assert clf.update(invalid_nan) == Regime.NO_TRADE
    assert clf.update(invalid_inf) == Regime.NO_TRADE


def test_regime_min_hold_time_blocks_switch():
    clf = RegimeClassifier(
        RegimeThresholds(
            hold_bars=1,
            min_regime_hold_minutes=5,
        )
    )
    ts = datetime(2025, 1, 1, 9, 30, tzinfo=timezone.utc)
    assert clf.update(_ctx(ts=ts, atr_norm=1.0, adx14=30.0, di_spread=12.0)) == Regime.TRENDING
    assert clf.update(_ctx(ts=ts + timedelta(minutes=2), atr_norm=1.0, adx14=14.0, di_spread=2.0)) == Regime.TRENDING
    assert clf.update(_ctx(ts=ts + timedelta(minutes=6), atr_norm=1.0, adx14=14.0, di_spread=2.0)) == Regime.CHOPPY
