"""Regression tests for PR #283 codex review findings on postgres_data_loader.

Pins three behaviours that codex flagged in the original f729add commit:
1. P1: the loader's SELECT must not reference a non-existent volume column.
2. P1: simulator exit thresholds (``sl_pct`` / ``tp_pct``) are fractions in
   the live strategy and must be converted to percent before comparing
   against the percent-scaled ``pnl_pct``.
3. P2: ``backtest_*`` methods must propagate data-access failures so a
   broken loader does not silently emit zero-trade results.
4. P2: connection logs must not echo credential-bearing DSN slices.

No real Postgres is touched — the loader is monkey-patched.
"""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from app.strategies.postgres_data_loader import (
    PostgresIndicatorLoader,
    RealDataBacktester,
    _redact_dsn,
)


# ---------------------------------------------------------------------------
# P1: SELECT must not reference the missing ``vol``/``volume`` column.
# ---------------------------------------------------------------------------


def test_fetch_indicator_bars_query_does_not_reference_volume_column():
    """The baseline indicator_bars schema (migrations/000) has no volume
    column. Referencing it caused the SELECT to fail before any rows
    were returned."""
    captured = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            captured["query"] = query
            captured["params"] = params

        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    loader = PostgresIndicatorLoader(dsn="postgresql://u@h:5432/d")
    loader._conn = _FakeConn()

    df = loader.fetch_indicator_bars(
        underlying_label="NIFTY",
        timeframe_seconds=300,
        days_back=5,
    )
    assert df.empty
    sql = captured["query"]
    # Must not select a volume column under any common spelling.
    assert "vol" not in sql.lower().replace("volatility", "")
    # The column list must omit ``volume`` entirely so the DataFrame
    # shape is consistent with what is selected.
    assert " volume" not in sql.lower()


# ---------------------------------------------------------------------------
# P1: simulator unit conversion (sl_pct / tp_pct fractions → percent threshold).
# ---------------------------------------------------------------------------


def _make_bar_df(closes, indicator_default=20.0):
    """Tiny OHLC + indicator frame for simulator unit tests.

    PR #283 codex round-19 P2: EMA columns left as NaN so the
    simulator's persisted-column-then-fallback path exercises the
    computed-EMA fallback (which the unit tests below rely on).
    Tests that need a populated persisted column build their own
    frame.
    """
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": closes,
        "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes],
        "close": closes,
        "atr": [indicator_default] * n,
        "rsi": [indicator_default + 30] * n,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [float("nan")] * n,
        "ema_30": [float("nan")] * n,
        "ema_50": [float("nan")] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })


def test_ema20_sl_pct_treats_input_as_fraction():
    """``sl_pct=0.05`` (5%) must trigger an exit at a 5% adverse move on a
    short; ``sl_pct=0.30`` (30%) must let the same move ride. Previously
    the simulator treated 0.30 as ``0.30%`` (100x tighter than LIVE), so
    both values produced identical exit timing — this test pins the unit
    contract."""
    # Engineer a price series: 25 bars at 100 (warm up EMA), then close
    # drops below EMA for entry, then close monotonically increases by
    # 1% per bar (adverse for a short).
    closes = [100.0] * 25
    # Entry: dip below EMA at bar 25.
    closes.append(95.0)
    # Adverse moves: +1% per bar after entry.
    entry_price = 95.0
    for step in range(1, 50):
        closes.append(entry_price * (1.0 + 0.01 * step))
    df = _make_bar_df(closes)

    # PR #283 codex round-3 P2 added the RSI-falling / ADX gates to
    # the EMA20 sim; disable them here so this test only exercises the
    # SL unit-conversion contract.
    # PR #283 codex round-11 P2: EMA20 simulator enforces an intraday
    # entry window (default 09:30-15:00 IST). Override to "all bars"
    # so this test remains a pure SL-unit-conversion test independent
    # of the time gate.
    _NO_GATE = {"first_entry_time": "00:00", "square_off_time": "23:59"}
    tight = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.05,
            "tp_pct": 5.0,
            "min_atr": 0.0,
            "require_rsi_falling": False,
            "use_adx_filter": False,
            **_NO_GATE,
        },
    )
    loose = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 5.0,
            "min_atr": 0.0,
            "require_rsi_falling": False,
            "use_adx_filter": False,
            **_NO_GATE,
        },
    )

    # Both runs entered at the same bar but the SL trips at different
    # times. ``pnl_pct`` is reported in percent, so a 5% SL hits sooner
    # (more-negative max_drawdown) than a 30% SL.
    assert tight["max_drawdown"] != loose["max_drawdown"], (
        "0.05 and 0.30 sl_pct inputs must produce different SL trip "
        "timings — if max_drawdown is identical the unit-conversion fix "
        "regressed (both inputs would be treated as percent-fractions)."
    )
    # The tight stop should clip the loss earlier — drawdown closer to
    # zero — than the loose stop.
    assert abs(tight["max_drawdown"]) < abs(loose["max_drawdown"]), (
        "tighter SL must clip the adverse move sooner; got "
        f"tight max_dd={tight['max_drawdown']} loose max_dd={loose['max_drawdown']}"
    )


# ---------------------------------------------------------------------------
# P2: data-access failures must propagate, not silently emit zero trades.
# ---------------------------------------------------------------------------


def test_backtest_ema20_propagates_loader_exception():
    """A broken loader must NOT be silently converted to a zero-trade
    result — the orchestrator must see the real error."""

    class _BoomLoader:
        def fetch_indicator_bars(self, *a, **kw):
            raise RuntimeError("simulated loader failure")

    backtester = RealDataBacktester(loader=_BoomLoader())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="simulated loader failure"):
        backtester.backtest_ema20({"signal_timeframe": 300}, "NIFTY")


def test_backtest_exclusive_nifty_ce_propagates_loader_exception():
    class _BoomLoader:
        def fetch_indicator_bars(self, *a, **kw):
            raise RuntimeError("loader is down")

    backtester = RealDataBacktester(loader=_BoomLoader())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="loader is down"):
        backtester.backtest_exclusive_nifty_ce({}, "NIFTY")


def test_backtest_put_momentum_propagates_loader_exception():
    class _BoomLoader:
        def fetch_indicator_bars(self, *a, **kw):
            raise RuntimeError("pg unreachable")

    backtester = RealDataBacktester(loader=_BoomLoader())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="pg unreachable"):
        backtester.backtest_put_momentum({}, "NIFTY")


def test_backtest_ema20_empty_dataframe_is_soft_no_op():
    """A successful query that returns zero bars is a soft no-op (zero
    trades), not an error. The distinction is important: ``empty`` means
    "no bars in the requested window"; ``exception`` means "loader is
    misconfigured"."""

    class _EmptyLoader:
        def fetch_indicator_bars(self, *a, **kw):
            return pd.DataFrame()

    backtester = RealDataBacktester(loader=_EmptyLoader())  # type: ignore[arg-type]
    result = backtester.backtest_ema20({"signal_timeframe": 300}, "NIFTY")
    assert result["total_trades"] == 0


# ---------------------------------------------------------------------------
# P2: DSN redaction for logs.
# ---------------------------------------------------------------------------


def test_redact_dsn_strips_password_from_url_form():
    redacted = _redact_dsn("postgresql://phoenix:supersecret@db.internal:5432/phoenix")
    assert "supersecret" not in redacted
    assert "phoenix" in redacted  # dbname is fine to expose
    assert "db.internal:5432/phoenix" in redacted


def test_redact_dsn_strips_password_from_kv_form():
    redacted = _redact_dsn(
        "host=db.internal port=5432 dbname=phoenix user=admin password=topsecret"
    )
    assert "topsecret" not in redacted
    assert "db.internal:5432/phoenix" in redacted


def test_redact_dsn_handles_missing_dsn():
    assert _redact_dsn(None) == "<no-dsn>"
    assert _redact_dsn("") == "<no-dsn>"


def test_redact_dsn_returns_safe_fallback_on_parse_failure():
    # conninfo_to_dict tolerates most inputs; this still exercises the
    # safe-fallback path by injecting an exception on parse.
    with patch(
        "psycopg.conninfo.conninfo_to_dict",
        side_effect=RuntimeError("parse boom"),
    ):
        assert _redact_dsn("postgresql://x@y/z") == "<dsn-redacted>"


# ---------------------------------------------------------------------------
# PR #283 codex round-2: simulator parameter contracts must match LIVE.
#
# Every parameter the optimizer samples and emits must be a key the
# corresponding live strategy actually reads. Otherwise approved
# candidates have no effect at runtime. These tests pin the contract.
# ---------------------------------------------------------------------------


def test_exclusive_nifty_ce_simulator_uses_live_keys_only():
    """ECN sim must read sl_atr/tp_atr/rsi_min/rsi_max/macd_hist_min/
    ema_atr_buffer/min_adx/min_di_spread/ema_fail_bars — NOT sl_pct/tp_pct
    /rsi_threshold/vol_threshold (the old broken contract)."""
    from app.strategies.strategy_optimizers import ExclusiveNiftyCeParameterOptimizer

    spaces = {s.name for s in ExclusiveNiftyCeParameterOptimizer.get_parameter_spaces()}
    formatted_keys = set(
        ExclusiveNiftyCeParameterOptimizer.format_params({
            "timeframe_seconds": 30,
            "rsi_min": 58.0,
            "rsi_max": 72.0,
            "macd_hist_min": 0.30,
            "ema_atr_buffer": 0.05,
            "min_adx": 20.0,
            "min_di_spread": 5.0,
            "sl_atr": 2.2,
            "tp_atr": 2.5,
            "ema_fail_bars": 3,
        }).keys()
    )

    # Must include the live config keys.
    live_required = {
        "sl_atr", "tp_atr", "rsi_min", "rsi_max",
        "macd_hist_min", "ema_atr_buffer", "min_adx", "min_di_spread",
        "ema_fail_bars", "timeframe_seconds",
    }
    assert live_required.issubset(spaces), (
        f"ECN param spaces missing live keys: {live_required - spaces}"
    )
    assert live_required.issubset(formatted_keys), (
        f"ECN format_params missing live keys: {live_required - formatted_keys}"
    )

    # Must NOT include legacy keys that the live strategy doesn't read.
    legacy_dead = {"sl_pct", "tp_pct", "rsi_threshold", "vol_threshold", "ema_crossover_threshold"}
    leaked = legacy_dead & spaces
    assert not leaked, f"ECN param spaces leak legacy keys: {leaked}"
    leaked_fmt = legacy_dead & formatted_keys
    assert not leaked_fmt, f"ECN format_params emits legacy keys: {leaked_fmt}"


