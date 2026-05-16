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
    """Tiny OHLC + indicator frame for simulator unit tests."""
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
        "ema_20": closes,
        "ema_30": closes,
        "ema_50": closes,
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
    tight = RealDataBacktester._simulate_ema20(
        df,
        {
            "ema_period": 20,
            "sl_pct": 0.05,
            "tp_pct": 5.0,
            "min_atr": 0.0,
            "require_rsi_falling": False,
            "use_adx_filter": False,
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
    """PM sim must read option_sl_pct/partial_tp_r/final_tp_r and the rest
    of PutMomentumScalperConfig — NOT sl_pct/tp_pct/trend_ema_period."""
    from app.strategies.strategy_optimizers import PutMomentumParameterOptimizer

    spaces = {s.name for s in PutMomentumParameterOptimizer.get_parameter_spaces()}
    formatted_keys = set(
        PutMomentumParameterOptimizer.format_params({
            "rsi_min": 25.0,
            "rsi_max": 45.0,
            "min_atr_ratio": 0.0015,
            "option_sl_pct": 0.25,
            "partial_tp_r": 1.0,
            "final_tp_r": 1.5,
            "rsi_falling_bars_required": 2,
            "lookback_breakdown_bars": 10,
            "max_bars_in_trade": 8,
        }).keys()
    )

    live_required = {
        "option_sl_pct", "partial_tp_r", "final_tp_r",
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
