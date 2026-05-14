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
            logger.info(f"Connected to PostgreSQL: {self.dsn[:50]}...")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
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

        query = f"""
        SELECT
            ts_start as timestamp,
            o as open,
            h as high,
            l as low,
            c as close,
            COALESCE(vol, 0) as volume,
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
                "timestamp", "open", "high", "low", "close", "volume",
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
        """Backtest EMA20 strategy on real data."""
        try:
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

            # Simulate EMA20 strategy
            metrics = self._simulate_ema20(df, params)
            return metrics

        except Exception as e:
            logger.error(f"Error backtesting EMA20 on {underlying_label}: {e}")
            return {
                "total_trades": 0,
                "total_pnl": 0,
                "win_rate": 0,
                "sharpe_ratio": 0,
                "max_drawdown": 0,
            }

    def backtest_exclusive_nifty_ce(self, params: Dict[str, Any], underlying_label: str) -> Dict[str, Any]:
        """Backtest Exclusive Nifty CE Buy strategy on real data."""
        try:
            df = self.loader.fetch_indicator_bars(
                underlying_label=underlying_label,
                timeframe_seconds=300,  # 5min bars
                days_back=20,
            )

            if df.empty:
                return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

            metrics = self._simulate_exclusive_nifty_ce(df, params)
            return metrics

        except Exception as e:
            logger.error(f"Error backtesting Exclusive Nifty CE on {underlying_label}: {e}")
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

    def backtest_put_momentum(self, params: Dict[str, Any], underlying_label: str) -> Dict[str, Any]:
        """Backtest Put Momentum Scalper strategy on real data."""
        try:
            df = self.loader.fetch_indicator_bars(
                underlying_label=underlying_label,
                timeframe_seconds=300,  # 5min bars
                days_back=20,
            )

            if df.empty:
                return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

            metrics = self._simulate_put_momentum(df, params)
            return metrics

        except Exception as e:
            logger.error(f"Error backtesting Put Momentum on {underlying_label}: {e}")
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

    @staticmethod
    def _simulate_ema20(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate EMA20 strategy logic on OHLC data."""
        import numpy as np

        ema_period = params.get("ema_period", 20)
        sl_pct = params.get("sl_pct", 0.30)
        tp_pct = params.get("tp_pct", 0.30)
        min_atr = params.get("min_atr", 0.1)

        # Calculate EMA
        df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

        # Generate signals
        trades = []
        in_trade = False
        entry_price = 0
        entry_idx = -1

        for i in range(ema_period, len(df)):
            if not in_trade:
                # Entry condition: close below EMA + ATR check
                if df["close"].iloc[i] < df["ema"].iloc[i] and df["atr"].iloc[i] >= min_atr:
                    in_trade = True
                    entry_price = df["close"].iloc[i]
                    entry_idx = i

            else:
                # Exit conditions
                current_price = df["close"].iloc[i]
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

                if pnl_pct <= -sl_pct or pnl_pct >= tp_pct or i == len(df) - 1:
                    in_trade = False
                    trades.append({
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl_pct": pnl_pct,
                    })

        if not trades:
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0, "sharpe_ratio": 0, "max_drawdown": 0}

        pnls = [t["pnl_pct"] for t in trades]
        wins = len([p for p in pnls if p > 0])

        return {
            "total_trades": len(trades),
            "total_pnl": sum(pnls),
            "win_rate": wins / len(trades) if trades else 0,
            "sharpe_ratio": np.mean(pnls) / (np.std(pnls) + 1e-6) if len(pnls) > 1 else 0,
            "max_drawdown": min(pnls) if pnls else 0,
        }

    @staticmethod
    def _simulate_exclusive_nifty_ce(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate Exclusive Nifty CE Buy strategy."""
        import numpy as np

        # Key parameters for Exclusive Nifty CE
        rsi_threshold = params.get("rsi_threshold", 50)
        vol_threshold = params.get("vol_threshold", 1.2)
        sl_pct = params.get("sl_pct", 0.15)
        tp_pct = params.get("tp_pct", 0.30)

        # Simple simulation
        trades = []
        in_trade = False
        entry_price = 0

        for i in range(1, len(df)):
            if not in_trade and df["rsi"].iloc[i] < rsi_threshold:
                in_trade = True
                entry_price = df["close"].iloc[i]

            elif in_trade:
                current_price = df["close"].iloc[i]
                pnl_pct = ((current_price - entry_price) / entry_price) * 100

                if pnl_pct <= -sl_pct or pnl_pct >= tp_pct or i == len(df) - 1:
                    in_trade = False
                    trades.append({"entry": entry_price, "exit": current_price, "pnl_pct": pnl_pct})

        if not trades:
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

        pnls = [t["pnl_pct"] for t in trades]
        wins = len([p for p in pnls if p > 0])

        return {
            "total_trades": len(trades),
            "total_pnl": sum(pnls),
            "win_rate": wins / len(trades) if trades else 0,
            "sharpe_ratio": np.mean(pnls) / (np.std(pnls) + 1e-6) if len(pnls) > 1 else 0,
            "max_drawdown": min(pnls) if pnls else 0,
        }

    @staticmethod
    def _simulate_put_momentum(df: pd.DataFrame, params: Dict[str, Any]) -> Dict[str, Any]:
        """Simulate Put Momentum Scalper strategy."""
        import numpy as np

        # Key parameters for Put Momentum Scalper
        rsi_min = params.get("rsi_min", 25)
        rsi_max = params.get("rsi_max", 45)
        sl_pct = params.get("sl_pct", 0.25)
        tp_pct = params.get("tp_pct", 0.40)

        trades = []
        in_trade = False
        entry_price = 0

        for i in range(1, len(df)):
            if not in_trade and rsi_min <= df["rsi"].iloc[i] <= rsi_max:
                in_trade = True
                entry_price = df["close"].iloc[i]

            elif in_trade:
                current_price = df["close"].iloc[i]
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

                if pnl_pct <= -sl_pct or pnl_pct >= tp_pct or i == len(df) - 1:
                    in_trade = False
                    trades.append({"entry": entry_price, "exit": current_price, "pnl_pct": pnl_pct})

        if not trades:
            return {"total_trades": 0, "total_pnl": 0, "win_rate": 0}

        pnls = [t["pnl_pct"] for t in trades]
        wins = len([p for p in pnls if p > 0])

        return {
            "total_trades": len(trades),
            "total_pnl": sum(pnls),
            "win_rate": wins / len(trades) if trades else 0,
            "sharpe_ratio": np.mean(pnls) / (np.std(pnls) + 1e-6) if len(pnls) > 1 else 0,
            "max_drawdown": min(pnls) if pnls else 0,
        }