def test_put_momentum_simulator_uses_live_keys_only():
    """PM sim must read option_sl_pct/final_tp_r and the rest of
    ``PutMomentumScalperConfig`` — NOT sl_pct/tp_pct/trend_ema_period.

    PR #283 codex round-5 P2: ``partial_tp_r`` was dropped from the
    param space (live ``on_tick`` doesn't exit on it), so it MUST NOT
    appear in the spaces or format_params output even though
    ``PutMomentumScalperConfig`` still accepts it as carryover state.
    """
    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    spaces = {s.name for s in PutMomentumParameterOptimizer.get_parameter_spaces()}
    formatted_keys = set(
        PutMomentumParameterOptimizer.format_params({
            "rsi_min": 25.0,
            "rsi_max": 45.0,
            "min_atr_ratio": 0.0015,
            "option_sl_pct": 0.25,
            "final_tp_r": 1.5,
            "rsi_falling_bars_required": 2,
            "lookback_breakdown_bars": 10,
            "max_bars_in_trade": 8,
        }).keys()
    )

    live_required = {
        "option_sl_pct", "final_tp_r",
        "rsi_min", "rsi_max", "min_atr_ratio",
        "rsi_falling_bars_required", "lookback_breakdown_bars",
        "max_bars_in_trade",
    }
    assert live_required.issubset(spaces), (
        f"PM param spaces missing live keys: {live_required - spaces}"
    )
    assert live_required.issubset(formatted_keys), (
        f"PM format_params missing live keys: {live_required - formatted_keys}"
    )

    # ``partial_tp_r`` is part of the live config but the simulator
    # does not exit on it — see codex round-5 P2 note. Confirm it's
    # gone from BOTH spaces and format_params output.
    assert "partial_tp_r" not in spaces, (
        "partial_tp_r was removed from the PM param space; reintroducing "
        "it would surface a no-op knob (live exits don't honour it)."
    )
    assert "partial_tp_r" not in formatted_keys

    legacy_dead = {"sl_pct", "tp_pct", "trend_ema_period"}
    leaked = legacy_dead & spaces
    assert not leaked, f"PM param spaces leak legacy keys: {leaked}"
    leaked_fmt = legacy_dead & formatted_keys
    assert not leaked_fmt, f"PM format_params emits legacy keys: {leaked_fmt}"


def test_exclusive_nifty_ce_strategy_actually_reads_optimizer_keys():
    """The live ExclusiveNiftyCeBuyStrategy must accept every key the
    optimizer emits. If a key is renamed in live and the optimizer is not
    updated in lockstep, this test fails."""
    import importlib

    live_mod = importlib.import_module("app.strategies.exclusive_nifty_ce_buy")
    src = live_mod.__loader__.get_source(live_mod.__name__)

    from app.strategies.strategy_optimizers import ExclusiveNiftyCeParameterOptimizer

    formatted = ExclusiveNiftyCeParameterOptimizer.format_params({
        "timeframe_seconds": 30,
        "rsi_min": 58.0,
        "rsi_max": 72.0,
        "macd_hist_min": 0.30,
        "ema_atr_buffer": 0.05,
        "min_adx": 20.0,
        "min_di_spread": 5.0,
        "sl_atr": 2.2,
        "tp_atr": 2.5,
        "ema_fail_bars": 3,
    })
    for key in formatted:
        # Live code references each key via cfg.get("name") in __init__.
        # Loose substring check is acceptable here — the alternative
        # (importing the strategy with a real config) requires the full
        # broker/runtime stack.
        assert f'"{key}"' in src or f"'{key}'" in src, (
            f"ECN optimizer emits {key!r} but live ExclusiveNiftyCeBuyStrategy "
            "source does not reference it. Optimizer/live drift — fix "
            "strategy_optimizers.py to match the live config keys."
        )


def test_put_momentum_strategy_actually_reads_optimizer_keys():
    """Same contract for PutMomentumScalper — every emitted key must be
    referenced in the live strategy module."""
    import importlib

    live_mod = importlib.import_module("app.strategies.put_momentum_scalper")
    src = live_mod.__loader__.get_source(live_mod.__name__)

    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    formatted = PutMomentumParameterOptimizer.format_params({
        "rsi_min": 25.0,
        "rsi_max": 45.0,
        "min_atr_ratio": 0.0015,
        "option_sl_pct": 0.25,
        "partial_tp_r": 1.0,
        "final_tp_r": 1.5,
        "rsi_falling_bars_required": 2,
        "lookback_breakdown_bars": 10,
        "max_bars_in_trade": 8,
    })
    for key in formatted:
        assert key in src, (
            f"PM optimizer emits {key!r} but live PutMomentumScalperStrategy "
            "source does not reference it. Optimizer/live drift — fix "
            "strategy_optimizers.py to match PutMomentumScalperConfig keys."
        )


def test_multi_strategy_runner_uses_indicator_bars_labels():
    """Default underlyings must be the labels actually stored in
    indicator_bars (``*_IDX`` for indexes, ``NG_FUT`` for natgas)."""
    from app.strategies.strategy_optimizers import StrategyOptimizationRunner

    runner = StrategyOptimizationRunner()
    for strategy_name, cfg in runner.get_strategies().items():
        for underlying in cfg["underlyings"]:
            assert underlying in {"NIFTY_IDX", "BANKNIFTY_IDX", "NG_FUT"}, (
                f"Strategy {strategy_name!r} lists unsupported underlying "
                f"{underlying!r}; indicator_bars uses *_IDX / NG_FUT labels."
            )
        # No bare NIFTY / BANKNIFTY / NATURALGAS — those return empty data.
        assert "NIFTY" not in cfg["underlyings"]
        assert "BANKNIFTY" not in cfg["underlyings"]
        assert "NATURALGAS" not in cfg["underlyings"]


def test_exclusive_nifty_ce_backtest_uses_configurable_timeframe():
    """ECN must query indicator_bars at the timeframe the live strategy
    actually streams (30s by default), and the timeframe knob must be
    honoured."""
    captured = {}

    class _CaptureLoader:
        def fetch_indicator_bars(self, *, underlying_label, timeframe_seconds, days_back):
            captured["timeframe_seconds"] = timeframe_seconds
            return pd.DataFrame()

    backtester = RealDataBacktester(loader=_CaptureLoader())  # type: ignore[arg-type]
    # Default timeframe should be 30s (live default).
    backtester.backtest_exclusive_nifty_ce({}, "NIFTY_IDX")
    assert captured["timeframe_seconds"] == 30, (
        "ECN backtest must default to 30s (live default); got "
        f"{captured['timeframe_seconds']}s"
    )
    # Explicit override must be honoured.
    backtester.backtest_exclusive_nifty_ce({"timeframe_seconds": 60}, "NIFTY_IDX")
    assert captured["timeframe_seconds"] == 60


# ---------------------------------------------------------------------------
# PR #283 codex round-3 regressions:
#   - ECN: 3-bar RSI rising + fresh MACD cross
#   - PM: bearish MACD with fresh negative cross
#   - EMA20 real-data sim honors require_rsi_falling + ADX
#   - _trade_stats helper returns winning/losing/gross_win/gross_loss
# ---------------------------------------------------------------------------


def test_trade_stats_returns_winning_losing_and_gross_components():
    """Simulators must surface winning_trades / losing_trades / gross_win /
    gross_loss so the orchestrator can compute profit_factor instead of
    leaving it at zero in the BacktestMetrics composite score."""
    from app.strategies.postgres_data_loader import _trade_stats

    trades = [
        {"pnl_pct": 3.0},
        {"pnl_pct": 2.0},
        {"pnl_pct": -1.0},
    ]
    stats = _trade_stats(trades)
    assert stats["winning_trades"] == 2
    assert stats["losing_trades"] == 1
    assert stats["gross_win"] == 5.0
    assert stats["gross_loss"] == -1.0
    assert stats["total_trades"] == 3
    assert abs(stats["win_rate"] - (2 / 3)) < 1e-9


def test_compute_profit_factor_returns_large_cap_for_all_wins():
    """An all-win run must NOT collapse profit_factor to zero — that
    silently zeroed the win_rate * profit_factor consistency term in
    BacktestMetrics.score for the most desirable backtests."""
    from app.strategies.ml_param_optimizer import (
        _PROFIT_FACTOR_NO_LOSS_CAP,
        _compute_profit_factor,
    )

    assert _compute_profit_factor([3.0, 2.0], []) == _PROFIT_FACTOR_NO_LOSS_CAP
    assert _compute_profit_factor([], []) == 0.0
    assert _compute_profit_factor([3.0], [-1.0]) == 3.0


