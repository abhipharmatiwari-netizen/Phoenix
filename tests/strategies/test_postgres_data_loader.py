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

    tight = RealDataBacktester._simulate_ema20(
        df,
        {"ema_period": 20, "sl_pct": 0.05, "tp_pct": 5.0, "min_atr": 0.0},
    )
    loose = RealDataBacktester._simulate_ema20(
        df,
        {"ema_period": 20, "sl_pct": 0.30, "tp_pct": 5.0, "min_atr": 0.0},
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
