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
    """Backtest strategies on real PostgreSQL data instead of synthetic."""

    def __init__(self, loader: PostgresIndicatorLoader):
        self.loader = loader

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
            days_back=20,
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
            days_back=20,
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
            days_back=20,
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
        # PR #283 codex round-11 P2: live ``_passes_adx_filter`` enforces
        # BOTH bearish DI bias AND ``min_di_spread`` when ADX is on (see
        # ``app/strategies/ema20_strategy.py``). The simulator only
        # checked ``minus_di > plus_di``; ADX-enabled candidates with
        # narrow DI spread were admitted here even though live rejects
        # them. Yaml-deployed default is ``min_di_spread: 0.0`` so the
        # simulator default keeps it permissive for legacy callers; the
        # optimizer can sample stricter values.
        min_di_spread = float(params.get("min_di_spread", 0.0))

        # PR #283 codex round-11 P2: live EMA20 also gates intraday
        # entries via ``first_entry_time`` (default 09:30 from the
        # enabled yaml) and forces square-off at ``square_off_time``
        # (default 15:00). The simulator was admitting entries from
        # the very first bar and carrying them to the end of the
        # frame. ``params`` overrides allow optimizer tuning.
        from datetime import time as _time

        def _parse_hhmm(s, default):
            try:
                hh, mm = s.split(":")
                return _time(int(hh), int(mm))
            except Exception:
                return default

        first_entry_t = _parse_hhmm(
            str(params.get("first_entry_time", "9:30")), _time(9, 30)
        )
        square_off_t = _parse_hhmm(
            str(params.get("square_off_time", "15:00")), _time(15, 0)
        )

        # Build IST time-of-day index for the entry/squareoff gate.
        ist_tod = None
        if "timestamp" in df.columns:
            try:
                ts = pd.to_datetime(df["timestamp"])
                if ts.dt.tz is None:
                    ts = ts.dt.tz_localize("UTC")
                ist_tod = ts.dt.tz_convert("Asia/Kolkata").dt.time
            except Exception:
                ist_tod = None

        def _within_entry_window(idx):
            if ist_tod is None:
                return True
            tod = ist_tod.iloc[idx]
            return first_entry_t <= tod < square_off_t

        def _past_squareoff(idx):
            if ist_tod is None:
                return False
            return ist_tod.iloc[idx] >= square_off_t

        # Calculate EMA
        df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

        # Generate signals
        trades = []
        in_trade = False
        entry_price = 0

        for i in range(ema_period, len(df)):
            if not in_trade:
                if not _within_entry_window(i):
                    continue
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
                    # PR #283 codex round-11 P2: live also requires the
                    # DI spread to clear ``min_di_spread``. Without
                    # this, ADX-enabled candidates with narrow DI
                    # spread were admitted even though live rejects.
                    di_spread_abs = abs(plus_di_val - minus_di_val)
                    di_ok = (
                        minus_di_val > plus_di_val
                        and di_spread_abs >= min_di_spread
                    )
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

                # PR #283 codex round-11 P2: force exit at
                # ``square_off_time`` so positions don't carry
                # overnight when the live strategy would have flattened.
                squareoff_hit = _past_squareoff(i)
                if (
                    pnl_pct <= -sl_pct_threshold
                    or pnl_pct >= tp_pct_threshold
                    or squareoff_hit
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
        # PR #283 codex round-7 P2: live ECN config fields for the
        # post-late-start TP cap and the post-exit cooldown.
        cooldown_bars_cfg = int(params.get("cooldown_bars", 2))
        late_tp_cap_atr = float(params.get("late_tp_cap_atr", 2.6))
        # PR #283 codex round-12 P2: live ECN trailing-EMA exit
        # (TRAIL_EMA20). Once underlying has moved at least
        # ``trail_active_atr * entry_atr`` in favor, an intra-bar dip
        # below ``ema20 - trail_cushion_atr * atr`` exits the trade
        # before the regular EMA-fail count triggers. Late session
        # tightens both knobs.
        trail_active_atr_cfg = float(params.get("trail_active_atr", 0.8))
        trail_cushion_atr_cfg = float(params.get("trail_cushion_atr", 0.16))
        late_trail_active_atr_cfg = float(
            params.get("late_trail_active_atr", 0.6)
        )
        late_trail_cushion_cfg = float(params.get("late_trail_cushion", 0.08))

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

        # PR #283 codex round-7 P2: convert ``df["timestamp"]`` to IST
        # so ECN entries are gated by session window and exits at
        # squareoff time. Same infra as the PM simulator.
        ist_time_of_day = None
        if "timestamp" in df.columns:
            try:
                ts = pd.to_datetime(df["timestamp"])
                if ts.dt.tz is None:
                    ts = ts.dt.tz_localize("UTC")
                ts = ts.dt.tz_convert("Asia/Kolkata")
                ist_time_of_day = ts.dt.time
            except Exception:
                ist_time_of_day = None

        from datetime import time as _time

        # PR #283 codex round-8 P2: align with the LIVE ECN config in
        # ``app/config/strategy_env.yaml`` (session_start=10:15,
        # late_start=14:45, last_entry_time=14:45, square_off_time=15:15).
        # The earlier 09:16 / 14:00 defaults were lifted from the
        # strategy class's hard-coded fallbacks rather than the enabled
        # production config, so the simulator was admitting entries
        # 1 hour before live and applying the late-TP cap 45 minutes
        # too early. Each is overridable via ``params`` so the
        # optimizer can tune them if needed.
        def _parse_hhmm(s: str, default: tuple[int, int]) -> "tuple[int, int]":
            try:
                hh, mm = s.split(":")
                return int(hh), int(mm)
            except Exception:
                return default

        session_hh, session_mm = _parse_hhmm(
            str(params.get("session_start", "10:15")), (10, 15)
        )
        late_hh, late_mm = _parse_hhmm(
            str(params.get("late_start", "14:45")), (14, 45)
        )
        last_entry_hh, last_entry_mm = _parse_hhmm(
            str(params.get("last_entry_time", "14:45")), (14, 45)
        )
        # PR #283 codex round-9 P2: live ``ExclusiveNiftyCeBuyStrategy``
        # reads the key ``squareoff_time`` (one word) — see
        # ``app/strategies/exclusive_nifty_ce_buy.py`` and the enabled
        # yaml ``square_off_time`` (two words). Accept BOTH spellings so
        # candidates persisted under either name score against the
        # correct exit time.
        squareoff_raw = params.get("squareoff_time")
        if squareoff_raw is None:
            squareoff_raw = params.get("square_off_time", "15:15")
        squareoff_hh, squareoff_mm = _parse_hhmm(str(squareoff_raw), (15, 15))
        _ECN_SESSION_START = _time(session_hh, session_mm)
        _ECN_LATE_START = _time(late_hh, late_mm)
        _ECN_LAST_ENTRY = _time(last_entry_hh, last_entry_mm)
        _ECN_SQUAREOFF = _time(squareoff_hh, squareoff_mm)

        # PR #283 codex round-8 P2: live ECN config gates entry on
        # ``state.trades_today < self.max_trades_per_day`` (default 1).
        # The simulator must enforce the same cap so an optimizer can't
        # rank a parameter set on multi-entry sessions production
        # would never have placed.
        #
        # PR #283 codex round-9 P2: live ECN treats ``max_trades_per_day=0``
        # as UNLIMITED (the explicit comment in
        # ``exclusive_nifty_ce_buy.py`` notes "0 = unlimited"). The
        # previous ``trades_today >= 0`` test blocked every entry for
        # the entire backtest. ``<= 0`` now means "no cap".
        max_trades_per_day_cfg = int(params.get("max_trades_per_day", 1))

        # PR #283 codex round-11 P2: derive the timeframe seconds from
        # the actual bar spacing in the frame so the next-bar start
        # time matches ``candle.end_ts = ts_start + timeframe_seconds``.
        # Probing the next stored row is wrong when the frame has gaps
        # (overnight, halts) — the next row could be hours later, and
        # the gate would incorrectly reject a 14:45 signal because the
        # next ROW is 09:30 the following day.
        timeframe_seconds = 300  # default 5min
        if "timestamp" in df.columns and len(df) >= 2:
            try:
                ts_series = pd.to_datetime(df["timestamp"])
                deltas = ts_series.diff().dropna()
                if not deltas.empty:
                    # Most common positive delta = bar interval.
                    timeframe_seconds = int(
                        max(1, deltas.dt.total_seconds().mode().iloc[0])
                    )
            except Exception:
                pass
        _BAR_INTERVAL = pd.Timedelta(seconds=timeframe_seconds)

        # Pre-compute each bar's END time-of-day in IST so the entry
        # window check uses ``candle.end_ts`` deterministically.
        ist_end_of_day = None
        if "timestamp" in df.columns:
            try:
                ts = pd.to_datetime(df["timestamp"])
                if ts.dt.tz is None:
                    ts = ts.dt.tz_localize("UTC")
                ts = ts.dt.tz_convert("Asia/Kolkata")
                ist_end_of_day = (ts + _BAR_INTERVAL).dt.time
            except Exception:
                ist_end_of_day = None

        def _within_ecn_entry_window(idx: int) -> bool:
            """PR #283 codex round-10/11 P2: live ECN evaluates the
            entry window against the NEXT bar's start time
            (``next_bar_start = candle.end_ts = ts_start +
            timeframe_seconds``), not the signal bar's ``ts_start``.
            A 30s signal bar at 14:45:00 has ``next_bar_start =
            14:45:30`` and is rejected against ``last_entry_time =
            14:45``. The probe uses the derived ``timeframe_seconds``
            so it works correctly across overnight gaps and trading
            halts (where probing the next STORED row would be hours/
            days late)."""
            if ist_end_of_day is None:
                return True
            tod = ist_end_of_day.iloc[idx]
            return _ECN_SESSION_START <= tod <= _ECN_LAST_ENTRY

        def _ecn_past_squareoff(idx: int) -> bool:
            if ist_time_of_day is None:
                return False
            return ist_time_of_day.iloc[idx] >= _ECN_SQUAREOFF

        def _ecn_is_late(idx: int) -> bool:
            if ist_time_of_day is None:
                return False
            return ist_time_of_day.iloc[idx] >= _ECN_LATE_START

        trades = []
        in_trade = False
        entry_price = 0.0
        entry_atr = 0.0
        ema_fail_count = 0
        # PR #283 codex round-7 P2: track cooldown bars between exits
        # (matches live ``state.cooldown_bars``). Live decrements the
        # counter on the EXIT bar (before any re-entry check), so a
        # ``cooldown_bars=2`` setting blocks the exit bar + 1 follow-on
        # bar (2 bars total). Here the exit bar is never re-evaluated,
        # so post-exit we set ``cooldown_remaining = cooldown_bars - 1``
        # to block the same total of 2 bars rather than 3.
        cooldown_remaining = 0
        # PR #283 codex round-8 P2: track trades-per-day to enforce
        # ``max_trades_per_day`` (live default 1 for ECN).
        trades_today = 0
        last_seen_date = None

        # Need at least 50 bars to have a meaningful ema_50.
        for i in range(50, len(df)):
            close_i = float(df["close"].iloc[i])
            # PR #283 codex round-7 P2: live ``_manage_position_on_bar``
            # exits on ``candle.low <= sl_level`` / ``candle.high >= tp_level``
            # (intra-bar extremes), not on the bar close.
            high_i = float(df["high"].iloc[i]) if "high" in df.columns else close_i
            low_i = float(df["low"].iloc[i]) if "low" in df.columns else close_i
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

            # PR #283 codex round-8 P2: daily trade-count reset on IST
            # day boundary so ``max_trades_per_day`` is enforced against
            # the same calendar day live uses.
            #
            # PR #283 codex round-9 P2: tz-naive timestamps need the
            # same UTC-localize-then-IST-convert path used to build
            # ``ist_time_of_day`` above. Calling ``tz_convert`` on a
            # tz-naive value raises ``TypeError`` and the previous
            # broad except left ``trades_today`` stuck across days in
            # multi-day synthetic / unit-test fixtures.
            if ist_time_of_day is not None:
                try:
                    ts_raw = pd.to_datetime(df["timestamp"].iloc[i])
                    if getattr(ts_raw, "tzinfo", None) is None:
                        ts_raw = ts_raw.tz_localize("UTC")
                    bar_date = ts_raw.tz_convert("Asia/Kolkata").date()
                except Exception:
                    bar_date = None
                if bar_date is not None and bar_date != last_seen_date:
                    last_seen_date = bar_date
                    trades_today = 0

            if not in_trade:
                # PR #283 codex round-7 P2: post-exit cooldown matches
                # live ``state.cooldown_bars``. While positive, all
                # signal evaluation is skipped.
                if cooldown_remaining > 0:
                    cooldown_remaining -= 1
                    continue
                # PR #283 codex round-8 P2: ``max_trades_per_day`` cap
                # (live ECN default 1). Without this the simulator
                # books a second entry on days with multiple signals
                # that production would never have placed.
                # PR #283 codex round-9 P2: ``max_trades_per_day <= 0``
                # is the live "unlimited" sentinel; skip the cap check
                # entirely in that case.
                if max_trades_per_day_cfg > 0 and trades_today >= max_trades_per_day_cfg:
                    continue
                # PR #283 codex round-7 P2: entry session window
                # (between session_start and last_entry_time).
                if not _within_ecn_entry_window(i):
                    continue
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
                # PR #283 codex round-10 P2: live ECN accepts ALSO a
                # "near-MACD" rising configuration (see
                # exclusive_nifty_ce_buy.py:1118 ``macd_near_cross_up``):
                # MACD still below signal but rising, with both
                # ``macd_div`` and ``macd_hist`` rising vs the prior
                # bar. The simulator was rejecting these entries that
                # live would admit when ``allow_near_macd=True``
                # (live default).
                macd_div_now = macd_i - macd_signal_i
                macd_div_prev = prev_macd - prev_macd_signal
                prev_hist = macd_div_prev  # macd_hist == macd_div in this loader
                # PR #283 codex round-11 P2: yaml-deployed default is
                # ``macd_near: 0.0`` (see ``app/config/strategy_env.yaml``),
                # narrower than the strategy class fallback of 0.40. Match
                # the DEPLOYED configuration so the simulator doesn't open
                # the near-MACD path wider than production.
                allow_near_macd = bool(params.get("allow_near_macd", True))
                macd_near_thresh = float(params.get("macd_near", 0.0))
                macd_near_cross_up = (
                    macd_div_now < 0
                    and macd_div_now > -macd_near_thresh
                    and macd_div_now > macd_div_prev
                    and macd_hist_i > prev_hist
                )
                macd_confirmed = macd_cross_up and macd_hist_i >= macd_hist_min
                macd_ok = macd_confirmed or (allow_near_macd and macd_near_cross_up)
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
            # PR #283 codex round-7 P2: when ``ist_time_of_day >= 14:00``
            # the live strategy applies a tighter ``late_tp_cap_atr``
            # ceiling on the target (default 2.6 × ATR). Without the cap
            # candidates with very large ``tp_atr`` looked profitable in
            # the simulator on bars that live would have closed earlier.
            late_now = _ecn_is_late(i)
            effective_tp_atr = min(tp_atr, late_tp_cap_atr) if late_now else tp_atr
            # PR #283 codex round-12 P2: late-session also tightens the
            # trailing-EMA knobs (``late_trail_active_atr`` and
            # ``late_trail_cushion``).
            eff_trail_active_atr = (
                late_trail_active_atr_cfg if late_now else trail_active_atr_cfg
            )
            eff_trail_cushion = (
                late_trail_cushion_cfg if late_now else trail_cushion_atr_cfg
            )
            sl_price = entry_price - sl_atr * entry_atr
            tp_price = entry_price + effective_tp_atr * entry_atr
            # Trailing-EMA exit (TRAIL_EMA20): fires only AFTER the
            # underlying has moved at least ``trail_active_atr * entry_atr``
            # in favor AND the bar's low pierces ``ema20 - cushion * atr``.
            trail_active_level = entry_price + eff_trail_active_atr * entry_atr
            trail_level = ema20_i - eff_trail_cushion * atr_i
            trail_armed = high_i >= trail_active_level
            trail_exit = trail_armed and low_i < trail_level
            below_ema_threshold = close_i < (ema20_i - ema_fail_buffer_atr * atr_i)
            ema_fail_count = ema_fail_count + 1 if below_ema_threshold else 0
            ema_fail_exit = ema_fail_count >= ema_fail_bars

            # PR #283 codex round-7 P2: live exits on intra-bar extremes,
            # not close — see app/strategies/exclusive_nifty_ce_buy.py
            # ``_manage_position_on_bar``.
            stop_hit = low_i <= sl_price
            target_hit = high_i >= tp_price
            squareoff_exit = _ecn_past_squareoff(i)
            time_stop = i == len(df) - 1

            if (
                stop_hit
                or target_hit
                or trail_exit
                or ema_fail_exit
                or squareoff_exit
                or time_stop
            ):
                in_trade = False
                # When multiple exits trigger on the same bar we can't
                # tell from OHLC which fired first; live priority is
                # SL → TP → TRAIL_EMA20 → EMA_FAIL → squareoff. Mark the
                # exit price at the live-priority level.
                if stop_hit:
                    exit_price = sl_price
                elif target_hit:
                    exit_price = tp_price
                elif trail_exit:
                    exit_price = trail_level
                else:
                    exit_price = close_i
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                trades.append(
                    {"entry": entry_price, "exit": exit_price, "pnl_pct": pnl_pct}
                )
                # PR #283 codex round-8 P2: live ``_manage_position_on_bar``
                # decrements ``state.cooldown_bars`` on the EXIT bar
                # itself (before any cooldown check), so a config of
                # ``cooldown_bars=2`` blocks the exit bar + 1 follow-on
                # bar (2 bars total). Here the exit bar is never
                # re-evaluated for re-entry, so setting
                # ``cooldown_remaining = cooldown_bars`` would block
                # the exit bar PLUS 2 follow-on bars (3 bars total) —
                # one too many. Using ``cooldown_bars - 1`` aligns the
                # total blocked bars with live (clamped to >= 0).
                cooldown_remaining = max(0, cooldown_bars_cfg - 1)
                # PR #283 codex round-8 P2: count the entry against the
                # day's quota so subsequent entries on the same calendar
                # day are blocked by ``max_trades_per_day``.
                trades_today += 1

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

        # PR #283 codex round-7 P2: convert ``df["timestamp"]`` to IST
        # time-of-day so the simulator can honour live PM's entry
        # windows (morning 09:20-11:30 IST + afternoon 13:30-15:00
        # IST) AND the EOD 15:20 IST exit. Without these the
        # simulator can open / hold trades during lunch, late
        # afternoon, or even overnight — none of which live ``on_tick``
        # would permit.
        ist_time_of_day = None
        if "timestamp" in df.columns:
            try:
                ts = pd.to_datetime(df["timestamp"])
                # Localise tz-naive timestamps to UTC, then convert to IST.
                if ts.dt.tz is None:
                    ts = ts.dt.tz_localize("UTC")
                ts = ts.dt.tz_convert("Asia/Kolkata")
                ist_time_of_day = ts.dt.time
            except Exception:
                ist_time_of_day = None

        from datetime import time as _time

        def _parse_hhmm_pm(s, default):
            try:
                hh, mm = s.split(":")
                return _time(int(hh), int(mm))
            except Exception:
                return default

        # PR #283 codex round-12 P2: live PM ``_within_entry_window``
        # (put_momentum_scalper.py:790) checks ``entry_start`` /
        # ``entry_end`` FIRST and only falls back to the
        # morning/afternoon split when those are NOT configured. The
        # deployed yaml for NIFTY / BANKNIFTY uses the single-window
        # path with ``09:20`` / ``14:45``. The previous hard-coded
        # split was admitting bars between 11:30 and 13:30 IST that
        # live would reject.
        entry_start_str = params.get("entry_start")
        entry_end_str = params.get("entry_end")
        if entry_start_str and entry_end_str:
            _SINGLE_WINDOW = (
                _parse_hhmm_pm(str(entry_start_str), _time(9, 20)),
                _parse_hhmm_pm(str(entry_end_str), _time(14, 45)),
            )
        else:
            _SINGLE_WINDOW = None
        _MORNING_START = _parse_hhmm_pm(
            str(params.get("morning_start", "09:20")), _time(9, 20)
        )
        _MORNING_END = _parse_hhmm_pm(
            str(params.get("morning_end", "11:30")), _time(11, 30)
        )
        _AFTERNOON_START = _parse_hhmm_pm(
            str(params.get("afternoon_start", "13:30")), _time(13, 30)
        )
        _AFTERNOON_END = _parse_hhmm_pm(
            str(params.get("afternoon_end", "15:00")), _time(15, 0)
        )
        _EOD = _parse_hhmm_pm(str(params.get("eod", "15:20")), _time(15, 20))

        def _within_entry_window(idx: int) -> bool:
            if ist_time_of_day is None:
                return True  # synthetic / unparseable timestamps — skip gate
            tod = ist_time_of_day.iloc[idx]
            if _SINGLE_WINDOW is not None:
                return _SINGLE_WINDOW[0] <= tod <= _SINGLE_WINDOW[1]
            return (
                _MORNING_START <= tod <= _MORNING_END
                or _AFTERNOON_START <= tod <= _AFTERNOON_END
            )

        def _past_eod(idx: int) -> bool:
            if ist_time_of_day is None:
                return False
            return ist_time_of_day.iloc[idx] >= _EOD

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
                # PR #283 codex round-7 P2: only enter inside the
                # configured morning / afternoon windows. Live
                # ``_within_entry_window`` rejects lunch / pre-session
                # / post-15:00 bars before any signal evaluation.
                if not _within_entry_window(i):
                    continue
                # 15m downtrend proxy: 5m close < EMA50 AND close < EMA20.
                downtrend_proxy = close_i < ema50_i and close_i < ema20_i
                # PR #283 codex round-8 P2: live ``_is_breakdown_bar``
                # (app/strategies/put_momentum_scalper.py:1230) uses
                # ONLY ``candle.low <= min(prior_lows) AND
                # lower_wick_ratio <= 0.30``. Earlier rounds also OR-ed
                # in ``close < prior_low``, which admitted breakdown
                # candles live would reject when the close was below
                # the prior low but the wick ratio exceeded 0.30
                # (large reversal candles). The OR is removed.
                prior_low = float(df["low"].iloc[i - lookback_breakdown_bars:i].min())
                low_i = float(df["low"].iloc[i])
                high_i = float(df["high"].iloc[i])
                bar_range = high_i - low_i
                # Avoid divide-by-zero on doji bars; treat as no wick.
                lower_wick_ratio = (
                    (close_i - low_i) / bar_range if bar_range > 0 else 0.0
                )
                # PR #283 codex round-8 P2: ``<=`` matches live's
                # ``candle.low <= min(lows)``. An equal-low wick
                # candle was previously rejected even though live
                # admits it.
                breakdown = low_i <= prior_low and lower_wick_ratio <= 0.30
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
                    # PR #283 codex round-7 P2: live PM's
                    # ``_enter_position`` captures ``candle.h`` — the
                    # BREAKDOWN CANDLE's own high — not the lookback
                    # window's max. The previous lookback-max made the
                    # invalidation threshold too high (could only fire
                    # if the underlying rose above the multi-day swing
                    # high), so reversals that live would catch were
                    # left open here.
                    breakdown_high_at_entry = float(df["high"].iloc[i])
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
            # PR #283 codex round-7 P2: live ``on_tick`` exits any open
            # PM position at 15:20 IST (EOD square-off). Without this
            # the simulator could carry late-session trades into
            # overnight / next-day bars and book PnL live would never
            # have realized.
            eod_exit = _past_eod(i)
            time_stop = (
                bars_in_trade >= max_bars_in_trade
                or i == len(df) - 1
                or eod_exit
            )

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