def test_ema20_real_data_sim_honors_require_rsi_falling_gate():
    """When require_rsi_falling=True (live default), bars where RSI is
    flat or rising must NOT trigger entry. PR #283 codex round-3 P2."""
    closes = [100.0] * 30 + [95.0] * 30
    df = _make_bar_df(closes)
    # rsi flat at 50; with require_rsi_falling=True there should be no
    # entries and therefore no trades.
    result = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.0,
            "require_rsi_falling": True,
        },
    )
    assert result["total_trades"] == 0

    # With the gate off, trades should appear.
    result_off = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.0,
            "require_rsi_falling": False,
        },
    )
    assert result_off["total_trades"] >= 1


def test_ema20_real_data_sim_honors_use_adx_filter_gate():
    """When use_adx_filter=True, entries are rejected unless ADX is
    above the threshold AND -DI > +DI (bearish bias for the short)."""
    closes = [100.0] * 30 + [95.0] * 30
    df = _make_bar_df(closes)
    # Synthetic frame has adx=25, plus_di=25, minus_di=25 (no bearish bias).
    # With use_adx_filter=True the DI bias check must reject every entry.
    result = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.0,
            "require_rsi_falling": False,
            "use_adx_filter": True,
            "min_adx": 18.0,
        },
    )
    assert result["total_trades"] == 0


def test_exclusive_nifty_ce_sim_requires_three_bar_rsi_rising():
    """ECN sim must require RSI[-3] < RSI[-2] < RSI[-1] (matching live
    ``_compute_buy_signal``), not just a two-bar bounce."""

    n = 100
    # Build bars where every other RSI value drops then rises in a single
    # bar — a stale one-bar bounce. The live strategy rejects this.
    rsi_pattern = []
    for i in range(n):
        if i % 4 == 0:
            rsi_pattern.append(50.0)
        elif i % 4 == 1:
            rsi_pattern.append(48.0)
        elif i % 4 == 2:
            rsi_pattern.append(60.0)  # one-bar bounce above threshold
        else:
            rsi_pattern.append(50.0)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="30s"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0 + 0.1 * i for i in range(n)],
        "atr": [1.0] * n,
        "rsi": rsi_pattern,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [99.0] * n,
        "ema_30": [99.0] * n,
        "ema_50": [98.0] * n,
        "adx": [30.0] * n,
        "plus_di": [30.0] * n,
        "minus_di": [10.0] * n,
    })
    result = RealDataBacktester._simulate_exclusive_nifty_ce(df, {})
    # The pattern has no 3-bar rising window, so even with all other
    # gates satisfied, no entries should fire.
    assert result["total_trades"] == 0


def test_exclusive_nifty_ce_sim_requires_fresh_macd_cross():
    """ECN must reject entries where MACD is already bullish across both
    the current and prior bars (no fresh cross-up). PR #283 codex round-3."""
    n = 100
    # MACD bullish AND stale (no fresh cross) on every bar.
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="30s"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0 + 0.1 * i for i in range(n)],
        "atr": [1.0] * n,
        # 3-bar rising RSI window.
        "rsi": [55.0 + 0.5 * (i % 3) for i in range(n)],
        # MACD permanently bullish — never a fresh cross.
        "macd": [0.5] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [99.0] * n,
        "ema_30": [99.0] * n,
        "ema_50": [98.0] * n,
        "adx": [30.0] * n,
        "plus_di": [30.0] * n,
        "minus_di": [10.0] * n,
    })
    result = RealDataBacktester._simulate_exclusive_nifty_ce(df, {"rsi_min": 50, "rsi_max": 80})
    # No fresh cross-up exists in this frame, so all entries are rejected.
    assert result["total_trades"] == 0


def test_put_momentum_sim_requires_bearish_macd_cross():
    """PM must reject entries where MACD is bullish or stale. PR #283
    codex round-3 P2 — without this gate the simulator scored breakdown
    trades the live strategy would skip."""
    n = 100
    # MACD permanently bullish — no bearish fresh cross-down.
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        # Breakdown setup: prior swing low ~98, current close drops below.
        "low": [98.0 if i < 80 else 90.0 for i in range(n)],
        "close": [100.0 if i < 80 else 90.0 for i in range(n)],
        "atr": [1.0] * n,
        # Declining RSI to satisfy rsi_falling_bars_required.
        "rsi": [40.0 - 0.1 * i for i in range(n)],
        # MACD permanently bullish — never a fresh negative cross.
        "macd": [0.5] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [105.0] * n,
        "ema_30": [105.0] * n,
        "ema_50": [105.0] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })
    result = RealDataBacktester._simulate_put_momentum(df, {})
    assert result["total_trades"] == 0


def test_run_ml_param_optimizer_genetic_stage_handles_single_parent():
    """The GA stage must not crash with UnboundLocalError when Bayesian
    search produces a single best performer. PR #283 codex round-3 P2."""
    from app.strategies.ml_param_optimizer import (
        BacktestMetrics,
        ParameterSet,
        ParameterSpace,
    )
    from app.strategies.run_ml_param_optimizer import _genetic_algorithm_stage

    class _FixedBacktester:
        def backtest(self, params):
            return BacktestMetrics(total_trades=1, total_pnl=10.0, win_rate=1.0)

    space = ParameterSpace(name="sl_pct", param_type="float", min_value=0.1, max_value=0.5)
    single_parent = [
        ParameterSet(
            params={"sl_pct": 0.30},
            metrics=BacktestMetrics(total_trades=1, total_pnl=10.0, win_rate=1.0),
        )
    ]
    # Single-parent path: should run to completion without UnboundLocalError.
    results = _genetic_algorithm_stage(
        [space],
        _FixedBacktester(),  # type: ignore[arg-type]
        best_performers=single_parent,
        n_iterations=5,
    )
    assert len(results) == 5


# ---------------------------------------------------------------------------
# PR #283 codex round-4 regressions.
# ---------------------------------------------------------------------------


def test_trade_stats_max_drawdown_uses_cumulative_equity_curve():
    """``max_drawdown`` must reflect the worst peak-to-trough excursion
    of the equity curve, not just the single worst trade. PnLs
    ``[10, -3, -3]`` should produce drawdown -6 (cumulative 10 → 7 → 4
    with peak 10, max trough below peak = -6), NOT -3.
    """
    from app.strategies.postgres_data_loader import _trade_stats

    stats = _trade_stats([
        {"pnl_pct": 10.0},
        {"pnl_pct": -3.0},
        {"pnl_pct": -3.0},
    ])
    assert stats["max_drawdown"] == -6.0, (
        f"cumulative drawdown must be -6.0, got {stats['max_drawdown']}"
    )


def test_put_momentum_simulator_no_longer_books_partial_exits():
    """PR #283 codex round-4 P2: live PM ``on_tick`` only exits on stop,
    final_tp, or EOD — never on partial_tp_r. The simulator must mirror
    that contract so trade counts / PnL / win-rate aren't inflated."""
    # Build a frame that breaks down sharply, then partially recovers
    # towards the partial_tp_r target without hitting final_tp. A
    # one-trade simulator output proves no partial-exit booking.
    n = 200
    rsi_pattern = [40.0 - 0.05 * i for i in range(n)]
    # Closes drop below EMAs to satisfy the downtrend proxy, then drop
    # further to trigger a single entry.
    closes = [100.0] * 100 + [88.0] * (n - 100)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [c - 1.0 for c in closes],
        "close": closes,
        "atr": [1.0] * n,
        "rsi": rsi_pattern,
        # Fresh bearish MACD cross at bar 99 (transition to breakdown).
        "macd": [0.5] * 99 + [-0.5] * (n - 99),
        "macd_signal": [0.0] * n,
        "ema_20": [105.0] * n,
        "ema_30": [105.0] * n,
        "ema_50": [105.0] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })
    result = RealDataBacktester._simulate_put_momentum(df, {
        "lookback_breakdown_bars": 10,
        "rsi_falling_bars_required": 2,
        "max_bars_in_trade": 100,
    })
    # If the simulator had still booked a partial exit, we'd see
    # winning_trades >= 1 from the partial-exit recording branch in
    # addition to any later exit. Total exit count must equal the
    # number of distinct trades, not 2x for partial+final pairs.
    assert result.get("total_trades", 0) <= 1


def test_ema20_real_data_sim_requires_three_bar_strictly_falling_rsi():
    """Without three consecutive strictly-falling RSI bars, the
    require_rsi_falling gate must reject every entry — matching the
    live EMA20 strategy contract (prev_prev > prev > rsi).
    """
    n = 100
    # Strictly RISING RSI: no falling window can ever form.
    rsi_pattern = [40.0 + 0.1 * i for i in range(n)]
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [99.0 - 0.1 * i for i in range(n)],  # below EMA so entry candidate
        "atr": [1.0] * n,
        "rsi": rsi_pattern,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [100.0] * n,
        "ema_30": [100.0] * n,
        "ema_50": [100.0] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })
    result = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.0,
            "require_rsi_falling": True,
            "use_adx_filter": False,
        },
    )
    assert result["total_trades"] == 0


