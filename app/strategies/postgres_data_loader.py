"""
PostgreSQL data loader for real OHLC + indicator data.
Fetches historical bar data from indicator_bars table for strategy optimization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any
import pandas as pd

try:
    from app.data.postgres import connect_with_retry, get_control_plane_dsn
    from app.config.settings import get_settings
except ImportError:
    connect_with_retry = None
    get_control_plane_dsn = None

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-trade ``pnl_pct`` into the metrics dict the
    ``MultiStrategyOptimizer`` orchestrator consumes.

    PR #283 codex round-3 P2: returns ``winning_trades`` and
    ``losing_trades`` so the orchestrator can compute and populate
    ``BacktestMetrics.profit_factor`` instead of leaving it at the
    default 0.0 — which previously zeroed out the
    ``win_rate * profit_factor`` consistency term in the composite
    score for every real-data run.

    PR #283 codex round-4 P2: ``max_drawdown`` is the worst peak-to-
    trough excursion of the cumulative equity curve, not just the
    single worst trade. With pnls like ``[10, -3, -3]`` the previous
    ``min(pnls) = -3`` underreported the actual equity drawdown of
    ``-6``, letting riskier parameter sets rank too highly.
    """
    import numpy as np

    if not trades:
        return {
            "total_trades": 0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "winning_trades": 0,
            "losing_trades": 0,
            "gross_win": 0.0,
            "gross_loss": 0.0,
        }
    pnls = [t["pnl_pct"] for t in trades]
    winning = [p for p in pnls if p > 0]
    losing = [p for p in pnls if p < 0]

    # Equity-curve drawdown: peak ← max of cumulative pnl so far;
    # drawdown ← min(cumulative - peak) across the run.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        if cumulative - peak < max_dd:
            max_dd = cumulative - peak

    return {
        "total_trades": len(trades),
        "total_pnl": sum(pnls),
        "win_rate": len(winning) / len(trades),
        "sharpe_ratio": np.mean(pnls) / (np.std(pnls) + 1e-6) if len(pnls) > 1 else 0.0,
        "max_drawdown": max_dd,
        "winning_trades": len(winning),
        "losing_trades": len(losing),
        "gross_win": sum(winning),
        "gross_loss": sum(losing),
    }


def _redact_dsn(dsn: Optional[str]) -> str:
    """Return a host/db label safe for logs.

    Connection strings can include the database password — either as a
    ``password=`` keyword or in the URL userinfo (``postgresql://user:pw@host``).
    Slicing the raw DSN leaks credentials into OCI logs (PR #283 codex P2).
    This helper extracts just host + dbname when possible and otherwise
    returns ``"<dsn-redacted>"``.
    """
    if not dsn:
        return "<no-dsn>"
    try:
        # Late import keeps the module importable without psycopg.
        from psycopg.conninfo import conninfo_to_dict  # type: ignore
        parts = conninfo_to_dict(dsn)
    except Exception:
        return "<dsn-redacted>"
    host = parts.get("host", "?")
    port = parts.get("port", "?")
    dbname = parts.get("dbname") or parts.get("database") or "?"
    return f"{host}:{port}/{dbname}"


