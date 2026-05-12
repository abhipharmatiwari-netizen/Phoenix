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


# ---------------------------------------------------------------------------
# Codex P2 follow-up tests (PR #208 round 2)
# ---------------------------------------------------------------------------

def test_open_vertical_legs_unresolved_logs_reason(monkeypatch, caplog):
    """Codex P2 #1: _open_vertical previously silently returned when option
    legs couldn't be resolved. Now emits vertical_legs_unresolved with the
    spread_type and which leg(s) are missing."""
    mod, strategy = _make_strategy(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._open_vertical(
            spread_type="PUT_SPREAD",
            short_label=None,
            long_label="NIFTY_PE_19700",
            width=300.0,
            expiry=date(2026, 5, 12),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=vertical_legs_unresolved" in m for m in msgs
    )
    assert any("spread_type=PUT_SPREAD" in m for m in msgs)
    assert any("short=False" in m for m in msgs)


def test_open_vertical_credit_estimate_failed_logs_reason(monkeypatch, caplog):
    """Codex P2 #1: _estimate_credit returns None credit when latest_price
    is missing. The vertical_credit_estimate_failed reason fires."""
    mod, strategy = _make_strategy(monkeypatch)
    # _latest_price returns None -> _estimate_credit returns (None, 0, 0, 0)
    strategy._latest_price = lambda _label: None  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._open_vertical(
            spread_type="CALL_SPREAD",
            short_label="NIFTY_CE_24500",
            long_label="NIFTY_CE_24800",
            width=300.0,
            expiry=date(2026, 5, 12),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=vertical_credit_estimate_failed" in m
        for m in msgs
    )


def test_open_vertical_credit_below_min_logs_reason(monkeypatch, caplog):
    """Codex P2 #1: a vertical with credit below the floor logs
    vertical_credit_below_min with the actual + minimum values."""
    mod, strategy = _make_strategy(
        monkeypatch, min_credit_pct_of_width=0.5
    )
    # Make _estimate_credit return a low credit (10) on a wide spread (300).
    # 10 < 0.5 * 300 = 150 -> below min.
    strategy._estimate_credit = lambda *_a, **_kw: (10.0, 300.0, 1, 290.0)  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._open_vertical(
            spread_type="PUT_SPREAD",
            short_label="NIFTY_PE_19800",
            long_label="NIFTY_PE_19500",
            width=300.0,
            expiry=date(2026, 5, 12),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=vertical_credit_below_min" in m for m in msgs
    )
    assert any("credit=10" in m for m in msgs)


def test_open_vertical_risk_disallowed_logs_reason(monkeypatch, caplog):
    """Codex P2 #1: when _risk_allows returns False (per-trade or total
    risk cap exceeded, or max_open_spreads hit), vertical_risk_disallowed
    fires with the relevant cap fields."""
    # account_equity=1M, risk_per_trade_pct=0.0001 -> per-trade cap = 100.
    # max_loss=200 > 100 -> risk_allows False.
    mod, strategy = _make_strategy(monkeypatch, risk_per_trade_pct=0.0001)
    strategy._estimate_credit = lambda *_a, **_kw: (100.0, 300.0, 1, 200.0)  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._open_vertical(
            spread_type="CALL_SPREAD",
            short_label="NIFTY_CE_24500",
            long_label="NIFTY_CE_24800",
            width=300.0,
            expiry=date(2026, 5, 12),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=vertical_risk_disallowed" in m for m in msgs
    )
    assert any("max_loss_rupees=200" in m for m in msgs)


def test_atm_quote_unavailable_distinct_from_out_of_range(monkeypatch, caplog):
    """Codex round-3 #3: when _latest_price returns None for either ATM
    leg, the reason is atm_quote_unavailable -- NOT
    atm_straddle_pct_out_of_range with a phantom 0.0 leg price.
    Otherwise ops would tune the ratio bands when the real blocker is
    the LTP feed."""
    mod, strategy = _make_strategy(monkeypatch)
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    # Override _latest_price to return None (data unavailable).
    strategy._latest_price = lambda _label: None  # type: ignore[assignment]
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST), close=24000.0)
    # Bullish-eligible setup.
    indicators = _ind(ema_20=23900.0, rsi=60.0, macd_hist=2.0)
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, indicators)
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=atm_quote_unavailable" in m for m in msgs
    ), msgs
    # Must NOT mis-report as out-of-range.
    assert not any(
        "signal_evaluated_with_reason=atm_straddle_pct_out_of_range" in m
        for m in msgs
    )