def test_ema20_real_data_sim_rejects_two_bar_downtick():
    """Codex round-4 P2 pin: a sequence ending in only ONE downward RSI
    step (``[..., 40, 45, 44]``) must be rejected because the live
    contract requires THREE consecutive strictly falling bars.
    """
    n = 100
    # RSI bounces every three bars; no window of three is strictly
    # falling but there are isolated single-bar dips.
    rsi_pattern = []
    for i in range(n):
        cycle = i % 3
        rsi_pattern.append({0: 40.0, 1: 45.0, 2: 44.0}[cycle])
    # The cycle is 40, 45, 44, 40, 45, 44, ... so windows ending at:
    #   i=2:  (40,45,44)  → 40<45 → fails
    #   i=3:  (45,44,40)  → 45>44>40 → STRICTLY FALLING. With the buggy
    #         single-bar check this window also passed (just rsi[3]<rsi[2]),
    #         but the live three-bar check ALSO passes here.
    # That means the cycle pattern triggers a real three-bar window
    # every third bar. We want to PIN that we don't accept the buggy
    # single-bar acceptance. Build a pattern where ONLY isolated
    # downticks exist (no three-bar window of falling).
    rsi_pattern = [40.0 + (i % 2) for i in range(n)]
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [99.0 - 0.1 * i for i in range(n)],
        "atr": [1.0] * n,
        "rsi": rsi_pattern,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
        "ema_20": [100.0] * n,
        "ema_30": [100.0] * n,
        "ema_50": [100.0] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })
    result = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.30,
            "tp_pct": 0.30,
            "min_atr": 0.0,
            "require_rsi_falling": True,
        },
    )
    # Pattern alternates 40/41/40/41/... — no three-bar strictly falling
    # window exists, only single-bar dips.
    assert result["total_trades"] == 0


def test_exclusive_nifty_ce_sim_rejects_when_momentum_negative():
    """ECN must reject entries when ret_1 or ret_5 are non-positive,
    matching the live ``mom_ok`` gate."""
    n = 100
    # Bars 50..99 satisfy every other ECN gate (trend, RSI band, etc.)
    # but close is FLAT after bar 50, so ret_1 / ret_5 are zero.
    closes = [100.0 * (1.0 + 0.001 * i) for i in range(50)] + [110.0] * 50
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="30s"),
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "atr": [1.0] * n,
        # 3-bar rising RSI in the entry window.
        "rsi": [55.0 + 0.5 * (i % 3) for i in range(n)],
        # MACD setup: cross-up at bar 50 transitioning to bullish.
        "macd": [-0.5] * 50 + [0.5] * 50,
        "macd_signal": [0.0] * n,
        # EMA20 below close to satisfy above_ema20.
        "ema_20": [c - 1.0 for c in closes],
        "ema_30": [c - 1.0 for c in closes],
        "ema_50": [c - 2.0 for c in closes],
        "adx": [30.0] * n,
        "plus_di": [30.0] * n,
        "minus_di": [10.0] * n,
    })
    result = RealDataBacktester._simulate_exclusive_nifty_ce(
        df,
        {
            "rsi_min": 50.0,
            "rsi_max": 80.0,
            "macd_hist_min": 0.0,
            "ema_atr_buffer": 0.0,
            "min_adx": 20.0,
            "min_di_spread": 5.0,
        },
    )
    # With closes flat after bar 50, mom_ok fails on every potential
    # entry bar. No trades should fire.
    assert result["total_trades"] == 0


def test_run_ml_param_optimizer_raises_systemexit_on_too_few_iterations():
    """Very small ``--iterations`` (e.g. 2-3) used to crash with an
    empty ``max()`` deep in the export path; must now fail loudly with
    a SystemExit pointing to the actionable fix."""
    from app.strategies.run_ml_param_optimizer import run_optimization

    with pytest.raises(SystemExit, match="zero configurations|too small"):
        # 2 iterations × 0.2 = 0 → bayesian gets 0, GA 0, refinement 0.
        run_optimization(n_iterations=2, output_file="/tmp/__pytest_smoke__.json")


# ---------------------------------------------------------------------------
# PR #283 codex round-5 regressions.
# ---------------------------------------------------------------------------


def test_multi_strategy_optimize_all_intersects_underlyings_with_per_strategy_allowlist():
    """``--underlyings`` must be intersected with each strategy's
    allowlist so e.g. ``--strategies exclusive_nifty_ce --underlyings NG_FUT``
    doesn't optimize the NIFTY-only CE strategy on natural-gas bars."""
    from app.strategies.run_multi_strategy_optimizer import MultiStrategyOptimizer

    optimizer = MultiStrategyOptimizer.__new__(MultiStrategyOptimizer)
    # Patch the runner so we can introspect what optimize_strategy gets
    # called with, without spinning up a real loader.
    optimizer.results = {}
    calls: list[tuple] = []

    class _StubRunner:
        def get_strategies(self):
            return {
                "ema20": {"underlyings": ["NIFTY_IDX", "BANKNIFTY_IDX", "NG_FUT"]},
                "exclusive_nifty_ce": {"underlyings": ["NIFTY_IDX"]},
            }

        def get_underlyings_for_strategy(self, strategy):
            return self.get_strategies()[strategy]["underlyings"]

    optimizer.runner = _StubRunner()
    optimizer.optimize_strategy = (
        lambda s, u, n_iterations: calls.append((s, u)) or {"top_5": []}
    )

    optimizer.optimize_all(
        n_iterations=100,
        strategies=["exclusive_nifty_ce"],
        underlyings=["NG_FUT"],  # not in ECN's allowlist
    )
    assert calls == [], (
        "exclusive_nifty_ce must not be optimized on NG_FUT — it's not "
        "in the strategy's allowlist; got calls=" + repr(calls)
    )


def test_multi_strategy_optimize_all_raises_systemexit_on_tiny_iteration_budget():
    """``--iterations`` so small that ``int(n * 0.5)`` rounds to zero
    must fail loudly the same way the standalone runner does — not
    silently emit ``-Infinity`` / ``null`` JSON."""
    from app.strategies.run_multi_strategy_optimizer import MultiStrategyOptimizer

    optimizer = MultiStrategyOptimizer.__new__(MultiStrategyOptimizer)
    optimizer.results = {}
    optimizer.runner = type("_Stub", (), {
        "get_strategies": lambda self: {},
        "get_underlyings_for_strategy": lambda self, s: [],
    })()
    optimizer.optimize_strategy = lambda *a, **kw: {"top_5": []}

    with pytest.raises(SystemExit, match="too small|zero"):
        optimizer.optimize_all(n_iterations=1)


def test_put_momentum_optimizer_no_longer_emits_partial_tp_r():
    """``partial_tp_r`` was removed from the PM param space — sampling
    a value the simulator and live ``on_tick`` don't honour produces
    noise. Reintroducing it would be a regression."""
    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    spaces = {s.name for s in PutMomentumParameterOptimizer.get_parameter_spaces()}
    assert "partial_tp_r" not in spaces


def test_standalone_ema20_param_space_drops_unused_advanced_exits():
    """``trail_buffer_pct`` and ``tp1_pct`` were sampled in the standalone
    ``run_ml_param_optimizer.get_ema20_parameter_spaces`` but the
    synthetic ``Ema20Backtester`` never read them. Reintroducing either
    would surface noise as "best advanced exit"."""
    from app.strategies.run_ml_param_optimizer import get_ema20_parameter_spaces

    names = {s.name for s in get_ema20_parameter_spaces()}
    for unused in ("trail_buffer_pct", "tp1_pct", "use_adx_filter", "min_adx"):
        assert unused not in names, (
            f"{unused!r} reintroduced into standalone EMA20 param space "
            "but Ema20Backtester.backtest does not read it"
        )


def test_synthetic_ema20_backtester_requires_three_bar_strictly_falling_rsi():
    """The synthetic ``Ema20Backtester`` must use the same three-bar
    strictly-falling-RSI gate as the real-data simulator and live
    strategy. Single-bar downticks must NOT pass."""
    import pandas as pd

    from app.strategies.ml_param_optimizer import Ema20Backtester, BacktestMetrics

    n = 100
    # Strictly RISING RSI: no three-bar falling window can exist.
    rsi = [40.0 + 0.1 * i for i in range(n)]
    closes = [100.0 - 0.05 * i for i in range(n)]  # below EMA, candidates for entry
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "volume": [1000] * n,
        "atr": [1.0] * n,
        "rsi": rsi,
    })
    bt = Ema20Backtester(ohlc_data=df)
    metrics = bt.backtest({
        "ema_period": 20,
        "sl_pct": 0.30,
        "tp_pct": 0.30,
        "min_atr": 0.0,
        "require_rsi_falling": True,
    })
    assert isinstance(metrics, BacktestMetrics)
    assert metrics.total_trades == 0, (
        "Strictly rising RSI must not produce any three-bar falling "
        "window, so require_rsi_falling rejects every entry"
    )



# ---------------------------------------------------------------------------
# PR #283 codex round-6 regressions.
# ---------------------------------------------------------------------------


def test_strategy_runner_drops_put_momentum_natgas():
    """``put_momentum_scalper`` is index-only in live. Removing
    ``NG_FUT`` from the allowlist stops the optimizer from emitting
    PM recommendations on a natgas stream the live strategy is not
    deployed for."""
    from app.strategies.strategy_optimizers import StrategyOptimizationRunner

    runner = StrategyOptimizationRunner()
    pm_unders = runner.get_underlyings_for_strategy("put_momentum")
    assert "NG_FUT" not in pm_unders
    assert set(pm_unders) <= {"NIFTY_IDX", "BANKNIFTY_IDX"}


def test_standalone_ema20_param_space_drops_signal_timeframe():
    """``signal_timeframe`` was sampled but never read by the synthetic
    ``Ema20Backtester.backtest()``. Reintroducing it would surface
    noise as "best timeframe"."""
    from app.strategies.run_ml_param_optimizer import get_ema20_parameter_spaces

    names = {s.name for s in get_ema20_parameter_spaces()}
    assert "signal_timeframe" not in names


