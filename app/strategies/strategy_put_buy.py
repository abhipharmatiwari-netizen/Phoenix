from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.strategies.put_buy_indicators import atr, ema, macd, rsi, sma
from app.strategies.strategy_config import StrategyConfig


@dataclass
class Signal:
    timestamp: pd.Timestamp
    signal_type: str
    price: float
    reason: str
    meta: Optional[Dict[str, float]] = None


@dataclass
class PositionState:
    entry_time: pd.Timestamp
    entry_price: float
    initial_stop: float
    stop_price: float
    R: float
    target1: float
    target2: float
    qty: float
    remaining_qty: float
    tp1_done: bool = False
    tp1_time: Optional[pd.Timestamp] = None
    tp1_price: Optional[float] = None
    tp1_qty: float = 0.0
    stop_after_tp1: Optional[float] = None
    trail_active: bool = False
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_R: float = 0.0
    realized_rupees: Optional[float] = None
    option_lots: Optional[int] = None
    option_entry_premium: Optional[float] = None
    option_risk_per_lot: Optional[float] = None


class PutBuyStrategy:
    def __init__(self, config: Optional[StrategyConfig] = None) -> None:
        self.config = config or StrategyConfig()
        self._session_start = self._parse_time(self.config.session_start)
        self._session_end = self._parse_time(self.config.session_end)
        self._no_trade_until = self._parse_time(self.config.no_trade_until)
        self._last_entry_time = self._parse_time(self.config.last_entry_time)
        self._eod_exit_time = self._parse_time(self.config.eod_exit_time)

    # ---------- Public API ----------

    def prepare_indicators(
        self, df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Prepare 5m and 15m data with indicators."""
        df = df.copy()
        self._validate_ohlcv(df)
        df["timestamp"] = self._ensure_timezone(df["timestamp"])
        df = df.sort_values("timestamp")

        base_minutes = self._infer_minutes(df["timestamp"])
        if base_minutes <= 1.5:
            df_5m = self._resample_ohlcv(df, "5min")
            df_15m = self._resample_ohlcv(df, "15min")
        elif 4 <= base_minutes <= 6:
            df_5m = df.reset_index(drop=True)
            df_15m = self._resample_ohlcv(df, "15min")
        else:
            raise ValueError(
                "Unsupported input timeframe. Provide 1m or 5m bars."
            )

        df_5m = self._add_indicators_5m(df_5m)
        df_15m = self._add_indicators_15m(df_15m)
        return df_5m, df_15m

    def generate_signals(
        self, df_5m: pd.DataFrame, df_15m: pd.DataFrame
    ) -> List[Signal]:
        _, signals = self._simulate(df_5m, df_15m, options_df=None)
        return signals

    def run_backtest(
        self,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        *,
        options_df: Optional[pd.DataFrame] = None,
    ) -> Tuple[pd.DataFrame, Dict[str, float]]:
        trades, _ = self._simulate(df_5m, df_15m, options_df=options_df)
        trades_df = pd.DataFrame(trades)
        summary = self._compute_performance(trades_df)
        return trades_df, summary

    # ---------- Simulation core ----------

    def _simulate(
        self,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        *,
        options_df: Optional[pd.DataFrame],
    ) -> Tuple[List[Dict[str, object]], List[Signal]]:
        df = self._merge_timeframes(df_5m, df_15m)
        df = df.sort_values("timestamp").reset_index(drop=True)

        options_df = self._prepare_options_df(options_df)

        trades: List[Dict[str, object]] = []
        signals: List[Signal] = []

        position: Optional[PositionState] = None
        pending_entry: Optional[Dict[str, object]] = None
        pullback_active = False
        pullback_high: Optional[float] = None
        stall_count = 0

        current_day: Optional[date] = None
        trades_taken_today = 0
        realized_R_today = 0.0
        consecutive_losses = 0
        stop_trading_today = False

        prev_rsi: Optional[float] = None
        prev_macd_hist: Optional[float] = None
        prev_high: Optional[float] = None

        for idx, row in df.iterrows():
            ts: pd.Timestamp = row["timestamp"]
            day = ts.date()
            if current_day != day:
                current_day = day
                trades_taken_today = 0
                realized_R_today = 0.0
                consecutive_losses = 0
                stop_trading_today = False
                pullback_active = False
                pullback_high = None
                stall_count = 0
                pending_entry = None
                prev_high = None
                if position:
                    self._force_exit(
                        position,
                        ts,
                        row["open"],
                        "EOD",
                        trades,
                        signals,
                        trades_taken_today,
                        realized_R_today,
                        consecutive_losses,
                        options_df,
                    )
                    position = None

            if pending_entry and idx == pending_entry["entry_index"]:
                if self._can_enter(
                    ts,
                    stop_trading_today,
                    trades_taken_today,
                    consecutive_losses,
                    realized_R_today,
                ):
                    entry_price = float(row["open"])
                    atr_val = float(row["atr14"])
                    stop_price = self._calculate_stop_price(
                        entry_price,
                        float(pending_entry["pullback_high"]),
                        atr_val,
                    )
                    R = stop_price - entry_price
                    if R > 0:
                        position = self._open_position(
                            ts,
                            entry_price,
                            stop_price,
                            R,
                            options_df,
                            df,
                            idx,
                        )
                        if position:
                            trades_taken_today += 1
                            signals.append(
                                Signal(
                                    timestamp=ts,
                                    signal_type="BUY_PUT",
                                    price=entry_price,
                                    reason="ENTRY",
                                )
                            )
                            pullback_active = False
                            pullback_high = None
                            stall_count = 0
                    pending_entry = None
                else:
                    pending_entry = None

            if position:
                if ts.time() >= self._eod_exit_time:
                    self._force_exit(
                        position,
                        ts,
                        row["open"],
                        "EOD",
                        trades,
                        signals,
                        trades_taken_today,
                        realized_R_today,
                        consecutive_losses,
                        options_df,
                    )
                    position = None
                else:
                    exited = self._process_exits(
                        position,
                        row,
                        prev_high,
                        trades,
                        signals,
                        trades_taken_today,
                        realized_R_today,
                        consecutive_losses,
                        options_df,
                    )
                    if exited:
                        realized_R_today += position.realized_R
                        if position.realized_R < 0:
                            consecutive_losses += 1
                        else:
                            consecutive_losses = 0
                        if realized_R_today <= -abs(self.config.daily_loss_limit_R):
                            stop_trading_today = True
                        if consecutive_losses >= self.config.max_consecutive_losses:
                            stop_trading_today = True
                        position = None

            if position is None and pending_entry is None:
                if self._can_generate_signal(ts):
                    bearish_mode = self._bear_filter(row)
                    if not bearish_mode:
                        pullback_active = False
                        pullback_high = None
                        stall_count = 0
                    else:
                        approach = self._pullback_approach(row)
                        if approach and not pullback_active:
                            pullback_active = True
                            pullback_high = float(row["high"])
                            stall_count = 0
                        if pullback_active:
                            if pullback_high is None:
                                pullback_high = float(row["high"])
                            else:
                                pullback_high = max(pullback_high, float(row["high"]))
                            if float(row["close"]) > float(row["ema21"]):
                                stall_count = 0
                            else:
                                stall_count += 1

                            if not approach:
                                pullback_active = False
                                pullback_high = None
                                stall_count = 0
                        if (
                            pullback_active
                            and stall_count >= self.config.stall_candles
                            and self._entry_trigger(row, prev_rsi, prev_macd_hist)
                        ):
                            entry_index = idx + self.config.entry_delay_bars
                            if entry_index < len(df):
                                pending_entry = {
                                    "entry_index": entry_index,
                                    "pullback_high": pullback_high,
                                }

            prev_rsi = float(row["rsi14"]) if not np.isnan(row["rsi14"]) else prev_rsi
            prev_macd_hist = (
                float(row["macd_hist"]) if not np.isnan(row["macd_hist"]) else prev_macd_hist
            )
            prev_high = float(row["high"])

        return trades, signals

    # ---------- Indicator + helper methods ----------

    def _add_indicators_5m(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema9"] = ema(df["close"], self.config.ema_5m_fast)
        df["ema21"] = ema(df["close"], self.config.ema_5m_slow)
        df["rsi14"] = rsi(df["close"], self.config.rsi_5m_period)
        macd_line, macd_signal, macd_hist = macd(
            df["close"],
            self.config.macd_fast,
            self.config.macd_slow,
            self.config.macd_signal,
        )
        df["macd"] = macd_line
        df["macd_signal"] = macd_signal
        df["macd_hist"] = macd_hist
        df["atr14"] = atr(df, self.config.atr_5m_period)
        df["vol_sma"] = sma(df["volume"], self.config.vol_sma_period)
        return df

    def _add_indicators_15m(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema50"] = ema(df["close"], self.config.ema_15m_period)
        df["rsi14"] = rsi(df["close"], self.config.rsi_15m_period)
        df["atr14"] = atr(df, self.config.atr_15m_period)
        if self.config.use_atr_filter:
            df["atr_threshold"] = (
                df["atr14"]
                .rolling(
                    window=self.config.atr_percentile_lookback,
                    min_periods=self.config.atr_percentile_lookback,
                )
                .quantile(self.config.atr_min_percentile / 100.0)
            )
        else:
            df["atr_threshold"] = np.nan
        return df

    def _merge_timeframes(
        self, df_5m: pd.DataFrame, df_15m: pd.DataFrame
    ) -> pd.DataFrame:
        df_5m = df_5m.copy()
        df_15m = df_15m.copy()
        df_5m["timestamp"] = self._ensure_timezone(df_5m["timestamp"])
        df_15m["timestamp"] = self._ensure_timezone(df_15m["timestamp"])
        df_5m = df_5m.sort_values("timestamp")
        df_15m = df_15m.sort_values("timestamp")

        cols = ["timestamp", "close", "ema50", "rsi14", "atr14", "atr_threshold"]
        df_15m = df_15m[cols].rename(
            columns={
                "close": "close_15m",
                "ema50": "ema50_15m",
                "rsi14": "rsi14_15m",
                "atr14": "atr14_15m",
                "atr_threshold": "atr_threshold_15m",
            }
        )

        merged = pd.merge_asof(df_5m, df_15m, on="timestamp", direction="backward")
        return merged

    def _bear_filter(self, row: pd.Series) -> bool:
        if np.isnan(row["ema50_15m"]) or np.isnan(row["rsi14_15m"]):
            return False
        cond = (row["close_15m"] < row["ema50_15m"]) and (row["rsi14_15m"] < 50)
        if not cond:
            return False
        if self.config.use_atr_filter:
            if np.isnan(row["atr_threshold_15m"]):
                return False
            return row["atr14_15m"] >= row["atr_threshold_15m"]
        return True

    def _pullback_approach(self, row: pd.Series) -> bool:
        if np.isnan(row["ema21"]) or np.isnan(row["atr14"]):
            return False
        band = self.config.pullback_band * row["atr14"]
        return abs(row["close"] - row["ema21"]) <= band

    def _entry_trigger(
        self,
        row: pd.Series,
        prev_rsi: Optional[float],
        prev_macd_hist: Optional[float],
    ) -> bool:
        if np.isnan(row["ema9"]) or np.isnan(row["rsi14"]) or np.isnan(row["macd_hist"]):
            return False
        if row["close"] >= row["ema9"]:
            return False
        if row["macd_hist"] >= 0:
            return False
        if prev_macd_hist is None or row["macd_hist"] >= prev_macd_hist:
            return False
        if self.config.strict_rsi_cross:
            if prev_rsi is None:
                return False
            rsi_cross = prev_rsi >= self.config.rsi_entry and row["rsi14"] < self.config.rsi_entry
        else:
            rsi_cross = row["rsi14"] < self.config.rsi_entry
        if not rsi_cross:
            return False
        if self.config.use_volume_filter:
            if np.isnan(row["vol_sma"]):
                return False
            if row["volume"] <= row["vol_sma"]:
                return False
        return True

    def _calculate_stop_price(self, entry_price: float, pullback_high: float, atr_val: float) -> float:
        volatility_sl = entry_price + (self.config.atr_mult * atr_val)
        return max(pullback_high, volatility_sl)

    # ---------- Position + exit handling ----------

    def _open_position(
        self,
        ts: pd.Timestamp,
        entry_price: float,
        stop_price: float,
        R: float,
        options_df: Optional[pd.DataFrame],
        df: pd.DataFrame,
        entry_index: int,
    ) -> Optional[PositionState]:
        qty = 1.0
        option_lots = None
        option_entry_premium = None
        option_risk_per_lot = None

        if options_df is not None:
            option_slice = self._select_option_slice(options_df, ts, entry_price)
            option_entry_premium = self._get_option_price(
                option_slice, ts, self.config.option_entry_price_field
            )
            if option_entry_premium is None:
                return None
            if not self._option_liquidity_ok(option_slice, ts):
                return None
            stop_hit_ts = self._find_first_stop_hit_ts(df, entry_index, stop_price)
            stop_premium = 0.0
            if stop_hit_ts is not None:
                stop_price_opt = self._get_option_price(
                    option_slice, stop_hit_ts, self.config.option_exit_price_field
                )
                if stop_price_opt is not None:
                    stop_premium = stop_price_opt
            option_risk_per_lot = max(
                (option_entry_premium - stop_premium) * self.config.lot_size, 0.0
            )
            if option_risk_per_lot <= 0:
                return None
            risk_rupees = self.config.account_equity * self.config.risk_per_trade_pct
            option_lots = int(risk_rupees // option_risk_per_lot)
            if option_lots < 1:
                return None
            qty = float(option_lots)

        return PositionState(
            entry_time=ts,
            entry_price=entry_price,
            initial_stop=stop_price,
            stop_price=stop_price,
            R=R,
            target1=entry_price - (self.config.tp1_R * R),
            target2=entry_price - (self.config.tp2_R * R),
            qty=qty,
            remaining_qty=qty,
            option_lots=option_lots,
            option_entry_premium=option_entry_premium,
            option_risk_per_lot=option_risk_per_lot,
        )

    def _process_exits(
        self,
        position: PositionState,
        row: pd.Series,
        prev_high: Optional[float],
        trades: List[Dict[str, object]],
        signals: List[Signal],
        trades_taken_today: int,
        realized_R_today: float,
        consecutive_losses: int,
        options_df: Optional[pd.DataFrame],
    ) -> bool:
        ts: pd.Timestamp = row["timestamp"]
        high = float(row["high"])
        low = float(row["low"])

        stop_hit = high >= position.stop_price
        tp2_hit = low <= position.target2
        tp1_hit = (not position.tp1_done) and (low <= position.target1)

        if stop_hit:
            reason = "TRAIL" if position.trail_active else "STOP"
            self._close_position(
                position,
                ts,
                position.stop_price,
                reason,
                trades,
                signals,
                trades_taken_today,
                realized_R_today,
                consecutive_losses,
                options_df,
            )
            return True

        if tp2_hit:
            self._close_position(
                position,
                ts,
                position.target2,
                "TP2",
                trades,
                signals,
                trades_taken_today,
                realized_R_today,
                consecutive_losses,
                options_df,
            )
            return True

        if tp1_hit:
            tp1_qty = position.qty * self.config.partial_take_pct
            position.tp1_done = True
            position.tp1_time = ts
            position.tp1_price = position.target1
            position.tp1_qty = tp1_qty
            position.remaining_qty = max(position.remaining_qty - tp1_qty, 0.0)
            position.stop_price = position.entry_price
            position.stop_after_tp1 = position.stop_price
            position.trail_active = True
            self._add_partial_exit_pnl(position, position.target1, tp1_qty, options_df, ts)
            signals.append(
                Signal(
                    timestamp=ts,
                    signal_type="EXIT_PARTIAL",
                    price=position.target1,
                    reason="TP1",
                )
            )

        if position.tp1_done and position.remaining_qty > 0:
            if prev_high is None:
                prev_high = high
            trail_ref = max(prev_high, float(row["ema9"]))
            position.stop_price = min(position.stop_price, trail_ref)

        return False

    def _close_position(
        self,
        position: PositionState,
        ts: pd.Timestamp,
        exit_price: float,
        reason: str,
        trades: List[Dict[str, object]],
        signals: List[Signal],
        trades_taken_today: int,
        realized_R_today: float,
        consecutive_losses: int,
        options_df: Optional[pd.DataFrame],
    ) -> None:
        if position.remaining_qty > 0:
            self._add_partial_exit_pnl(
                position, exit_price, position.remaining_qty, options_df, ts
            )
            position.remaining_qty = 0.0
        position.exit_time = ts
        position.exit_price = exit_price
        position.exit_reason = reason

        trade_record = {
            "entry_time": position.entry_time,
            "entry_price": position.entry_price,
            "stop_price": position.initial_stop,
            "R": position.R,
            "target1": position.target1,
            "target2": position.target2,
            "tp1_time": position.tp1_time,
            "tp1_price": position.tp1_price,
            "tp1_qty": position.tp1_qty,
            "stop_after_tp1": position.stop_after_tp1,
            "exit_time": position.exit_time,
            "exit_price": position.exit_price,
            "exit_reason": position.exit_reason,
            "realized_R": position.realized_R,
            "realized_rupees": position.realized_rupees,
            "trades_taken_today": trades_taken_today,
            "realized_R_today": realized_R_today,
            "consecutive_losses": consecutive_losses,
        }
        trades.append(trade_record)
        signals.append(
            Signal(
                timestamp=ts,
                signal_type="EXIT",
                price=exit_price,
                reason=reason,
            )
        )

    def _force_exit(
        self,
        position: PositionState,
        ts: pd.Timestamp,
        exit_price: float,
        reason: str,
        trades: List[Dict[str, object]],
        signals: List[Signal],
        trades_taken_today: int,
        realized_R_today: float,
        consecutive_losses: int,
        options_df: Optional[pd.DataFrame],
    ) -> None:
        self._close_position(
            position,
            ts,
            exit_price,
            reason,
            trades,
            signals,
            trades_taken_today,
            realized_R_today,
            consecutive_losses,
            options_df,
        )

    def _add_partial_exit_pnl(
        self,
        position: PositionState,
        exit_price: float,
        qty: float,
        options_df: Optional[pd.DataFrame],
        ts: pd.Timestamp,
    ) -> None:
        if qty <= 0:
            return
        fraction = qty / position.qty if position.qty > 0 else 0.0
        exit_R = (position.entry_price - exit_price) / position.R
        position.realized_R += exit_R * fraction

        if position.option_lots and position.option_entry_premium is not None:
            option_slice = self._select_option_slice(options_df, ts, position.entry_price)
            exit_premium = self._get_option_price(
                option_slice, ts, self.config.option_exit_price_field
            )
            if exit_premium is None:
                if position.option_risk_per_lot is None:
                    return
                rupees = exit_R * position.option_risk_per_lot * position.option_lots
                rupees *= fraction
            else:
                rupees = (exit_premium - position.option_entry_premium) * self.config.lot_size
                rupees *= position.option_lots * fraction
            if position.realized_rupees is None:
                position.realized_rupees = 0.0
            position.realized_rupees += rupees

    # ---------- Options helpers ----------

    def _prepare_options_df(self, options_df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if options_df is None:
            return None
        options_df = options_df.copy()
        if "timestamp" not in options_df:
            raise ValueError("options_df must include a timestamp column")
        options_df["timestamp"] = self._ensure_timezone(options_df["timestamp"])
        options_df = options_df.sort_values("timestamp")
        return options_df

    def _select_option_slice(
        self, options_df: Optional[pd.DataFrame], entry_ts: pd.Timestamp, underlying_price: float
    ) -> Optional[pd.DataFrame]:
        if options_df is None:
            return None
        df = options_df
        if "option_type" in df.columns:
            df = df[df["option_type"].str.upper().isin(["PE", "PUT"])]
        if "right" in df.columns:
            df = df[df["right"].str.upper().isin(["PE", "PUT"])]
        if "expiry" in df.columns:
            expiry = self._select_expiry(df["expiry"], entry_ts)
            df = df[df["expiry"] == expiry]
        if "strike" in df.columns:
            atm = round(underlying_price / self.config.strike_step) * self.config.strike_step
            if self.config.option_moneyness.upper() == "ITM1":
                strike = atm + self.config.strike_step
            else:
                strike = atm
            df = df[df["strike"] == strike]
        return df

    def _select_expiry(self, expiry_series: Iterable, entry_ts: pd.Timestamp):
        expiry = pd.to_datetime(expiry_series, errors="coerce").dt.tz_localize(None)
        entry_date = entry_ts.tz_convert(self.config.tz).date()
        valid = expiry.dropna().dt.date
        if self.config.expiry_mode.upper() == "0DTE":
            candidates = expiry[valid == entry_date]
        elif self.config.expiry_mode.upper() == "1DTE":
            target = (entry_ts + pd.Timedelta(days=1)).date()
            candidates = expiry[valid == target]
        else:
            candidates = expiry[valid >= entry_date]
        if candidates.empty:
            return expiry.iloc[0]
        return candidates.min()

    def _get_option_price(
        self, option_slice: Optional[pd.DataFrame], ts: pd.Timestamp, field: str
    ) -> Optional[float]:
        if option_slice is None or option_slice.empty:
            return None
        if field not in option_slice.columns:
            return None
        opt = option_slice
        times = opt["timestamp"]
        idx = times.searchsorted(ts)
        candidates = []
        if idx < len(opt):
            candidates.append(opt.iloc[idx])
        if idx > 0:
            candidates.append(opt.iloc[idx - 1])
        best = None
        best_delta = None
        for row in candidates:
            delta = abs((row["timestamp"] - ts).total_seconds())
            if best is None or delta < best_delta:
                best = row
                best_delta = delta
        if best is None or best_delta is None:
            return None
        if best_delta > self.config.option_time_tolerance_seconds:
            return None
        value = best[field]
        if pd.isna(value):
            return None
        return float(value)

    def _option_liquidity_ok(self, option_slice: Optional[pd.DataFrame], ts: pd.Timestamp) -> bool:
        if option_slice is None or option_slice.empty:
            return True
        row = self._get_option_row(option_slice, ts)
        if row is None:
            return True
        if "bid" in row and "ask" in row:
            bid = float(row["bid"])
            ask = float(row["ask"])
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / ((ask + bid) / 2.0)
                if spread_pct > self.config.max_spread_pct:
                    return False
        if "volume" in row:
            if float(row["volume"]) < self.config.min_option_volume:
                return False
        if "open_interest" in row:
            if float(row["open_interest"]) < self.config.min_open_interest:
                return False
        return True

    def _get_option_row(self, option_slice: pd.DataFrame, ts: pd.Timestamp) -> Optional[Dict[str, object]]:
        if option_slice.empty:
            return None
        idx = option_slice["timestamp"].searchsorted(ts)
        if idx < len(option_slice):
            row = option_slice.iloc[idx]
            return row.to_dict()
        return option_slice.iloc[-1].to_dict()

    def _find_first_stop_hit_ts(
        self, df: pd.DataFrame, start_index: int, stop_price: float
    ) -> Optional[pd.Timestamp]:
        for j in range(start_index, len(df)):
            if float(df.loc[j, "high"]) >= stop_price:
                return df.loc[j, "timestamp"]
        return None

    # ---------- Performance ----------

    def _compute_performance(self, trades_df: pd.DataFrame) -> Dict[str, float]:
        if trades_df.empty:
            return {
                "total_trades": 0.0,
                "win_rate": 0.0,
                "avg_R": 0.0,
                "expectancy": 0.0,
                "max_drawdown_R": 0.0,
                "profit_factor": 0.0,
                "avg_hold_minutes": 0.0,
            }
        realized_R = trades_df["realized_R"].astype(float)
        total_trades = float(len(realized_R))
        wins = (realized_R > 0).sum()
        win_rate = float(wins / total_trades)
        avg_R = float(realized_R.mean())
        expectancy = avg_R

        equity = realized_R.cumsum()
        running_max = equity.cummax()
        drawdown = equity - running_max
        max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0

        gross_profit = realized_R[realized_R > 0].sum()
        gross_loss = realized_R[realized_R < 0].sum()
        profit_factor = float(gross_profit / abs(gross_loss)) if gross_loss != 0 else float("inf")

        hold_minutes = []
        for _, row in trades_df.iterrows():
            if pd.notna(row["entry_time"]) and pd.notna(row["exit_time"]):
                delta = row["exit_time"] - row["entry_time"]
                hold_minutes.append(delta.total_seconds() / 60.0)
        avg_hold_minutes = float(np.mean(hold_minutes)) if hold_minutes else 0.0

        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_R": avg_R,
            "expectancy": expectancy,
            "max_drawdown_R": max_drawdown,
            "profit_factor": profit_factor,
            "avg_hold_minutes": avg_hold_minutes,
        }

    # ---------- Time + data utilities ----------

    def _parse_time(self, value: str) -> time:
        return datetime.strptime(value, "%H:%M").time()

    def _can_generate_signal(self, ts: pd.Timestamp) -> bool:
        if not self._in_session(ts):
            return False
        if ts.time() < self._no_trade_until:
            return False
        if ts.time() > self._last_entry_time:
            return False
        return True

    def _can_enter(
        self,
        ts: pd.Timestamp,
        stop_trading_today: bool,
        trades_taken_today: int,
        consecutive_losses: int,
        realized_R_today: float,
    ) -> bool:
        if not self._in_session(ts):
            return False
        if ts.time() < self._no_trade_until:
            return False
        if ts.time() > self._last_entry_time:
            return False
        if stop_trading_today:
            return False
        if trades_taken_today >= self.config.max_trades_per_day:
            return False
        if consecutive_losses >= self.config.max_consecutive_losses:
            return False
        if realized_R_today <= -abs(self.config.daily_loss_limit_R):
            return False
        return True

    def _in_session(self, ts: pd.Timestamp) -> bool:
        t = ts.time()
        return self._session_start <= t <= self._session_end

    def _ensure_timezone(self, ts_series: pd.Series) -> pd.Series:
        ts = pd.to_datetime(ts_series, errors="coerce")
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(self.config.tz)
        else:
            ts = ts.dt.tz_convert(self.config.tz)
        return ts

    def _infer_minutes(self, ts_series: pd.Series) -> float:
        ts = pd.to_datetime(ts_series)
        diffs = ts.sort_values().diff().dropna()
        if diffs.empty:
            return 5.0
        return diffs.median().total_seconds() / 60.0

    def _resample_ohlcv(self, df: pd.DataFrame, rule: str) -> pd.DataFrame:
        df = df.copy()
        df = df.set_index("timestamp")
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        resampled = df.resample(rule, label="right", closed="right").agg(agg).dropna()
        resampled = resampled.reset_index()
        return resampled

    def _validate_ohlcv(self, df: pd.DataFrame) -> None:
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")