def test_phoenix_debug_loggers_env_var_sets_per_logger_level(monkeypatch):
    """Codex round-3 #2: the docstring documents
    PHOENIX_DEBUG_LOGGERS as the per-module DEBUG knob. The handler in
    app.main._configure_logging must actually flip the named loggers to
    DEBUG without affecting the global root level."""
    import importlib

    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv(
        "PHOENIX_DEBUG_LOGGERS",
        "app.strategies.nifty_weekly_credit_spreads,app.orders.order_lifecycle",
    )
    main = importlib.import_module("app.main")
    main._configure_logging()

    assert (
        logging.getLogger("app.strategies.nifty_weekly_credit_spreads").level
        == logging.DEBUG
    )
    assert (
        logging.getLogger("app.orders.order_lifecycle").level == logging.DEBUG
    )
    # Root level stays at the global LOG_LEVEL.
    assert logging.getLogger().level == logging.INFO


def test_no_entry_gates_passed_log_when_downstream_rejects(monkeypatch, caplog):
    """Codex P2 #2: a bar that passes the top-level gates but then gets
    rejected inside the spread builder must NOT emit an
    'entry_gates_passed' / 'spread_opened' line. Only the downstream
    rejection reason should appear."""
    mod, strategy = _make_strategy(
        monkeypatch,
        atm_straddle_min_pct=0.014,
        atm_straddle_max_pct=0.03,
        min_credit_pct_of_width=0.5,
    )
    _patch_chain(strategy, expiry=date(2026, 5, 12))
    # ATM straddle range is [1.4%, 3.0%]; close=24000 -> straddle in [336, 720].
    # Set _latest_price to 200 -> straddle = 400 / 24000 = 1.67% in range.
    strategy._latest_price = lambda _label: 200.0  # type: ignore[assignment]
    # Force the downstream condor builder to reject on credit floor.
    strategy._estimate_credit = lambda *_a, **_kw: (10.0, 300.0, 1, 290.0)  # type: ignore[assignment]
    strategy._find_option_by_strike = lambda *_a, **_kw: "NIFTY_OPT"  # type: ignore[assignment]
    candle = _candle(ist_dt=datetime(2026, 5, 8, 11, 0, tzinfo=IST), close=24000.0)
    # Push lots of flat closes so sideways regime gate passes.
    strategy.last_5m_closes = [24000.0] * 20
    indicators = _ind(rsi=50.0, macd_hist=0.0)  # neutral -> sideways eligible
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._maybe_enter(candle, indicators)
    msgs = [r.getMessage() for r in caplog.records]
    # No misleading "gates passed" line.
    assert not any(
        "signal_evaluated_with_reason=entry_gates_passed" in m for m in msgs
    )
    # No spurious "opened" line either since the entry was rejected.
    assert not any(
        "signal_evaluated_with_reason=spread_opened" in m for m in msgs
    )
    # The downstream rejection reason DID fire.
    assert any(
        "signal_evaluated_with_reason=condor_put_credit_below_min" in m
        for m in msgs
    )


def test_enter_spread_does_not_emit_spread_opened_directly(monkeypatch, caplog):
    """Codex round-3 #1: _enter_spread is called twice for a condor (PUT,
    then CALL). It must NOT emit spread_opened on its own -- otherwise a
    condor whose CALL side fails and rolls back the PUT side would have
    a false-positive 'opened' log for the PUT leg pair."""
    mod, strategy = _make_strategy(monkeypatch)
    # Stub everything _enter_spread needs to claim success on both legs.
    strategy._latest_price = lambda _label: 100.0  # type: ignore[assignment]
    strategy._strike_from_label = lambda _label: 24000.0  # type: ignore[assignment]
    strategy._resolve_order_identity = lambda _label: ("X", "NFO", "1")  # type: ignore[assignment]
    strategy._remember_order_identity = lambda _label: None  # type: ignore[assignment]
    strategy._reset_exit_retry_state = lambda _id: None  # type: ignore[assignment]
    strategy._sync_spread_state_to_risk_manager = lambda _s: None  # type: ignore[assignment]
    strategy._spread_leg_strategy_context = lambda *a, **kw: {}  # type: ignore[assignment]

    accepted = SimpleNamespace(status="ACCEPTED")
    strategy._place_order = lambda **kw: accepted  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        ok = strategy._enter_spread(
            "PUT_SPREAD", "P_SHORT", "P_LONG", credit=50.0, width_pts=300.0,
            expiry=date(2026, 5, 12),
        )
    # Issue #262: _enter_spread returns the spread_id (truthy str) on
    # success / None on failure.
    assert ok
    msgs = [r.getMessage() for r in caplog.records]
    # spread_opened MUST NOT appear from _enter_spread itself; only the
    # higher-level builder emits it after the full structure is in place.
    assert not any(
        "signal_evaluated_with_reason=spread_opened" in m for m in msgs
    ), msgs