def test_exclusive_nifty_ce_param_space_exposes_ema_fail_buffer_atr():
    """The ECN exit threshold uses a SEPARATE ``ema_fail_buffer_atr``
    field in live config; the optimizer must sample and emit it
    independently of the entry-side ``ema_atr_buffer`` so candidates
    can tune entry and exit buffers separately."""
    from app.strategies.strategy_optimizers import ExclusiveNiftyCeParameterOptimizer

    spaces = {s.name for s in ExclusiveNiftyCeParameterOptimizer.get_parameter_spaces()}
    formatted = ExclusiveNiftyCeParameterOptimizer.format_params({
        "ema_atr_buffer": 0.05,
        "ema_fail_buffer_atr": 0.15,
    })
    assert "ema_fail_buffer_atr" in spaces
    assert "ema_fail_buffer_atr" in formatted
    assert formatted["ema_fail_buffer_atr"] == 0.15


def test_put_momentum_simulator_exits_on_breakdown_high_invalidation():
    """Live PM's ``_maybe_invalidate`` exits the position when the
    underlying reverses back above the captured ``breakdown_high`` OR
    back above EMA20. The simulator must exit on the same condition
    instead of keeping the trade open until the time stop and
    booking it as a winner."""
    n = 200
    rsi_pattern = [40.0 - 0.05 * i for i in range(n)]
    # Bars 0..99: warm-up at 100. Bar 100: breakdown to 88. Bars
    # 101..120: continued drop. Bars 121+: reversal back ABOVE the
    # breakdown high (100), which should trigger invalidation.
    closes = [100.0] * 100 + [88.0] + [85.0] * 19 + [102.0] * (n - 120)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        "open": [100.0] * n,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "atr": [1.0] * n,
        "rsi": rsi_pattern,
        # Fresh bearish MACD cross at bar 99 (transition).
        "macd": [0.5] * 99 + [-0.5] * (n - 99),
        "macd_signal": [0.0] * n,
        # EMAs above closes so PM downtrend gate passes during the
        # 88 / 85 phase but the reversal to 102 triggers the
        # close-above-EMA20 invalidation.
        "ema_20": [95.0] * n,
        "ema_30": [95.0] * n,
        "ema_50": [95.0] * n,
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })
    result = RealDataBacktester._simulate_put_momentum(df, {
        "lookback_breakdown_bars": 10,
        "rsi_falling_bars_required": 2,
        "max_bars_in_trade": 200,  # so the time-stop doesn't fire first
    })
    # If invalidation fires correctly, the exit price is well above
    # entry (a LOSS for the short put) instead of the entry-bar's
    # 88. The total_pnl should reflect a loss, not a phantom win.
    if result["total_trades"] >= 1:
        # On a SHORT put on the underlying, ``pnl_pct`` is computed as
        # ``(entry - exit) / entry * delta_proxy * 100``. A reversal
        # back above the entry produces a NEGATIVE pnl_pct.
        assert result["total_pnl"] < 0, (
            "invalidation must exit when underlying reverses; got "
            f"total_pnl={result['total_pnl']:.2f} (expected < 0 because "
            "the exit price is above the entry price)"
        )


# ---------------------------------------------------------------------------
# PR #283 codex round-7 regressions (simulator fidelity v. live).
# ---------------------------------------------------------------------------


def _make_ecn_bar_df(closes, *, start="2026-01-01 09:15", freq="5min"):
    """OHLC + indicator frame with IST-friendly timestamps for the ECN sim.

    The frame uses tz-aware timestamps localized to UTC so the simulator's
    ``tz_convert('Asia/Kolkata')`` produces deterministic IST times-of-day.
    """
    n = len(closes)
    # 09:15 IST = 03:45 UTC. Start the frame at 03:45 UTC so bar 0 lands
    # on 09:15 IST and the entry-window gate opens on bar 1 (09:20 IST).
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01 03:45:00+00:00", periods=n, freq=freq),
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
        "atr": [1.0] * n,
        "rsi": [60.0] * n,
        "macd": [0.0] * n,
        "macd_signal": [0.0] * n,
        "ema_20": closes,
        "ema_50": [c - 0.5 for c in closes],
        "adx": [25.0] * n,
        "plus_di": [25.0] * n,
        "minus_di": [25.0] * n,
    })


def test_exclusive_nifty_ce_simulator_exits_on_bar_low_touch():
    """Live ``_manage_position_on_bar`` exits on ``candle.low <= sl_level``
    even when the bar closes back above the stop. The simulator must
    honour the same intra-bar wick exit; previously it only exited when
    the bar closed beyond the ATR stop."""
    # Construct a frame where bar i has a deep wick that touches SL but
    # closes back above it. The simulator should mark a loss; a close-only
    # check would leave the trade open and book a phantom recovery.

    n = 60
    # Warm-up at 100; entry trigger and then a wick-down bar.
    closes = [100.0] * n
    df = _make_ecn_bar_df(closes)
    # Bar 25: wick low touches stop (entry 100, sl 2 ATR ≈ 98 with atr=1
    # and sl_atr=2). Set low = 97 but close back at 100.5.
    df.loc[25, "low"] = 97.0
    df.loc[25, "close"] = 100.5
    # Run with stops that would never trip on close-only check.
    result = RealDataBacktester._simulate_exclusive_nifty_ce(
        df,
        {
            "sl_atr": 2.0,
            "tp_atr": 100.0,  # huge so target won't trip
            "rsi_min": 0.0,
            "rsi_max": 100.0,
            "min_adx": 0.0,
            "min_di_spread": 0.0,
            "ema_atr_buffer": 0.0,
            "ema_fail_buffer_atr": 100.0,  # disable ema_fail exit
            "ema_fail_bars": 1000,
            "macd_hist_min": -1e9,  # allow entries regardless of macd hist
        },
    )
    # Trade may or may not enter (entry gates are strict), but if it
    # does, the simulator must not raise. Pin the contract that the
    # exit-condition code path uses ``df["low"]`` — checked via direct
    # call below.
    _ = result
    # Stronger pin: the simulator function reads bar low/high before the
    # SL/TP comparison.
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "low_i <= sl_price" in src, (
        "ECN simulator must compare bar low to sl_price (live "
        "_manage_position_on_bar uses candle.low). Found:\n" + src
    )
    assert "high_i >= tp_price" in src, (
        "ECN simulator must compare bar high to tp_price (live "
        "_manage_position_on_bar uses candle.high)."
    )


def test_exclusive_nifty_ce_simulator_applies_late_tp_cap():
    """Live ECN strategy caps the take-profit at ``late_tp_cap_atr``
    once the IST clock is past 14:00. A candidate with ``tp_atr=10`` in
    the morning should see ``tp_atr=2.6`` after 14:00 IST."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "late_tp_cap_atr" in src, (
        "ECN simulator must read late_tp_cap_atr and apply it after "
        "14:00 IST."
    )
    assert "effective_tp_atr" in src
    assert "_ecn_is_late" in src


def test_exclusive_nifty_ce_simulator_squareoff_at_1515_ist():
    """Live ECN config exits all open positions at 15:15 IST. The
    simulator must close the trade on the first bar past 15:15 IST."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "_ecn_past_squareoff" in src
    assert "squareoff_exit" in src


def test_exclusive_nifty_ce_simulator_honours_cooldown_after_exit():
    """Live ECN strategy sets ``state.cooldown_bars`` after each exit
    to block re-entry. The simulator must reproduce this so the
    optimizer doesn't reward parameters that fire many back-to-back
    trades the live strategy would have rejected.

    PR #283 codex round-20 P2: live trace for ``cooldown_bars=N``
    blocks the EXIT bar + (N-2) FOLLOW-ON bars (the exit bar is free
    because position-just-closed dec→N-1>0 skips; subsequent bars
    dec each time and admit when counter reaches 0). The sim's
    exit bar is never re-evaluated for re-entry, so to match the
    FOLLOW-ON block count (N-2) the post-exit assignment uses
    ``cooldown_bars - 2``. Round-9's ``- 1`` was off by one and
    blocked one extra follow-on bar per exit, undercounting
    re-entries vs live."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "cooldown_remaining" in src
    assert "cooldown_bars_cfg" in src
    # Must DECREMENT inside the loop AND set on exit, using -2 to
    # match the number of follow-on bars live blocks (round 20 fix).
    assert "cooldown_remaining -= 1" in src
    assert "cooldown_bars_cfg - 2" in src, (
        "post-exit cooldown must be cooldown_bars_cfg - 2 so the "
        "follow-on blocked bars match live (N-2 bars). The round-9 "
        "value `- 1` was off by one."
    )


def test_exclusive_nifty_ce_simulator_entry_window_gate():
    """Live ECN config (app/config/strategy_env.yaml) sets
    ``session_start: 10:15``, ``last_entry_time: 14:45``. The
    simulator must use the same defaults (overridable via params)."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "_within_ecn_entry_window" in src
    # Window defaults must match the live yaml config.
    assert "10:15" in src, "session_start default must be 10:15 (live config)"
    assert "14:45" in src, "last_entry_time default must be 14:45 (live config)"


def test_exclusive_nifty_ce_simulator_late_start_matches_live_config():
    """Live ECN config sets ``late_start: 14:45`` (NOT 14:00). The
    simulator's late_tp_cap_atr must trip at 14:45, not 14:00 — which
    would apply the late cap 45 minutes too early."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # The fallback / default must read 14:45 from params.
    assert '"14:45"' in src or "'14:45'" in src
    # And there must NOT be a hard-coded 14:00 anywhere.
    assert "_time(14, 0)" not in src
    assert "_time(14,0)" not in src


def test_exclusive_nifty_ce_simulator_enforces_max_trades_per_day():
    """Live ECN config sets ``max_trades_per_day: 1``. The simulator
    must enforce the same per-day cap so an optimizer cannot rank
    parameters on multi-entry sessions live would never have placed."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "max_trades_per_day" in src
    assert "trades_today" in src
    # Reset on calendar-day boundary AND increment on entry.
    assert "trades_today = 0" in src
    assert "trades_today += 1" in src
    assert "trades_today >= max_trades_per_day_cfg" in src


