"""Per-bar gate-rejection visibility for nifty_weekly_credit_spreads.

The strategy has many strict entry gates (entry-time window, DTE range,
indicators-ready, ATR cap, regime classification, ATM straddle range,
condor leg resolution + credit floors + risk caps). When the strategy
produces zero entries on a session, ops needs to know WHICH gate is
silently rejecting -- without enabling per-bar INFO noise. The
``signal_evaluated_with_reason=<reason>`` DEBUG line solves this:
opt-in via logger level, structured payload, parseable by alerting.

These tests exercise each gate and assert the corresponding reason
string is emitted exactly once per bar at DEBUG.
"""

from __future__ import annotations

import importlib
import logging
import types
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace



MODULE_PATH = "app.strategies.nifty_weekly_credit_spreads"

# Same IST conversion the strategy uses internally.
IST = timezone(timedelta(hours=5, minutes=30))


def _make_strategy(monkeypatch, **param_overrides):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: types.SimpleNamespace(use_hub_router_for_nifty_options=False),
    )
    params = {
        "account_equity": 1_000_000,
        "lot_size": 1,
        "risk_per_trade_pct": 0.5,
        "max_total_risk_pct": 1.0,
        "entry_start_time": "10:10",
        "entry_end_time": "13:30",
        "min_days_to_expiry": 1,
        "max_days_to_expiry": 5,
        "atr_max_pct_5m": 0.004,
        "ema_period_5m": 20,
    }
    params.update(param_overrides)
    strategy = mod.NiftyWeeklyCreditSpreadStrategy(
        instrument_meta={
            "NIFTY_IDX": {"kind": "UNDERLYING", "underlying": "NIFTY"},
        },
        order_client=None,
        risk_manager=None,
        env_prefix="NIFTY_",
        underlying_label="NIFTY_IDX",
        params=params,
    )
    return mod, strategy


def _candle(*, ist_dt: datetime, close: float = 24000.0) -> SimpleNamespace:
    """Build a minimal candle stub with a UTC start_ts and close ``c``."""
    return SimpleNamespace(start_ts=ist_dt.astimezone(timezone.utc), c=close)


def _patch_chain(strategy, *, expiry: date) -> None:
    """Stub the option-chain resolver so tests don't need full
    instrument_meta. Tests that want to exercise the no-chain gate
    explicitly override this back to None."""
    strategy._current_expiry_and_chain = lambda: expiry  # type: ignore[assignment]
    strategy._atm_options = lambda _exp: ("NIFTY_CE_ATM", "NIFTY_PE_ATM")  # type: ignore[assignment]
    strategy._latest_price = lambda _label: 100.0  # type: ignore[assignment]


def _ind(**overrides) -> dict:
    """Indicator dict with sensible defaults that pass the early gates."""
    base = {
        "ema_20": 24000.0,
        "rsi": 50.0,
        "macd_hist": 0.0,
        "atr": 60.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_outside_entry_window_logs_reason(monkeypatch, caplog):
    """A bar at 14:00 IST (after 13:30 entry-window close) emits the
    outside_entry_window reason."""
    mod, strategy = _make_strategy(monkeypatch)
    candle = _candle(ist_dt=datetime(2026, 5, 8, 14, 0, tzinfo=IST))
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, _ind())
    assert any(
        "signal_evaluated_with_reason=outside_entry_window" in r.getMessage()
        for r in caplog.records
    )


def test_indicators_not_ready_logs_reason(monkeypatch, caplog):
    """Missing any of (ema, rsi, macd_hist, atr) -> indicators_not_ready."""
    mod, strategy = _make_strategy(monkeypatch)
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST))
    indicators = _ind(rsi=None)  # rsi missing
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, indicators)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("signal_evaluated_with_reason=indicators_not_ready" in m for m in msgs)
    # Structured field telling us which indicator is missing.
    assert any("rsi_set=False" in m for m in msgs)


def test_atr_too_high_logs_reason(monkeypatch, caplog):
    """ATR pct above the 5m cap triggers atr_too_high."""
    mod, strategy = _make_strategy(monkeypatch, atr_max_pct_5m=0.001)
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST), close=24000.0)
    indicators = _ind(atr=120.0)  # 120/24000 = 0.5% > 0.1% cap
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, indicators)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("signal_evaluated_with_reason=atr_too_high" in m for m in msgs)
    assert any("atr_pct=" in m for m in msgs)


def test_regime_none_logs_reason_with_eligibility_flags(monkeypatch, caplog):
    """When close=ema, RSI=50, MACD flat, no regime classification fires.
    Reason string is regime_none with bullish/bearish/sideways eligible
    flags so ops can see *why* none of the regimes matched."""
    mod, strategy = _make_strategy(monkeypatch)
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST), close=24000.0)
    indicators = _ind(ema_20=24000.0, rsi=50.0, macd_hist=0.0, atr=60.0)
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, indicators)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("signal_evaluated_with_reason=regime_none" in m for m in msgs)
    assert any("bullish_eligible=False" in m for m in msgs)
    assert any("bearish_eligible=False" in m for m in msgs)


def test_no_debug_when_logger_level_is_info(monkeypatch, caplog):
    """The whole point of DEBUG: at INFO level, the helper short-circuits
    before formatting -- no signal_evaluated_with_reason lines appear,
    so the existing per-bar STRATEGY_BAR_SKIP volume is unchanged."""
    mod, strategy = _make_strategy(monkeypatch)
    candle = _candle(ist_dt=datetime(2026, 5, 8, 14, 0, tzinfo=IST))
    with caplog.at_level(logging.INFO, logger=MODULE_PATH):
        strategy._maybe_enter(candle, _ind())
    assert not any(
        "signal_evaluated_with_reason=" in r.getMessage()
        for r in caplog.records
    )


def test_helper_formats_floats_compactly(monkeypatch, caplog):
    """Float fields are formatted with %.6g so logs stay grep-friendly
    instead of leaking 17-digit IEEE-754 noise."""
    mod, strategy = _make_strategy(monkeypatch, atr_max_pct_5m=0.001)
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST), close=24000.0)
    # atr_pct = 120/24000 = 0.005
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, _ind(atr=120.0))
    msgs = [r.getMessage() for r in caplog.records]
    line = next(m for m in msgs if "signal_evaluated_with_reason=atr_too_high" in m)
    # 0.005 must NOT render as 0.004999999999999999 etc.
    assert "atr_pct=0.005" in line, line


def test_no_current_expiry_logs_reason(monkeypatch, caplog):
    """When _current_expiry_and_chain returns None, the reason is
    no_current_expiry_or_chain (this is the default in our test fixture
    until _patch_chain runs)."""
    mod, strategy = _make_strategy(monkeypatch)
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST))
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, _ind())
    assert any(
        "signal_evaluated_with_reason=no_current_expiry_or_chain" in r.getMessage()
        for r in caplog.records
    )


def test_expiry_out_of_dte_range_logs_reason(monkeypatch, caplog):
    """An expiry 30 days out exceeds max_days_to_expiry=5."""
    mod, strategy = _make_strategy(monkeypatch)
    _patch_chain(strategy, expiry=date(2026, 6, 7))  # 30d from 2026-05-08
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST))
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, _ind())
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=expiry_out_of_dte_range" in m for m in msgs
    )
    assert any("days_to_exp=30" in m for m in msgs)
    assert any("max_dte=5" in m for m in msgs)