class PostgresIndicatorLoader:
    """Load real OHLC + indicator data from PostgreSQL indicator_bars table."""

    def __init__(self, dsn: Optional[str] = None, table_name: str = "indicator_bars"):
        """
        Args:
            dsn: PostgreSQL DSN (connection string). If None, uses default from settings.
            table_name: Table name to query (default: indicator_bars)
        """
        self.dsn = dsn or self._default_dsn()
        self.table_name = table_name
        self._conn = None

    @staticmethod
    def _default_dsn() -> str:
        """Get default DSN from settings."""
        try:
            settings = get_settings()
            if get_control_plane_dsn:
                return get_control_plane_dsn(settings)
        except Exception:
            pass
        raise RuntimeError(
            "No PostgreSQL DSN provided and could not determine from settings. "
            "Set CONTROL_PLANE_PG_HOST, CONTROL_PLANE_PG_DB, etc. or pass dsn explicitly."
        )

    def connect(self):
        """Establish PostgreSQL connection."""
        if connect_with_retry is None:
            raise RuntimeError("psycopg library required but not installed")

        try:
            self._conn = connect_with_retry(self.dsn, autocommit=True)
            logger.info("Connected to PostgreSQL: %s", _redact_dsn(self.dsn))
        except Exception as e:
            logger.error("Failed to connect to PostgreSQL %s: %s", _redact_dsn(self.dsn), e)
            raise

    def disconnect(self):
        """Close PostgreSQL connection."""
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _get_connection(self):
        """Get or create connection."""
        if self._conn is None:
            self.connect()
        return self._conn

    def fetch_indicator_bars(
        self,
        underlying_label: str,
        timeframe_seconds: int,
        days_back: int = 20,
        end_date: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """
        Fetch OHLC + indicator data from PostgreSQL.

        Args:
            underlying_label: Instrument name (e.g., "NIFTY", "BANKNIFTY", "NATURALGAS")
            timeframe_seconds: Bar timeframe in seconds (e.g., 300 for 5min)
            days_back: Number of days of historical data to fetch
            end_date: End date for data range (default: today)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume,
                                    atr, rsi, macd, macd_signal, ema_20, ema_30,
                                    ema_50, adx, plus_di, minus_di
        """
        if self._conn is None:
            self.connect()

        end_date = end_date or datetime.now(IST).date()
        start_date = end_date - timedelta(days=days_back)

        # PR #283 codex P1: the baseline ``indicator_bars`` schema in
        # migrations/000_indicator_bars.sql defines OHLC + indicators
        # but NO volume column, so a previous ``COALESCE(vol, 0) AS volume``
        # caused the query to fail before any rows were returned. Volume
        # is not consumed by the optimizer's simulators, so it is simply
        # omitted from the SELECT.
        query = f"""
        SELECT
            ts_start as timestamp,
            o as open,
            h as high,
            l as low,
            c as close,
            atr,
            rsi,
            macd,
            macd_signal,
            ema_20,
            ema_30,
            ema_50,
            adx,
            plus_di,
            minus_di
        FROM {self.table_name}
        WHERE label = %s
            AND timeframe_seconds = %s
            AND ts_start::date >= %s
            AND ts_start::date <= %s
        ORDER BY ts_start ASC
        """

        try:
            with self._get_connection().cursor() as cur:
                cur.execute(query, (underlying_label, timeframe_seconds, start_date, end_date))
                rows = cur.fetchall()

            if not rows:
                logger.warning(
                    f"No data found for {underlying_label} @ {timeframe_seconds}s "
                    f"between {start_date} and {end_date}"
                )
                return pd.DataFrame()

            # Convert to DataFrame
            columns = [
                "timestamp", "open", "high", "low", "close",
                "atr", "rsi", "macd", "macd_signal", "ema_20", "ema_30",
                "ema_50", "adx", "plus_di", "minus_di"
            ]
            df = pd.DataFrame(rows, columns=columns)

            # Ensure timestamp is datetime
            df["timestamp"] = pd.to_datetime(df["timestamp"])

            # Fill NaN indicators with forward fill
            for col in ["atr", "rsi", "macd", "macd_signal", "ema_20", "ema_30", "ema_50", "adx", "plus_di", "minus_di"]:
                if col in df.columns:
                    df[col] = df[col].fillna(method="ffill").fillna(0)

            logger.info(
                f"Loaded {len(df)} bars for {underlying_label} @ {timeframe_seconds}s "
                f"({df['timestamp'].min()} to {df['timestamp'].max()})"
            )

            return df

        except Exception as e:
            logger.error(f"Error fetching indicator bars: {e}")
            raise

    def list_available_underlyings(self) -> List[str]:
        """List available underlying symbols in indicator_bars table."""
        query = f"SELECT DISTINCT label FROM {self.table_name} ORDER BY label"

        try:
            with self._get_connection().cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
            return [row[0] for row in rows]
        except Exception as e:
            logger.error(f"Error listing underlyings: {e}")
            return []

    def list_available_timeframes(self, underlying_label: str) -> List[int]:
        """List available timeframes for an underlying."""
        query = f"""
        SELECT DISTINCT timeframe_seconds FROM {self.table_name}
        WHERE label = %s
        ORDER BY timeframe_seconds
        """

        try:
            with self._get_connection().cursor() as cur:
                cur.execute(query, (underlying_label,))
                rows = cur.fetchall()
            return sorted([row[0] for row in rows])
        except Exception as e:
            logger.error(f"Error listing timeframes: {e}")
            return []

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


class RealDataBacktester:
    """Backtest strategies on real PostgreSQL data instead of synthetic.

    PR #288 codex round-1 P2: ``lookback_days`` is configurable at
    construction so the multi-strategy CLI's ``--lookback-days`` flag (and
    the candidate-writer's ``backtest_window``) reflects the data actually
    loaded — previously the backtester hardcoded ``days_back=20`` while
    the candidate writer recorded whatever the CLI specified, so promoted
    candidates were non-reproducible when ``--lookback-days != 20``.

    PR #288 codex round-5 P2: ``end_date`` is configurable at construction
    so every ``fetch_indicator_bars`` call uses the SAME end date instead
    of re-evaluating ``datetime.now(IST).date()`` per query. Without this,
    a run spanning IST midnight queries different windows for different
    candidates while the writer records a single ``backtest_window`` from
    the start-of-run date.
    """

    def __init__(
        self,
        loader: PostgresIndicatorLoader,
        *,
        lookback_days: int = 20,
        end_date: Optional[Any] = None,
    ):
        self.loader = loader
        self.lookback_days = max(1, int(lookback_days))
        # ``end_date`` may be ``None`` (caller hasn't captured one — the
        # loader will fall back to ``datetime.now(IST).date()`` per query
        # as before) or a ``datetime.date`` value (the captured date).
        self.end_date = end_date

    def backtest_ema20(self, params: Dict[str, Any], underlying_label: str) -> Dict[str, Any]:
        """Backtest EMA20 strategy on real data.

        PR #283 codex P2: data-access failures (loader query error, missing
        columns, connection misconfig) are NOT swallowed into zero-trade
        results — they propagate so the multi-strategy orchestrator's
        per-(strategy,underlying) ``try/except`` can record the actual
        error rather than silently emit JSON as though the run succeeded.
        Empty result sets remain a soft no-op (returns zero metrics) so
        a strategy with no matching bars during the window is not
        treated as a failure.
        """
        df = self.loader.fetch_indicator_bars(
            underlying_label=underlying_label,
            timeframe_seconds=params.get("signal_timeframe", 300),
            days_back=self.lookback_days,
            end_date=self.end_date,
        )

        if df.empty:
            logger.warning(f"No data for {underlying_label}, skipping backtest")
            return {
                "total_trades": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
            }

        return self._simulate_ema20(df, params)

    def backtest_exclusive_nifty_ce(self, params: Dict[str, Any], underlying_label: str) -> Dict[str, Any]:
        """Backtest Exclusive Nifty CE Buy strategy on real data.

        PR #283 codex round-2: queries the live ExclusiveNiftyCeBuy
        timeframe (default 30s — see
        ``EXCLUSIVE_NIFTY_CE_BUY_TIMEFRAME_SECONDS`` in
        docker-compose.oci-live.yml) so the simulator scores on the
        same data stream the live strategy consumes. See
        ``backtest_ema20`` for the surface-failures rationale.
        """
        df = self.loader.fetch_indicator_bars(
            underlying_label=underlying_label,
            timeframe_seconds=int(params.get("timeframe_seconds", 30)),
            days_back=self.lookback_days,
            end_date=self.end_date,
        )

        if df.empty:
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

        return self._simulate_exclusive_nifty_ce(df, params)

    def backtest_put_momentum(self, params: Dict[str, Any], underlying_label: str) -> Dict[str, Any]:
        """Backtest Put Momentum Scalper strategy on real data.

        PR #283 codex round-2: queries 5m bars (the live strategy's
        primary signal timeframe — see
        ``PutMomentumScalperConfig.timeframe_seconds_5m``). The simulator
        scores on the same data stream the live strategy consumes. See
        ``backtest_ema20`` for the surface-failures rationale.
        """
        df = self.loader.fetch_indicator_bars(
            underlying_label=underlying_label,
            timeframe_seconds=300,  # live PM uses 5m as primary signal TF
            days_back=self.lookback_days,
            end_date=self.end_date,
        )

        if df.empty:
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

        return self._simulate_put_momentum(df, params)

    @staticmethod
    def _simulate_ema20(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate EMA20 strategy logic on OHLC data.

        PR #283 codex round-3 P2: honors the ``require_rsi_falling``,
        ``use_adx_filter``, and ``min_adx`` gates the EMA20 parameter
        space emits — previously the real-data simulator entered solely
        on close<EMA + ATR, so the optimizer was ranking trades the
        live strategy would skip.
        """
        ema_period = params.get("ema_period", 20)
        # PR #283 codex P1: ``sl_pct`` / ``tp_pct`` are FRACTIONS in the
        # live EMA20 strategy (0.30 ⇒ 30%). Convert to percent here so
        # the comparison against the percent-scaled ``pnl_pct`` below
        # matches the live exit semantics — without this, a 0.30 input
        # exited at 0.30% (100× tighter than LIVE).
        sl_pct_threshold = params.get("sl_pct", 0.30) * 100.0
        tp_pct_threshold = params.get("tp_pct", 0.30) * 100.0
        min_atr = params.get("min_atr", 0.1)
        require_rsi_falling = bool(params.get("require_rsi_falling", True))
        use_adx_filter = bool(params.get("use_adx_filter", False))
        min_adx = float(params.get("min_adx", 18.0))

        # Calculate EMA
        df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

        # Generate signals
        trades = []
        in_trade = False
        entry_price = 0

        for i in range(ema_period, len(df)):
            if not in_trade:
                close_below_ema = df["close"].iloc[i] < df["ema"].iloc[i]
                atr_ok = df["atr"].iloc[i] >= min_atr if min_atr > 0 else True
                # PR #283 codex round-3 P2 + round-4 P2: RSI-falling and
                # ADX/DI gates mirror the live EMA20 strategy entry
                # filters. The live path requires the last THREE RSI
                # values to be strictly falling
                # (``prev_prev_rsi > prev_rsi > rsi`` —
                # app/strategies/ema20_strategy.py:1290-1304); the
                # single-bar downtick used in round 3 admitted entries
                # live would skip.
                if require_rsi_falling and "rsi" in df.columns and i >= 2:
                    rsi_falling = (
                        df["rsi"].iloc[i - 2]
                        > df["rsi"].iloc[i - 1]
                        > df["rsi"].iloc[i]
                    )
                elif require_rsi_falling:
                    rsi_falling = False
                else:
                    rsi_falling = True
                if use_adx_filter:
                    adx_val = float(df["adx"].iloc[i]) if "adx" in df.columns else 0.0
                    plus_di_val = float(df["plus_di"].iloc[i]) if "plus_di" in df.columns else 0.0
                    minus_di_val = float(df["minus_di"].iloc[i]) if "minus_di" in df.columns else 0.0
                    adx_ok = adx_val >= min_adx
                    # EMA20 is a short-when-below-EMA strategy; require
                    # bearish DI bias when the ADX filter is on.
                    di_ok = minus_di_val > plus_di_val
                else:
                    adx_ok = True
                    di_ok = True

                if close_below_ema and atr_ok and rsi_falling and adx_ok and di_ok:
                    in_trade = True
                    entry_price = df["close"].iloc[i]

            else:
                # Exit conditions. ``pnl_pct`` is a percentage (× 100).
                current_price = df["close"].iloc[i]
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

                if (
                    pnl_pct <= -sl_pct_threshold
                    or pnl_pct >= tp_pct_threshold
                    or i == len(df) - 1
                ):
                    in_trade = False
                    trades.append({
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl_pct": pnl_pct,
                    })

        return _trade_stats(trades)

    @staticmethod
    def _simulate_exclusive_nifty_ce(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate Exclusive Nifty CE Buy strategy.

        PR #283 codex round-2: parameter names and entry gates mirror the
        live ``ExclusiveNiftyCeBuyStrategy._compute_buy_signal`` so the
        optimizer scores the same regime the live strategy will actually
        enter on, and the resulting ``best_parameters`` map to keys the
        live config consumes.

        Live entry contract (approximated here):
          - trend_ok:     ema20 > ema50
          - rsi_ok:       rsi_min < rsi < rsi_max
          - rsi_rising:   3 consecutive bars of rising RSI
                          (matches live _compute_buy_signal exactly;
                          PR #283 codex round-3 P2).
          - above_ema20:  close > ema20 + ema_atr_buffer * atr
          - macd_ok:      FRESH cross-up
                          (prev_macd <= prev_macd_signal AND
                          macd > macd_signal) AND macd_hist >= macd_hist_min.
                          PR #283 codex round-3 P2 — stale bullish MACD
                          continuations no longer trigger entry, matching
                          the live _compute_buy_signal cross check.
          - adx_ok:       adx >= min_adx
          - di_ok:        |plus_di - minus_di| >= min_di_spread
                          AND plus_di > minus_di
        Live exit contract (approximated here):
          - sl_atr / tp_atr:  ATR-scaled stop and take-profit on the
                              underlying CE buy (long).
          - ema_fail_bars + ema_fail_buffer_atr:  exit after N
                              consecutive bars where
                              close < ema20 - ema_fail_buffer_atr * atr.
                              PR #283 codex round-6 P2: the EXIT
                              buffer is a separate live config field
                              (``ema_fail_buffer_atr``); previously
                              the simulator reused the entry buffer
                              (``ema_atr_buffer``) for the exit,
                              tying two independently-tunable knobs
                              together and ranking candidates with
                              the wrong fail threshold.

        Volume gate (``vol_ok``) and the MACD near-cross fallback are
        intentionally omitted — the baseline indicator_bars schema has
        no volume column and the buffers needed for the near-cross are
        not available in a bar-by-bar replay.
        """

        # Match the live config keys (app/strategies/exclusive_nifty_ce_buy.py).
        rsi_min = float(params.get("rsi_min", 58.0))
        rsi_max = float(params.get("rsi_max", 72.0))
        sl_atr = float(params.get("sl_atr", 2.2))
        tp_atr = float(params.get("tp_atr", 2.5))
        macd_hist_min = float(params.get("macd_hist_min", 0.30))
        ema_atr_buffer = float(params.get("ema_atr_buffer", 0.05))
        # PR #283 codex round-6 P2: live ECN exit uses a separate
        # ``ema_fail_buffer_atr``. Default mirrors the live config's
        # 0.10 so a candidate that doesn't tune this field continues
        # to score on the same threshold the live strategy would.
        ema_fail_buffer_atr = float(params.get("ema_fail_buffer_atr", 0.10))
        min_adx = float(params.get("min_adx", 20.0))
        min_di_spread = float(params.get("min_di_spread", 5.0))
        ema_fail_bars = int(params.get("ema_fail_bars", 3))

        # Use ema_20 / ema_50 from indicator_bars if present (live names);
        # otherwise compute as a fallback so a partial schema doesn't
        # silently zero the run.
        if "ema_20" in df.columns and df["ema_20"].notna().any():
            ema20 = df["ema_20"]
        else:
            ema20 = df["close"].ewm(span=20, adjust=False).mean()
        if "ema_50" in df.columns and df["ema_50"].notna().any():
            ema50 = df["ema_50"]
        else:
            ema50 = df["close"].ewm(span=50, adjust=False).mean()

        macd_hist = (df.get("macd", 0) - df.get("macd_signal", 0))
        if hasattr(macd_hist, "fillna"):
            macd_hist = macd_hist.fillna(0)

        trades = []
        in_trade = False
        entry_price = 0.0
        entry_atr = 0.0
        ema_fail_count = 0

        # Need at least 50 bars to have a meaningful ema_50.
        for i in range(50, len(df)):
            close_i = float(df["close"].iloc[i])
            atr_i = float(df["atr"].iloc[i]) if "atr" in df.columns else 0.0
            rsi_i = float(df["rsi"].iloc[i]) if "rsi" in df.columns else 0.0
            rsi_prev = float(df["rsi"].iloc[i - 1]) if "rsi" in df.columns else 0.0
            ema20_i = float(ema20.iloc[i])
            ema50_i = float(ema50.iloc[i])
            adx_i = float(df["adx"].iloc[i]) if "adx" in df.columns else 0.0
            plus_di_i = float(df["plus_di"].iloc[i]) if "plus_di" in df.columns else 0.0
            minus_di_i = float(df["minus_di"].iloc[i]) if "minus_di" in df.columns else 0.0
            macd_i = float(df.get("macd", pd.Series([0.0] * len(df))).iloc[i])
            macd_signal_i = float(df.get("macd_signal", pd.Series([0.0] * len(df))).iloc[i])
            macd_hist_i = macd_i - macd_signal_i

            if not in_trade:
                # Entry gates — approximation of the live _compute_buy_signal.
                trend_ok = ema20_i > ema50_i
                rsi_ok = rsi_min < rsi_i < rsi_max
                # PR #283 codex round-3 P2: live requires THREE consecutive
                # bars of rising RSI (rsi[-1] > rsi[-2] > rsi[-3]). The
                # earlier two-bar window admitted one-bar bounces after a
                # decline that live would reject.
                if i >= 2:
                    rsi_prev2 = float(df["rsi"].iloc[i - 2]) if "rsi" in df.columns else 0.0
                    rsi_rising = rsi_i > rsi_prev > rsi_prev2
                else:
                    rsi_rising = False
                above_ema20 = close_i > (ema20_i + ema_atr_buffer * atr_i)
                # PR #283 codex round-3 P2: live requires a FRESH cross-up
                # (prev_macd <= prev_macd_signal AND current macd > macd_signal),
                # not just a current bullish state. Stale crossed-up
                # continuations admitted entries live would reject.
                prev_macd = float(
                    df.get("macd", pd.Series([0.0] * len(df))).iloc[i - 1]
                )
                prev_macd_signal = float(
                    df.get("macd_signal", pd.Series([0.0] * len(df))).iloc[i - 1]
                )
                macd_cross_up = (
                    prev_macd <= prev_macd_signal and macd_i > macd_signal_i
                )
                macd_ok = macd_cross_up and macd_hist_i >= macd_hist_min
                adx_ok = adx_i >= min_adx
                di_spread = abs(plus_di_i - minus_di_i)
                di_ok = di_spread >= min_di_spread and plus_di_i > minus_di_i

                # PR #283 codex round-4 P2: live ``_compute_buy_signal``
                # also requires ``mom_ok`` (ret_1 > 0 AND ret_5 > 0) and
                # ``vol_ok`` (recent 20-bar volatility above a dynamic
                # threshold) — see app/strategies/exclusive_nifty_ce_buy.py:
                # 1162-1172. Without these, the simulator opens entries
                # on flat/illiquid regimes that the live strategy would
                # reject.
                if i >= 5:
                    ret_1 = (
                        (close_i - float(df["close"].iloc[i - 1]))
                        / float(df["close"].iloc[i - 1])
                        if float(df["close"].iloc[i - 1]) > 0
                        else 0.0
                    )
                    ret_5 = (
                        (close_i - float(df["close"].iloc[i - 5]))
                        / float(df["close"].iloc[i - 5])
                        if float(df["close"].iloc[i - 5]) > 0
                        else 0.0
                    )
                else:
                    ret_1 = 0.0
                    ret_5 = 0.0
                mom_ok = ret_1 > 0 and ret_5 > 0

                # ``vol_ok`` proxy: 20-bar realised volatility of close
                # returns must be above a floor proportional to ATR/close.
                # Live computes a dynamic ``vol_threshold`` from intraday
                # state; this floor uses the simpler ``atr/close`` ratio
                # which is non-zero in the same regimes the live gate
                # considers tradeable.
                vol_floor = max(0.0005, atr_i / close_i if close_i > 0 else 0.0)
                if i >= 20:
                    recent_closes = df["close"].iloc[i - 20: i].astype(float)
                    recent_returns = recent_closes.pct_change().dropna()
                    vol_20 = float(recent_returns.std()) if not recent_returns.empty else 0.0
                else:
                    vol_20 = 0.0
                vol_ok = vol_20 >= vol_floor

                if (
                    trend_ok
                    and rsi_ok
                    and rsi_rising
                    and above_ema20
                    and macd_ok
                    and adx_ok
                    and di_ok
                    and mom_ok
                    and vol_ok
                ):
                    in_trade = True
                    entry_price = close_i
                    entry_atr = atr_i if atr_i > 0 else max(close_i * 0.001, 1.0)
                    ema_fail_count = 0
                continue

            # Exit: ATR-based SL / TP on a long CE (proxied by underlying move).
            sl_price = entry_price - sl_atr * entry_atr
            tp_price = entry_price + tp_atr * entry_atr
            below_ema_threshold = close_i < (ema20_i - ema_fail_buffer_atr * atr_i)
            ema_fail_count = ema_fail_count + 1 if below_ema_threshold else 0
            ema_fail_exit = ema_fail_count >= ema_fail_bars

            stop_hit = close_i <= sl_price
            target_hit = close_i >= tp_price
            time_stop = i == len(df) - 1

            if stop_hit or target_hit or ema_fail_exit or time_stop:
                in_trade = False
                pnl_pct = ((close_i - entry_price) / entry_price) * 100
                trades.append({"entry": entry_price, "exit": close_i, "pnl_pct": pnl_pct})

        return _trade_stats(trades)

    @staticmethod
    def _simulate_put_momentum(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate Put Momentum Scalper strategy.

        PR #283 codex round-2: parameter names mirror
        ``PutMomentumScalperConfig`` (see app/strategies/put_momentum_scalper.py)
        and exit logic uses option-premium thresholds via an
        ATM-delta-proxy mapping so the resulting ``best_parameters`` can
        be applied to the live strategy.

        Live entry contract (approximated here):
          - 15m downtrend proxy: close < ema_50  (live uses a separate
            15m EMA20 + close-below check; collapsed here to a single
            slow-EMA cross check on the 5m frame since we don't load the
            15m series in this PR).
          - 5m breakdown:        close < lowest low over the prior
                                 ``lookback_breakdown_bars`` bars.
          - rsi range:           rsi_min <= rsi <= rsi_max
          - rsi falling:         ``rsi_falling_bars_required`` consecutive
                                 bars of declining RSI.
          - min_atr_ratio:       atr / close >= min_atr_ratio
                                 (rejects flat / illiquid regimes).
          - macd_bearish:        macd < macd_signal AND a FRESH negative
                                 cross (prev_macd >= prev_macd_signal AND
                                 current macd < macd_signal). PR #283
                                 codex round-3 P2 — without this the
                                 simulator opened breakdown trades while
                                 MACD was still bullish or stale-crossed,
                                 which the live strategy rejects.
        Live exit contract (approximated here):
          - option_sl_pct:       option-premium stop. Proxied by mapping
                                 a 1× ``option_sl_pct`` move on the
                                 underlying to a 5× option premium move
                                 (ATM-put delta ≈ 0.5, gamma ≈ small)
                                 — see ``_DELTA_PROXY`` below.
          - final_tp_r:          ``R`` is the initial option-premium
                                 risk distance. Final TP is ``final_tp_r``
                                 × R (default 1.5).
          - max_bars_in_trade:   time stop.

        PR #283 codex round-4 P2: live ``on_tick`` only exits on stop,
        final_tp, or EOD — it never books a partial exit at
        ``partial_tp_r``. The previous simulator booked a half-sized
        partial when ``option_pnl_pct >= partial_tp_r * R``, which
        inflated trade counts / total_pnl / win_rate for parameter sets
        that frequently tagged the partial level. The partial-exit
        branch is removed; ``partial_tp_r`` is still sampled in the
        parameter space because the live config exposes it (the live
        strategy carries it forward in ``OptionPosition.partial_tp``
        even though ``on_tick`` does not exit on it), so the optimizer
        can tune the value for the day the live exit path is extended
        to honour it.

        The volume / VWAP gates and the explicit 15m EMA20 + price-vs-VWAP
        check from the live strategy are omitted because the baseline
        indicator_bars schema does not carry volume / VWAP / 15m bars.
        Documented limitation; codex round-2 reviewer.
        """

        # Match the live config keys (PutMomentumScalperConfig).
        rsi_min = float(params.get("rsi_min", 25.0))
        rsi_max = float(params.get("rsi_max", 45.0))
        min_atr_ratio = float(params.get("min_atr_ratio", 0.0015))
        option_sl_pct = float(params.get("option_sl_pct", 0.25))
        partial_tp_r = float(params.get("partial_tp_r", 1.0))
        final_tp_r = float(params.get("final_tp_r", 1.5))
        rsi_falling_bars_required = int(params.get("rsi_falling_bars_required", 2))
        lookback_breakdown_bars = int(params.get("lookback_breakdown_bars", 10))
        max_bars_in_trade = int(params.get("max_bars_in_trade", 8))

        # ATM-put delta-proxy: an X% adverse move on the UNDERLYING maps
        # to roughly ``_DELTA_PROXY × X%`` on the option premium. 5× is a
        # rough but commonly-used short-tenor ATM ratio that lets the
        # ``option_sl_pct`` knob produce comparable exit timing to LIVE.
        # A precise option-pricing path would need IV + days-to-expiry
        # data the baseline indicator_bars does not carry.
        _DELTA_PROXY = 5.0

        if "ema_50" in df.columns and df["ema_50"].notna().any():
            ema50 = df["ema_50"]
        else:
            ema50 = df["close"].ewm(span=50, adjust=False).mean()
        # PR #283 codex round-4 P2: live PM 5m gate rejects whenever the
        # candle closes at or above either EMA20 OR EMA50. Previously
        # only EMA50 was checked, so the simulator could open trades on
        # bars where ``close < ema50`` but ``close >= ema20`` — live
        # would skip them.
        if "ema_20" in df.columns and df["ema_20"].notna().any():
            ema20 = df["ema_20"]
        else:
            ema20 = df["close"].ewm(span=20, adjust=False).mean()

        trades = []
        in_trade = False
        entry_price = 0.0
        bars_in_trade = 0
        initial_r_pct = 0.0  # option-premium % risk distance from entry
        # PR #283 codex round-6 P2: track the breakdown high at entry so
        # ``_maybe_invalidate`` can fire if the underlying reverses back
        # above it OR back above EMA20 — both are live exit triggers
        # (``put_momentum_scalper.py``) that arrive BEFORE the SL /
        # final / time-stop checks.
        breakdown_high_at_entry = 0.0

        start = max(50, lookback_breakdown_bars + rsi_falling_bars_required + 1)
        for i in range(start, len(df)):
            close_i = float(df["close"].iloc[i])
            atr_i = float(df["atr"].iloc[i]) if "atr" in df.columns else 0.0
            rsi_i = float(df["rsi"].iloc[i]) if "rsi" in df.columns else 0.0
            ema50_i = float(ema50.iloc[i])
            ema20_i = float(ema20.iloc[i])

            if not in_trade:
                # 15m downtrend proxy: 5m close < EMA50 AND close < EMA20.
                downtrend_proxy = close_i < ema50_i and close_i < ema20_i
                # 5m breakdown: close below the prior swing low.
                prior_low = df["low"].iloc[i - lookback_breakdown_bars:i].min()
                breakdown = close_i < float(prior_low)
                rsi_ok = rsi_min <= rsi_i <= rsi_max
                # rsi_falling_bars_required consecutive declining RSI bars.
                rsi_window = df["rsi"].iloc[
                    i - rsi_falling_bars_required: i + 1
                ].to_list()
                rsi_falling = all(
                    rsi_window[j] < rsi_window[j - 1] for j in range(1, len(rsi_window))
                ) if len(rsi_window) >= 2 else False
                # Volatility floor.
                atr_ratio = (atr_i / close_i) if close_i > 0 else 0.0
                vol_ok = atr_ratio >= min_atr_ratio

                # PR #283 codex round-3 P2: bearish MACD with fresh
                # negative cross. The live PutMomentumScalperStrategy
                # only takes entries when MACD has freshly crossed below
                # its signal line on the entry bar.
                macd_i = float(df.get("macd", pd.Series([0.0] * len(df))).iloc[i])
                macd_signal_i = float(
                    df.get("macd_signal", pd.Series([0.0] * len(df))).iloc[i]
                )
                prev_macd = float(
                    df.get("macd", pd.Series([0.0] * len(df))).iloc[i - 1]
                )
                prev_macd_signal = float(
                    df.get("macd_signal", pd.Series([0.0] * len(df))).iloc[i - 1]
                )
                macd_cross_down = (
                    prev_macd >= prev_macd_signal and macd_i < macd_signal_i
                )
                macd_bearish_ok = macd_cross_down and macd_i < macd_signal_i

                if (
                    downtrend_proxy
                    and breakdown
                    and rsi_ok
                    and rsi_falling
                    and vol_ok
                    and macd_bearish_ok
                ):
                    in_trade = True
                    entry_price = close_i
                    bars_in_trade = 0
                    # option-premium SL % is the user knob; that's the
                    # initial R the final TP target is scaled to.
                    initial_r_pct = option_sl_pct * 100.0
                    # PR #283 codex round-6 P2: live ``OptionPosition``
                    # captures the breakdown-high (swing high BEFORE
                    # the breakdown bar). Used by ``_maybe_invalidate``
                    # below.
                    breakdown_high_at_entry = float(
                        df["high"].iloc[i - lookback_breakdown_bars:i].max()
                    )
                continue

            bars_in_trade += 1
            # PUT trade is short-direction on underlying:
            # underlying drop is a WIN, underlying rise is a LOSS.
            underlying_pct = ((entry_price - close_i) / entry_price) * 100
            option_pnl_pct = underlying_pct * _DELTA_PROXY

            # PR #283 codex round-6 P2: live ``_maybe_invalidate`` exits
            # before SL / final / time stops when the underlying reverses
            # back above the breakdown high OR back above EMA20.
            invalidation = (
                close_i > breakdown_high_at_entry
                or close_i > ema20_i
            )
            stop_hit = option_pnl_pct <= -initial_r_pct
            final_hit = option_pnl_pct >= final_tp_r * initial_r_pct
            time_stop = bars_in_trade >= max_bars_in_trade or i == len(df) - 1

            # PR #283 codex round-4 P2: live ``on_tick`` only exits on
            # stop / final_tp / EOD (plus the round-6 invalidation
            # above). Partial-tp tagging never closes a position in
            # live trading, so booking a half-sized partial exit here
            # inflated trade counts and PnL for parameter sets that
            # frequently tagged ``partial_tp_r``. Reference unused
            # locals to keep linters quiet without altering behaviour.
            _ = partial_tp_r

            if invalidation or stop_hit or final_hit or time_stop:
                in_trade = False
                trades.append({
                    "entry": entry_price,
                    "exit": close_i,
                    "pnl_pct": option_pnl_pct,
                })

        return _trade_stats(trades)