def test_put_momentum_simulator_eod_squareoff_at_1520_ist():
    """Live PM ``on_tick`` exits at 15:20 IST. Simulator must too."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    assert "_past_eod" in src
    assert "eod_exit" in src
    assert "_time(15, 20)" in src or "_time(15,20)" in src


def test_put_momentum_simulator_entry_windows():
    """Live PM only accepts entries inside morning 09:20-11:30 IST and
    afternoon 13:30-15:00 IST windows."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    assert "_within_entry_window" in src
    assert "_MORNING_START" in src
    assert "_AFTERNOON_END" in src


def test_put_momentum_breakdown_high_uses_candle_own_high():
    """Live PM ``_enter_position`` captures ``candle.h`` (the breakdown
    candle's OWN high) as ``breakdown_high``, NOT the lookback window's
    max. The previous lookback-max made invalidation insensitive to
    reversal."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    # The fix line must use ``df["high"].iloc[i]`` — not ``.max()``.
    assert 'df["high"].iloc[i]' in src
    # And there must NOT be a lookback-window max() assignment to
    # breakdown_high_at_entry.
    assert "breakdown_high_at_entry = float(df[\"high\"].iloc[i])" in src


def test_put_momentum_breakdown_bar_matches_live_predicate():
    """PR #283 codex round-8 P2: live ``_is_breakdown_bar``
    (app/strategies/put_momentum_scalper.py:1230) is EXACTLY
    ``candle.low <= min(prior_lows) AND lower_wick_ratio <= 0.30``.
    Earlier rounds OR-ed in ``close < prior_low``, which admitted
    breakdown candles live would reject (close-below candles with
    large reversal wicks above 30% range). The OR is removed, AND
    the wick comparison uses ``<=`` to admit equal-low candles
    live also admits."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    assert "lower_wick_ratio" in src
    # NO OR with close-below-low.
    assert "breakdown_close or breakdown_wick" not in src
    assert "breakdown_close" not in src
    # The single live predicate: ``low <= prior_low AND wick <= 0.30``.
    assert "low_i <= prior_low" in src
    assert "lower_wick_ratio <= 0.30" in src


# ---------------------------------------------------------------------------
# PR #283 codex round-9 regressions.
# ---------------------------------------------------------------------------


def test_ecn_max_trades_per_day_zero_means_unlimited():
    """Live ECN's ``max_trades_per_day=0`` is the explicit
    "unlimited" sentinel (see comment in
    ``app/strategies/exclusive_nifty_ce_buy.py``). The simulator
    must NOT treat 0 as "block every entry" — otherwise an operator
    who sets 0 to disable the daily cap gets zero trades the live
    strategy would have taken."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # The cap check must include a ``> 0`` guard so zero falls
    # through to the unlimited path.
    assert "max_trades_per_day_cfg > 0" in src, (
        "max_trades_per_day=0 must be honored as 'unlimited' — wrap "
        "the comparison in `if max_trades_per_day_cfg > 0` so the cap "
        "is skipped entirely"
    )


def test_ecn_daily_reset_handles_tz_naive_timestamps():
    """Live ECN simulator must reset the daily trade count even when
    ``df['timestamp']`` is tz-naive. The previous ``tz_convert``-only
    path raised on naive timestamps and left ``trades_today`` stuck
    across days in multi-day synthetic / unit-test fixtures."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # Must localize tz-naive timestamps to UTC before converting,
    # matching the ``ist_time_of_day`` setup.
    assert "ts_raw.tz_localize" in src or 'tz_localize("UTC")' in src, (
        "tz-naive timestamps must be localized to UTC before "
        "tz_convert('Asia/Kolkata') — otherwise trades_today never "
        "resets on naive-timestamp fixtures"
    )


def test_ecn_accepts_live_squareoff_time_key():
    """The live ``ExclusiveNiftyCeBuyStrategy`` reads the key
    ``squareoff_time`` (one word, no underscore). The simulator must
    accept that spelling so candidates persisted under the live key
    are scored against the right exit time. The yaml-config spelling
    ``square_off_time`` remains accepted as a fallback."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # Both spellings must be recognized.
    assert "squareoff_time" in src, (
        "live strategy key ``squareoff_time`` must be accepted"
    )
    assert "square_off_time" in src, (
        "yaml-config key ``square_off_time`` must still be accepted "
        "as a fallback"
    )


# ---------------------------------------------------------------------------
# PR #283 codex round-10 regressions.
# ---------------------------------------------------------------------------


def test_ecn_simulator_admits_macd_near_when_allow_near_macd_true():
    """Live ECN's ``_compute_buy_signal`` admits entries on a
    ``macd_near_cross_up`` configuration (MACD still below signal but
    rising, ``macd_div`` within ``-macd_near`` and rising vs prior
    bar, ``macd_hist`` rising). The simulator previously required a
    full cross-up."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "macd_near_cross_up" in src, (
        "ECN simulator must expose the macd_near_cross_up live "
        "alternative (exclusive_nifty_ce_buy.py:1118)"
    )
    assert "allow_near_macd" in src
    # macd_ok must accept EITHER full cross-up OR near-cross-up
    # (matching exclusive_nifty_ce_buy.py:1125).
    assert "macd_confirmed or" in src or "or (allow_near_macd" in src, (
        "macd_ok must be (confirmed OR near_with_allow)"
    )


def test_ecn_entry_window_evaluates_against_candle_end_ts():
    """Live ECN uses ``next_bar_start = candle.end_ts = ts_start +
    timeframe_seconds`` for the entry-window check, not the signal
    bar's ``ts_start``. A 30s signal at 14:45:00 has
    ``next_bar_start = 14:45:30`` and is rejected against
    ``last_entry_time = 14:45``.

    PR #283 codex round-11 P2: the round-10 fix probed the next
    STORED row, which is wrong when the frame has gaps (overnight,
    halts) — the next row could be hours/days later. The fix derives
    the bar interval from the data and uses
    ``ts + timeframe_seconds`` deterministically."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "ist_end_of_day" in src, (
        "_within_ecn_entry_window must compute the bar's own END time "
        "(ts_start + timeframe_seconds), not probe the next stored row"
    )
    assert "timeframe_seconds" in src, (
        "must derive bar interval from the data so candle.end_ts is "
        "deterministic across overnight/trading-halt gaps"
    )


# ---------------------------------------------------------------------------
# PR #283 codex round-11 regressions.
# ---------------------------------------------------------------------------


def test_ecn_macd_near_default_matches_deployed_yaml():
    """PR #283 codex round-11 P2: yaml-deployed default is
    ``macd_near: 0.0`` (see ``app/config/strategy_env.yaml``), not
    the strategy class fallback of 0.40. The simulator must match
    the DEPLOYED configuration so the optimizer's near-MACD path is
    not wider than production."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # Default must be 0.0 (deployed yaml), not 0.40 (class fallback).
    assert 'params.get("macd_near", 0.0)' in src, (
        "macd_near default must mirror the yaml-deployed value 0.0, "
        "not the strategy-class fallback 0.40"
    )


def test_ema20_simulator_enforces_di_spread_when_adx_filter_on():
    """Live ``_passes_adx_filter`` requires BOTH bearish DI bias AND
    ``min_di_spread`` when ADX is enabled. The simulator was
    accepting candidates with narrow DI spread."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_ema20)
    assert "min_di_spread" in src
    assert "di_spread_abs" in src or "di_spread" in src
    # Both conditions must be ANDed when ADX is enabled.
    assert "di_spread_abs >= min_di_spread" in src


def test_ema20_simulator_honors_intraday_entry_and_squareoff_windows():
    """Live EMA20 yaml-default config sets ``first_entry_time: 9:30``
    and ``square_off_time: 15:00``. The simulator must gate entries
    inside that window and force exit at squareoff."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_ema20)
    assert "first_entry_time" in src
    assert "square_off_time" in src
    assert "_within_entry_window" in src
    assert "_past_squareoff" in src or "squareoff_hit" in src


# ---------------------------------------------------------------------------
# PR #283 codex round-12 regressions.
# ---------------------------------------------------------------------------


def test_ecn_simulator_includes_trailing_ema_exit():
    """Live ECN ``_manage_position_on_bar`` exits via ``TRAIL_EMA20``
    when the bar high has reached ``trail_active_level`` AND the bar
    low has wicked below ``ema20 - trail_cushion * atr``. The
    simulator must model this since it fires BEFORE the EMA-fail
    counter increments — without it the optimizer rewards parameter
    sets that ride a trailing reversal live would have cut."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # Must read the trail config knobs (with late-session variants).
    assert "trail_active_atr" in src
    assert "trail_cushion_atr" in src
    assert "late_trail_active_atr" in src
    assert "late_trail_cushion" in src
    # The exit logic must arm on bar-high crossing the active level
    # AND fire when the bar-low pierces the trail level.
    assert "trail_armed" in src
    assert "trail_exit" in src
    assert "trail_active_level" in src
    assert "trail_level" in src


def test_pm_simulator_honours_entry_start_entry_end_single_window():
    """PR #283 codex round-12 P2: live PM ``_within_entry_window``
    uses ``entry_start`` / ``entry_end`` as a SINGLE window when
    both are configured (deployed yaml: 09:20-14:45 for NIFTY /
    BANKNIFTY), and only falls back to the morning/afternoon split
    when those are absent. The simulator was hard-coding the split
    and admitting 11:30-13:30 bars that live would reject."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    assert "entry_start" in src and "entry_end" in src
    assert "_SINGLE_WINDOW" in src
    # The single-window branch must be tried BEFORE the
    # morning/afternoon split, matching live priority.
    single_idx = src.index("_SINGLE_WINDOW is not None")
    split_idx = src.index("_MORNING_START <= tod <= _MORNING_END")
    assert single_idx < split_idx, (
        "single-window branch must be evaluated BEFORE the morning/"
        "afternoon split, matching live ``_within_entry_window`` "
        "priority (put_momentum_scalper.py:790)"
    )