def test_open_vertical_emits_spread_opened_on_full_success(monkeypatch, caplog):
    """Codex round-3 #1 counterpart: when a vertical spread is fully
    placed via _open_vertical, it emits a single spread_opened log
    (because both legs of a vertical = the full structure)."""
    mod, strategy = _make_strategy(monkeypatch)
    # Force _enter_spread to succeed (skip its plumbing).
    strategy._enter_spread = lambda *_a, **_kw: True  # type: ignore[assignment]
    strategy._estimate_credit = lambda *_a, **_kw: (100.0, 300.0, 1, 200.0)  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        strategy._open_vertical(
            spread_type="PUT_SPREAD",
            short_label="NIFTY_PE_24000",
            long_label="NIFTY_PE_23700",
            width=300.0,
            expiry=date(2026, 5, 12),
        )
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=spread_opened" in m
        and "spread_type=PUT_SPREAD" in m
        for m in msgs
    ), msgs


def test_enter_spread_short_leg_rejected_logs_reason(monkeypatch, caplog):
    """Codex round-3 #4: when the broker rejects the short-leg entry
    order, _enter_spread emits entry_short_leg_order_rejected with the
    response status/message. Without this the bar would silently
    disappear from the diagnostic loop."""
    mod, strategy = _make_strategy(monkeypatch)
    strategy._latest_price = lambda _label: 100.0  # type: ignore[assignment]
    strategy._strike_from_label = lambda _label: 24000.0  # type: ignore[assignment]
    strategy._resolve_order_identity = lambda _label: ("X", "NFO", "1")  # type: ignore[assignment]
    strategy._remember_order_identity = lambda _label: None  # type: ignore[assignment]
    strategy._spread_leg_strategy_context = lambda *a, **kw: {}  # type: ignore[assignment]
    rejected = SimpleNamespace(status="REJECTED", message="margin shortfall")
    strategy._place_order = lambda **kw: rejected  # type: ignore[assignment]

    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        ok = strategy._enter_spread(
            "PUT_SPREAD", "P_SHORT", "P_LONG", credit=50.0, width_pts=300.0,
            expiry=date(2026, 5, 12),
        )
    # Issue #262: _enter_spread returns None on failure (was False before).
    assert ok is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=entry_short_leg_order_rejected" in m
        for m in msgs
    )
    assert any("response_status=REJECTED" in m for m in msgs)


def test_enter_spread_leg_price_unavailable_logs_reason(monkeypatch, caplog):
    """Codex round-3 #4: when _latest_price returns None at order-placement
    time (LTP feed gap between gate and submit), the reason is
    entry_leg_price_unavailable -- not silent."""
    mod, strategy = _make_strategy(monkeypatch)
    strategy._latest_price = lambda _label: None  # type: ignore[assignment]
    with caplog.at_level(logging.DEBUG, logger=MODULE_PATH):
        ok = strategy._enter_spread(
            "PUT_SPREAD", "P_SHORT", "P_LONG", credit=50.0, width_pts=300.0,
            expiry=date(2026, 5, 12),
        )
    # Issue #262 round-2: every failure path of _enter_spread now returns
    # None (was bool False before — the round-1 sweep missed this branch).
    assert ok is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "signal_evaluated_with_reason=entry_leg_price_unavailable" in m
        for m in msgs
    )