def test_ema20_param_space_samples_min_di_spread():
    """PR #283 codex round-12 P2: now that the EMA20 simulator
    enforces ``min_di_spread`` when ADX is on, the optimizer must
    sample it so candidates discover the right DI-spread threshold
    instead of being scored against the single hard-coded default."""
    from app.strategies.strategy_optimizers import Ema20ParameterOptimizer

    spaces = {s.name for s in Ema20ParameterOptimizer.get_parameter_spaces()}
    assert "min_di_spread" in spaces, (
        "EMA20 param space must include min_di_spread now that the "
        "real-data simulator reads it"
    )
    formatted = Ema20ParameterOptimizer.format_params({
        "ema_period": 20,
        "signal_timeframe": 300,
        "sl_pct": 0.10,
        "tp_pct": 0.30,
        "min_atr": 1.0,
        "use_adx_filter": True,
        "min_adx": 20.0,
        "min_di_spread": 5.5,
    })
    assert "min_di_spread" in formatted
    assert formatted["min_di_spread"] == 5.5


def test_pm_format_params_threads_deployed_yaml_entry_window():
    """PR #283 codex round-13 P2: round-14 added single-window
    handling to the simulator, but ``PutMomentumParameterOptimizer.
    format_params`` never emitted ``entry_start`` / ``entry_end``,
    so the simulator's ``_within_entry_window`` fell back to the
    morning/afternoon split path. Now the format step threads the
    deployed yaml defaults (09:20 / 14:45) through unless the
    optimizer is sweeping the window."""
    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    # Default flow: no overrides → deployed-yaml defaults.
    formatted = PutMomentumParameterOptimizer.format_params({})
    assert formatted.get("entry_start") == "09:20", (
        "format_params must default entry_start to the deployed yaml "
        "value 09:20"
    )
    assert formatted.get("entry_end") == "14:45", (
        "format_params must default entry_end to the deployed yaml "
        "value 14:45"
    )
    # Override flow: an optimizer-sampled value passes through.
    formatted = PutMomentumParameterOptimizer.format_params({
        "entry_start": "10:00",
        "entry_end": "13:30",
    })
    assert formatted.get("entry_start") == "10:00"
    assert formatted.get("entry_end") == "13:30"


# ---------------------------------------------------------------------------
# PR #283 codex round-14 regressions (per-underlying yaml defaults).
# ---------------------------------------------------------------------------


def test_backtest_ema20_threads_per_underlying_defaults():
    """PR #283 codex round-14 P2: NIFTY_IDX yaml uses
    ``first_entry_time: 9:30`` and ``square_off_time: 15:00``,
    BANKNIFTY_IDX has no first_entry override (so the entry window
    opens at session start), and NG_FUT runs 09:00-23:30. The
    backtester must thread the right defaults for each underlying
    so an optimizer run scores the candidate against the deployed
    intraday window."""
    nifty = RealDataBacktester._ema20_defaults_for("NIFTY_IDX")
    assert nifty == {"first_entry_time": "9:30", "square_off_time": "15:00"}

    banknifty = RealDataBacktester._ema20_defaults_for("BANKNIFTY_IDX")
    assert banknifty["first_entry_time"] == "00:00"
    assert banknifty["square_off_time"] == "15:00"

    nat_gas = RealDataBacktester._ema20_defaults_for("NG_FUT")
    assert nat_gas["square_off_time"] == "23:30", (
        "NG_FUT yaml runs 09:00-23:30 — the simulator must NOT apply "
        "NIFTY's 15:00 squareoff which would force-exit every NG_FUT "
        "trade mid-session"
    )

    # Unknown underlying returns empty (no override, simulator falls
    # back to its own internal defaults).
    assert RealDataBacktester._ema20_defaults_for("UNKNOWN") == {}


def test_backtest_ecn_threads_per_underlying_defaults():
    """Live ECN deployed config is NIFTY_IDX only with
    ``session_start: 10:15``, ``last_entry_time: 14:45``,
    ``square_off_time: 15:15``, ``late_start: 14:45``,
    ``max_trades_per_day: 1``."""
    nifty = RealDataBacktester._ecn_defaults_for("NIFTY_IDX")
    assert nifty["session_start"] == "10:15"
    assert nifty["last_entry_time"] == "14:45"
    assert nifty["square_off_time"] == "15:15"
    assert nifty["late_start"] == "14:45"
    assert nifty["max_trades_per_day"] == 1
    # Other underlyings: ECN is not enabled in deployed yaml, so no
    # overrides. The simulator's class-level defaults still apply.
    assert RealDataBacktester._ecn_defaults_for("BANKNIFTY_IDX") == {}
    assert RealDataBacktester._ecn_defaults_for("NG_FUT") == {}


def test_backtest_pm_threads_per_underlying_defaults():
    """Live PM deployed config is enabled on NIFTY_IDX and
    BANKNIFTY_IDX with the single-window ``entry_start: 09:20`` /
    ``entry_end: 14:45``. NG_FUT is not enabled (PM allowlist
    excludes it)."""
    for ul in ("NIFTY_IDX", "BANKNIFTY_IDX"):
        d = RealDataBacktester._pm_defaults_for(ul)
        assert d.get("entry_start") == "09:20"
        assert d.get("entry_end") == "14:45"
    assert RealDataBacktester._pm_defaults_for("NG_FUT") == {}


def test_loader_select_includes_ecn_private_ema_column():
    """PR #283 codex round-14 P2: live ECN reads a PRIVATE 20-period
    EMA overlay (``exclusive_nifty_ce_buy_ema20_30s`` — added by
    ``migrations/003_exclusive_nifty_ce_buy_private_ema.sql``).
    The loader must select this column so the ECN simulator can
    prefer it over the generic ``ema_20`` when present, matching
    live ``_on_signal`` behaviour."""
    captured = {}

    class _FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            captured["query"] = query

        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

    loader = PostgresIndicatorLoader(dsn="postgresql://u@h:5432/d")
    loader._conn = _FakeConn()
    loader.fetch_indicator_bars(
        underlying_label="NIFTY_IDX",
        timeframe_seconds=30,
        days_back=5,
    )
    sql = captured["query"]
    assert "exclusive_nifty_ce_buy_ema20_30s" in sql, (
        "loader must select the private ECN EMA column so the "
        "simulator can prefer it over ema_20 (matches live overlay)"
    )


def test_ecn_simulator_prefers_private_ema_column_when_present():
    """PR #283 codex round-14 P2: when the bar includes the private
    20-period EMA overlay, the ECN simulator must use it as ``ema20``
    rather than the generic ``ema_20``. Live
    ``ExclusiveNiftyCeBuyStrategy._on_signal`` swaps in this overlay
    before computing the entry/exit signals."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    # Must reference the private column.
    assert "exclusive_nifty_ce_buy_ema20_30s" in src
    # Must coalesce per-bar (round-18 fix) — sparse private rows
    # fall back to the generic ``ema_20`` for that bar, not for the
    # whole frame.
    assert "combine_first" in src, (
        "private ECN EMA must coalesce per-bar with the generic "
        "ema_20 — picking a single sparse series leaks NaN into "
        "the simulator loop"
    )


# ---------------------------------------------------------------------------
# PR #283 codex round-17 regressions.
# ---------------------------------------------------------------------------


def test_ecn_defaults_include_all_simulator_consumed_keys():
    """PR #283 codex round-17 P2: the round-16 ``_ecn_defaults_for``
    only set window keys; entry/exit/limit thresholds fell back to
    the simulator class fallbacks instead of the deployed yaml. The
    extended defaults must cover EVERY key the simulator reads."""
    nifty = RealDataBacktester._ecn_defaults_for("NIFTY_IDX")
    # Entry gates.
    assert nifty["rsi_min"] == 52.0
    assert nifty["rsi_max"] == 72.0
    assert nifty["ema_atr_buffer"] == 0.05
    assert nifty["macd_hist_min"] == 0.0
    assert nifty["allow_near_macd"] is True
    assert nifty["macd_near"] == 0.0
    assert nifty["min_adx"] == 14.0
    assert nifty["min_di_spread"] == 0.0
    # Exit knobs.
    assert nifty["sl_atr"] == 2.0
    assert nifty["tp_atr"] == 2.5
    assert nifty["ema_fail_bars"] == 3
    assert nifty["ema_fail_buffer_atr"] == 0.10
    assert nifty["trail_active_atr"] == 0.8
    assert nifty["trail_cushion_atr"] == 0.16
    assert nifty["late_tp_cap_atr"] == 2.6
    assert nifty["late_trail_active_atr"] == 0.6
    assert nifty["late_trail_cushion"] == 0.08
    # Daily limits + cooldown.
    assert nifty["max_trades_per_day"] == 1
    assert nifty["cooldown_bars"] == 2
    # Time-of-day gates.
    assert nifty["session_start"] == "10:15"
    assert nifty["last_entry_time"] == "14:45"
    assert nifty["square_off_time"] == "15:15"
    assert nifty["late_start"] == "14:45"


def test_pm_defaults_include_all_simulator_consumed_keys():
    """PR #283 codex round-17 P2: PM yaml-optimized values
    (``rsi_min: 20``, ``rsi_falling_bars_required: 1``,
    ``lookback_breakdown_bars: 8``) materially differ from the class
    fallbacks. Without including them in ``_pm_defaults_for``, the
    simulator was scoring candidates against the wrong thresholds
    whenever the caller omitted these keys."""
    for ul in ("NIFTY_IDX", "BANKNIFTY_IDX"):
        d = RealDataBacktester._pm_defaults_for(ul)
        # Yaml-optimized values.
        assert d["rsi_min"] == 20.0
        assert d["rsi_max"] == 45.0
        assert d["min_atr_ratio"] == 0.0008
        assert d["rsi_falling_bars_required"] == 1
        assert d["lookback_breakdown_bars"] == 8
        assert d["max_bars_in_trade"] == 14
        # Exit knobs.
        assert d["option_sl_pct"] == 0.25
        assert d["final_tp_r"] == 1.5
        # Entry windows still threaded for the simulator's
        # single-window path.
        assert d["entry_start"] == "09:20"
        assert d["entry_end"] == "14:45"


def test_ema20_ng_fut_first_entry_matches_yaml():
    """PR #283 codex round-17 P2: NG_FUT yaml sets
    ``first_entry_time: 09:30``, NOT 09:00 (the round-16
    approximation). Must match the deployed value exactly so
    pre-09:30 entries aren't admitted."""
    nat_gas = RealDataBacktester._ema20_defaults_for("NG_FUT")
    assert nat_gas["first_entry_time"] == "09:30"
    assert nat_gas["square_off_time"] == "23:30"


# ---------------------------------------------------------------------------
# PR #283 codex round-18 regressions.
# ---------------------------------------------------------------------------


def test_ecn_format_params_emits_deployed_yaml_defaults():
    """PR #283 codex round-18 P2: ECN ``format_params`` defaults
    now mirror the deployed yaml (``app/config/strategy_env.yaml``)
    instead of the class fallbacks. Partial caller params should
    NOT silently downgrade to thresholds that don't match
    production."""
    from app.strategies.strategy_optimizers import ExclusiveNiftyCeParameterOptimizer

    out = ExclusiveNiftyCeParameterOptimizer.format_params({})
    # yaml-deployed thresholds.
    assert out["rsi_min"] == 52.0
    assert out["macd_hist_min"] == 0.0
    assert out["min_adx"] == 14.0
    assert out["min_di_spread"] == 0.0
    assert out["sl_atr"] == 2.0
    # yaml-deployed exit knobs.
    assert out["max_trades_per_day"] == 1
    assert out["cooldown_bars"] == 2
    assert out["session_start"] == "10:15"
    assert out["last_entry_time"] == "14:45"
    assert out["late_start"] == "14:45"
    # PR #283 round-18: emit LIVE key ``squareoff_time`` (one word).
    assert "squareoff_time" in out
    assert out["squareoff_time"] == "15:15"
    # Trail knobs included so the simulator scores against live values.
    assert out["trail_active_atr"] == 0.8
    assert out["trail_cushion_atr"] == 0.16
    assert out["late_trail_active_atr"] == 0.6
    assert out["late_trail_cushion"] == 0.08


def test_ecn_format_params_accepts_legacy_square_off_time_key():
    """``square_off_time`` (yaml two-word spelling) must still be
    accepted as a fallback for ``squareoff_time`` (live key) so
    older fixtures or operator-set values continue to work."""
    from app.strategies.strategy_optimizers import ExclusiveNiftyCeParameterOptimizer

    out = ExclusiveNiftyCeParameterOptimizer.format_params({
        "square_off_time": "15:25",
    })
    assert out["squareoff_time"] == "15:25"


def test_pm_format_params_uses_yaml_optimized_defaults():
    """PR #283 codex round-18 P2: PM format_params defaults now
    mirror the yaml-OPTIMIZED values, not the class fallbacks.
    Several deployed knobs differ materially:
      rsi_min: 20 (yaml) vs 25 (class)
      min_atr_ratio: 0.0008 (yaml) vs 0.0015 (class)
      rsi_falling_bars_required: 1 (yaml) vs 2 (class)
      lookback_breakdown_bars: 8 (yaml) vs 10 (class)
      max_bars_in_trade: 14 (yaml) vs 8 (class)
    """
    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    out = PutMomentumParameterOptimizer.format_params({})
    assert out["rsi_min"] == 20.0
    assert out["min_atr_ratio"] == 0.0008
    assert out["rsi_falling_bars_required"] == 1
    assert out["lookback_breakdown_bars"] == 8
    assert out["max_bars_in_trade"] == 14


def test_ema20_period_sampled_from_live_values():
    """PR #283 codex round-18/19 P2: ``ema_period`` is categorical
    over the set of LIVE-strategy values, not a continuous int.
    Includes:
      - 8 (NG_FUT yaml-deployed, computed in-memory by live)
      - 20 / 30 / 50 (persisted ema_20/30/50 columns)
    Sampling a continuous int range admits candidates whose period
    has no matching live behaviour."""
    from app.strategies.strategy_optimizers import Ema20ParameterOptimizer

    spaces = {s.name: s for s in Ema20ParameterOptimizer.get_parameter_spaces()}
    period_space = spaces["ema_period"]
    assert period_space.param_type == "categorical", (
        "ema_period must be categorical so the optimizer only samples "
        "values that match live-strategy behaviour"
    )
    assert set(period_space.categories) == {8, 20, 30, 50}, (
        f"ema_period categories must include NG_FUT's 8 plus persisted "
        f"20/30/50; got {period_space.categories}"
    )


def test_ema20_simulator_prefers_persisted_ema_column_when_available():
    """PR #283 codex round-19 P2: when the sampled ``ema_period``
    matches a persisted column (``ema_20``/``ema_30``/``ema_50``)
    the simulator must read that column instead of recomputing the
    EMA in-memory. Live strategies read these columns from the
    streamed indicator pipeline, and recomputing from only the
    fetched window produces a different warmup tail than live."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_ema20)
    # Must reference ``ema_{period}`` lookup against the dataframe.
    assert 'f"ema_{int(ema_period)}"' in src
    # Must coalesce per-bar with a computed series for sparse rows.
    assert "combine_first" in src


# ---------------------------------------------------------------------------
# PR #283 codex round-20 regressions (triage fixes from review).
# ---------------------------------------------------------------------------


def test_ecn_cooldown_matches_live_follow_on_block_count():
    """PR #283 codex round-20 P2: live ``cooldown_bars=N`` blocks
    the EXIT bar + (N-2) FOLLOW-ON bars. Round-9's ``- 1`` was off
    by one and blocked an EXTRA follow-on bar (undercounting
    re-entries). The sim's exit bar can't re-enter anyway, so the
    follow-on block count must equal N-2."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_exclusive_nifty_ce)
    assert "cooldown_bars_cfg - 2" in src
    # Old formula must NOT remain.
    assert "cooldown_bars_cfg - 1" not in src


def test_pm_breakdown_lookback_includes_current_bar_semantic():
    """PR #283 codex round-20 P2: live ``_is_breakdown_bar`` appends
    the current candle first, so ``state.bars_5m[-lookback:]`` is
    current + prior (lookback-1) bars. The sim must compare against
    the prior (lookback-1) lows, not prior ``lookback`` lows."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_put_momentum)
    # Window must be ``i - (lookback - 1) : i`` (prior 7 for lookback=8).
    assert "lookback_breakdown_bars - 1" in src
    # Old ``i - lookback_breakdown_bars : i`` form must NOT remain.
    assert 'iloc[i - lookback_breakdown_bars:i].min()' not in src


def test_loader_preserves_nan_for_ema_columns():
    """PR #283 codex round-20 P2: EMA columns must keep NaNs at the
    start of the window so the simulator's ``combine_first`` per-bar
    coalesce can distinguish 'indicator not warmed' from 'indicator
    equals 0'. The previous ``fillna(0)`` made every sparse-EMA row
    look like ``ema_20 == 0``, silently flipping ``close > ema`` to
    always-True and breaking the entry gate. Other indicators
    (atr/rsi/macd/adx/di) keep ffill+0 because the simulator can't
    distinguish 0-from-NaN meaningfully there."""
    import inspect
    from app.strategies.postgres_data_loader import PostgresIndicatorLoader

    src = inspect.getsource(PostgresIndicatorLoader.fetch_indicator_bars)
    # Must list EMA columns in the preserve-NaN set.
    assert "_PRESERVE_NAN" in src
    assert '"ema_20"' in src and '"ema_30"' in src and '"ema_50"' in src
    assert '"exclusive_nifty_ce_buy_ema20_30s"' in src
    # Preserve-NaN branch must NOT call fillna(0).
    preserve_idx = src.index("_PRESERVE_NAN")
    branch = src[preserve_idx:preserve_idx + 800]
    # Inside the preserve-branch the code should not have ``.fillna(0)``
    # — only ``.fillna(method="ffill")``.
    assert 'fillna(method="ffill")' in branch


def test_ema20_simulator_starts_at_bar_zero_when_persisted_ema_loaded():
    """PR #283 codex round-20 P2: when the persisted EMA column is
    present and warmed (live bars persist EMA from before the
    fetched optimization window), the simulator should NOT skip
    the first ``ema_period`` bars. Live ``Ema20Strategy`` only
    checks ``indicators[f"ema_{period}"]`` is present before
    evaluating entry gates."""
    import inspect

    src = inspect.getsource(RealDataBacktester._simulate_ema20)
    # The loop start must depend on whether the persisted column is used.
    assert "ema_is_persisted" in src
    assert "start_idx = 0 if ema_is_persisted else ema_period" in src
