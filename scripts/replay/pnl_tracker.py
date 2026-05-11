"""
P&L tracker that reconstructs trades from mock execution fills.

Pairs ENTRY fills with EXIT fills chronologically, calculates per-trade
and aggregate P&L, and produces trade logs with summary metrics.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from scripts.replay.mock_execution import ReplayFill

logger = logging.getLogger(__name__)


@dataclass
class ReplayTrade:
    """A paired entry+exit reconstructed from fills."""

    trade_id: int
    strategy_id: str
    underlying: str
    entry_time: datetime
    entry_price: float
    entry_side: str         # BUY or SELL
    entry_tag: str
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_tag: str = ""
    exit_reason: str = ""
    quantity: int = 1
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    duration_seconds: float = 0.0
    r_multiple: Optional[float] = None
    entry_atr: Optional[float] = None
    strategy_context: Dict[str, Any] = field(default_factory=dict)
    execution_mode: str = ""
    entry_time_bucket: str = ""
    entry_time_of_day: str = ""
    entry_regime: str = ""


@dataclass
class DailySummary:
    """Aggregated metrics for a single trading date."""

    trading_date: date
    strategy_id: str
    underlying: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0


@dataclass
class StrategyMetrics:
    """Aggregate metrics across all dates for a strategy+underlying pair."""

    strategy_id: str
    underlying: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_consecutive_losses: int = 0
    avg_trade_duration_seconds: float = 0.0
    avg_r_multiple: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    expectancy: float = 0.0
    first_date: Optional[date] = None
    last_date: Optional[date] = None
    trading_days: int = 0


# Fee model: approximate brokerage + exchange charges for Indian markets.
# Angel Broking flat fee model.
BROKERAGE_PER_ORDER = 20.0          # INR per executed order
STT_PCT = 0.000625                  # Securities Transaction Tax (sell side, options)
EXCHANGE_TXN_PCT = 0.0000053        # Exchange transaction charges
GST_PCT = 0.18                      # GST on brokerage
STAMP_DUTY_PCT = 0.00003            # Stamp duty (buy side)


def estimate_fees(
    entry_price: float,
    exit_price: float,
    quantity: int,
    is_option: bool = True,
) -> float:
    """Estimate round-trip fees for a single trade."""
    entry_value = entry_price * quantity
    exit_value = exit_price * quantity
    turnover = entry_value + exit_value

    brokerage = BROKERAGE_PER_ORDER * 2  # entry + exit
    stt = exit_value * STT_PCT if is_option else turnover * STT_PCT
    exchange_txn = turnover * EXCHANGE_TXN_PCT
    gst = brokerage * GST_PCT
    stamp = entry_value * STAMP_DUTY_PCT

    return brokerage + stt + exchange_txn + gst + stamp


class PnLTracker:
    """Reconstructs trades from fills and computes P&L metrics."""

    def __init__(self, fee_model: bool = True) -> None:
        self.fills: List[ReplayFill] = []
        self.trades: List[ReplayTrade] = []
        self._open_positions: Dict[str, deque[ReplayFill]] = {}
        self._trade_counter = 0
        self._fee_model = fee_model

    def process_fills(self, fills: List[ReplayFill]) -> List[ReplayTrade]:
        """Convert a chronological list of fills into paired trades."""
        self.fills = list(fills)
        self.trades = []
        self._open_positions = {}
        self._trade_counter = 0

        for fill in fills:
            pos_key = self._position_key(fill)

            if fill.purpose == "ENTRY":
                self._open_positions.setdefault(pos_key, deque()).append(fill)
            elif fill.purpose == "EXIT":
                entry_fill = self._pop_open_position(pos_key, fill)
                if entry_fill is None:
                    logger.warning(
                        "EXIT fill without matching ENTRY: %s at %s",
                        pos_key,
                        fill.timestamp,
                    )
                    continue
                trade = self._pair_fills(entry_fill, fill)
                self.trades.append(trade)
            else:
                logger.warning("Unknown fill purpose: %s", fill.purpose)

        # Any open positions at end are unterminated trades
        for pos_key, entries in self._open_positions.items():
            for entry_fill in entries:
                logger.info(
                    "Unterminated position: %s entered at %s (no exit fill)",
                    pos_key,
                    entry_fill.timestamp,
                )

        return self.trades

    def _pair_fills(self, entry: ReplayFill, exit_fill: ReplayFill) -> ReplayTrade:
        """Create a ReplayTrade from an entry+exit fill pair."""
        self._trade_counter += 1

        # Determine P&L direction based on entry side
        if entry.side == "SELL":
            # Short: profit when price falls
            gross_pnl = (entry.fill_price - exit_fill.fill_price) * entry.quantity
        else:
            # Long: profit when price rises
            gross_pnl = (exit_fill.fill_price - entry.fill_price) * entry.quantity

        fees = 0.0
        if self._fee_model:
            fees = estimate_fees(
                entry.fill_price,
                exit_fill.fill_price,
                entry.quantity,
                is_option=True,
            )

        net_pnl = gross_pnl - fees

        duration = (exit_fill.timestamp - entry.timestamp).total_seconds()

        # Extract ATR from strategy context for R-multiple calculation
        entry_atr = entry.strategy_context.get("entry_atr") or entry.strategy_context.get("atr")
        r_multiple = None
        if entry_atr and entry_atr > 0:
            r_multiple = gross_pnl / (entry_atr * entry.quantity)

        # Parse exit reason from tag
        exit_reason = exit_fill.tag
        for prefix in ("EMA20_EXIT_", "PUT_MOM_EXIT_", "XNCB_EXIT_"):
            if exit_fill.tag.startswith(prefix):
                exit_reason = exit_fill.tag[len(prefix):]
                break

        replay_ctx = {}
        if isinstance(entry.strategy_context, dict):
            replay_ctx = dict(entry.strategy_context.get("_replay") or {})
        persisted_regime = str(replay_ctx.get("regime") or "").strip()
        adx_regime = "unknown"
        adx_val = replay_ctx.get("adx")
        try:
            adx_float = float(adx_val)
        except (TypeError, ValueError):
            adx_float = None
        if adx_float is not None:
            if adx_float < 18:
                adx_regime = "weak_trend"
            elif adx_float < 25:
                adx_regime = "moderate_trend"
            else:
                adx_regime = "strong_trend"
        atr_regime = "unknown"
        try:
            atr_float = float(replay_ctx.get("atr"))
            close_float = float(replay_ctx.get("close"))
            ratio = abs(atr_float) / max(abs(close_float), 0.000001)
            if ratio < 0.001:
                atr_regime = "low_vol"
            elif ratio < 0.0025:
                atr_regime = "normal_vol"
            else:
                atr_regime = "high_vol"
        except (TypeError, ValueError):
            atr_regime = "unknown"

        return ReplayTrade(
            trade_id=self._trade_counter,
            strategy_id=entry.strategy_id,
            underlying=entry.underlying,
            entry_time=entry.timestamp,
            entry_price=entry.fill_price,
            entry_side=entry.side,
            entry_tag=entry.tag,
            exit_time=exit_fill.timestamp,
            exit_price=exit_fill.fill_price,
            exit_tag=exit_fill.tag,
            exit_reason=exit_reason,
            quantity=entry.quantity,
            gross_pnl=gross_pnl,
            fees=fees,
            net_pnl=net_pnl,
            duration_seconds=duration,
            r_multiple=r_multiple,
            entry_atr=entry_atr,
            strategy_context=entry.strategy_context,
            execution_mode=str(replay_ctx.get("fill_mode") or entry.execution_mode or ""),
            entry_time_bucket=str(replay_ctx.get("time_bucket") or ""),
            entry_time_of_day=str(replay_ctx.get("time_of_day") or ""),
            entry_regime=persisted_regime or f"{adx_regime}|{atr_regime}",
        )

    @staticmethod
    def _position_key(fill: ReplayFill) -> str:
        strategy_id = str(fill.strategy_id)
        underlying = str(fill.underlying)
        replay_ctx = {}
        if isinstance(fill.strategy_context, dict):
            replay_ctx = dict(fill.strategy_context.get("_replay") or {})
        label = (
            str(replay_ctx.get("position_label") or "").strip()
            or str(getattr(fill, "symbol", "") or "").strip()
            or str(getattr(fill, "tag", "") or "").strip()
        )
        return f"{strategy_id}:{underlying}:{label}"

    def _pop_open_position(self, pos_key: str, fill: ReplayFill) -> Optional[ReplayFill]:
        queue = self._open_positions.get(pos_key)
        if queue:
            entry = queue.popleft()
            if not queue:
                self._open_positions.pop(pos_key, None)
            return entry

        fallback_key = self._fallback_exit_key(fill)
        if fallback_key is None:
            return None
        queue = self._open_positions.get(fallback_key)
        if not queue:
            return None
        entry = queue.popleft()
        if not queue:
            self._open_positions.pop(fallback_key, None)
        return entry

    def _fallback_exit_key(self, fill: ReplayFill) -> Optional[str]:
        strategy_id = str(fill.strategy_id)
        if strategy_id != "ema20_strategy":
            return None
        prefix = f"{strategy_id}:{fill.underlying}:"
        candidates = [
            key
            for key, entries in self._open_positions.items()
            if key.startswith(prefix) and entries
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def compute_metrics(
        self,
        strategy_id: str,
        underlying: str,
        *,
        replay_first_date: Optional[date] = None,
        replay_last_date: Optional[date] = None,
        replay_trading_days: Optional[int] = None,
    ) -> StrategyMetrics:
        """Compute aggregate metrics for a strategy+underlying pair."""
        relevant = [
            t
            for t in self.trades
            if t.strategy_id == strategy_id and t.underlying == underlying
        ]

        metrics = StrategyMetrics(strategy_id=strategy_id, underlying=underlying)
        if not relevant:
            if replay_first_date is not None:
                metrics.first_date = replay_first_date
            if replay_last_date is not None:
                metrics.last_date = replay_last_date
            if replay_trading_days is not None:
                metrics.trading_days = max(0, int(replay_trading_days))
            return metrics

        metrics.total_trades = len(relevant)
        metrics.winning_trades = sum(1 for t in relevant if t.net_pnl > 0)
        metrics.losing_trades = sum(1 for t in relevant if t.net_pnl <= 0)
        metrics.gross_pnl = sum(t.gross_pnl for t in relevant)
        metrics.net_pnl = sum(t.net_pnl for t in relevant)
        metrics.total_fees = sum(t.fees for t in relevant)
        metrics.win_rate = (
            metrics.winning_trades / metrics.total_trades
            if metrics.total_trades
            else 0.0
        )

        wins = [t.net_pnl for t in relevant if t.net_pnl > 0]
        losses = [t.net_pnl for t in relevant if t.net_pnl <= 0]
        metrics.avg_win = sum(wins) / len(wins) if wins else 0.0
        metrics.avg_loss = sum(losses) / len(losses) if losses else 0.0

        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        metrics.profit_factor = (
            total_wins / total_losses if total_losses > 0 else float("inf")
        )

        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in relevant:
            cumulative += t.net_pnl
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        metrics.max_drawdown = max_dd

        # Max consecutive losses
        max_consec = 0
        current_consec = 0
        for t in relevant:
            if t.net_pnl <= 0:
                current_consec += 1
                max_consec = max(max_consec, current_consec)
            else:
                current_consec = 0
        metrics.max_consecutive_losses = max_consec

        # Duration
        durations = [t.duration_seconds for t in relevant if t.duration_seconds > 0]
        metrics.avg_trade_duration_seconds = (
            sum(durations) / len(durations) if durations else 0.0
        )

        # R-multiple
        r_vals = [t.r_multiple for t in relevant if t.r_multiple is not None]
        metrics.avg_r_multiple = sum(r_vals) / len(r_vals) if r_vals else None

        # Sharpe ratio (daily returns)
        daily_pnls = self._daily_pnls(relevant)
        if len(daily_pnls) > 1:
            mean_d = sum(daily_pnls) / len(daily_pnls)
            var_d = sum((p - mean_d) ** 2 for p in daily_pnls) / (len(daily_pnls) - 1)
            std_d = math.sqrt(var_d) if var_d > 0 else 0.0
            metrics.sharpe_ratio = (
                (mean_d / std_d) * math.sqrt(252) if std_d > 0 else None
            )

        # Expectancy
        metrics.expectancy = (
            (metrics.win_rate * metrics.avg_win)
            + ((1 - metrics.win_rate) * metrics.avg_loss)
        )

        # Date range
        dates = sorted({t.entry_time.date() for t in relevant})
        metrics.first_date = dates[0] if dates else None
        metrics.last_date = dates[-1] if dates else None
        metrics.trading_days = len(dates)

        if replay_first_date is not None:
            metrics.first_date = replay_first_date
        if replay_last_date is not None:
            metrics.last_date = replay_last_date
        if replay_trading_days is not None:
            metrics.trading_days = max(0, int(replay_trading_days))

        return metrics

    def daily_summaries(
        self,
        strategy_id: str,
        underlying: str,
    ) -> List[DailySummary]:
        """Compute per-day summaries."""
        relevant = [
            t
            for t in self.trades
            if t.strategy_id == strategy_id and t.underlying == underlying
        ]
        by_date: Dict[date, List[ReplayTrade]] = {}
        for t in relevant:
            d = t.entry_time.date()
            by_date.setdefault(d, []).append(t)

        summaries = []
        for d in sorted(by_date):
            trades = by_date[d]
            s = DailySummary(
                trading_date=d,
                strategy_id=strategy_id,
                underlying=underlying,
                total_trades=len(trades),
                winning_trades=sum(1 for t in trades if t.net_pnl > 0),
                losing_trades=sum(1 for t in trades if t.net_pnl <= 0),
                gross_pnl=sum(t.gross_pnl for t in trades),
                total_fees=sum(t.fees for t in trades),
                net_pnl=sum(t.net_pnl for t in trades),
            )
            # Max intraday drawdown
            cum = 0.0
            peak = 0.0
            for t in trades:
                cum += t.net_pnl
                peak = max(peak, cum)
                s.max_drawdown = max(s.max_drawdown, peak - cum)
            s.peak_pnl = peak
            summaries.append(s)
        return summaries

    def trade_log_csv(self, strategy_id: str, underlying: str) -> str:
        """Export trade log as CSV string."""
        relevant = [
            t
            for t in self.trades
            if t.strategy_id == strategy_id and t.underlying == underlying
        ]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([
            "trade_id",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "side",
            "quantity",
            "gross_pnl",
            "fees",
            "net_pnl",
            "exit_reason",
            "duration_s",
            "r_multiple",
            "entry_atr",
            "execution_mode",
            "entry_time_bucket",
            "entry_regime",
        ])
        for t in relevant:
            writer.writerow([
                t.trade_id,
                t.entry_time.isoformat() if t.entry_time else "",
                t.exit_time.isoformat() if t.exit_time else "",
                f"{t.entry_price:.4f}",
                f"{t.exit_price:.4f}" if t.exit_price else "",
                t.entry_side,
                t.quantity,
                f"{t.gross_pnl:.2f}",
                f"{t.fees:.2f}",
                f"{t.net_pnl:.2f}",
                t.exit_reason,
                f"{t.duration_seconds:.0f}",
                f"{t.r_multiple:.3f}" if t.r_multiple is not None else "",
                f"{t.entry_atr:.4f}" if t.entry_atr is not None else "",
                t.execution_mode,
                t.entry_time_bucket,
                t.entry_regime,
            ])
        return buf.getvalue()

    def fill_log_csv(self, strategy_id: str, underlying: str) -> str:
        """Export raw replay fills as CSV for debugging blocked/unterminated trades."""

        relevant = [
            fill
            for fill in self.fills
            if fill.strategy_id == strategy_id and fill.underlying == underlying
        ]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([
            "timestamp",
            "strategy_id",
            "underlying",
            "side",
            "purpose",
            "tag",
            "quantity",
            "fill_price",
            "reference_price",
            "execution_mode",
            "phase",
            "fill_note",
            "strategy_context",
        ])
        for fill in relevant:
            writer.writerow([
                fill.timestamp.isoformat() if fill.timestamp else "",
                fill.strategy_id,
                fill.underlying,
                fill.side,
                fill.purpose,
                fill.tag,
                fill.quantity,
                f"{fill.fill_price:.4f}",
                f"{fill.reference_price:.4f}" if fill.reference_price is not None else "",
                fill.execution_mode,
                fill.phase,
                fill.fill_note,
                json.dumps(fill.strategy_context, sort_keys=True, default=str),
            ])
        return buf.getvalue()

    @staticmethod
    def _daily_pnls(trades: List[ReplayTrade]) -> List[float]:
        by_date: Dict[date, float] = {}
        for t in trades:
            d = t.entry_time.date()
            by_date[d] = by_date.get(d, 0.0) + t.net_pnl
        return [by_date[d] for d in sorted(by_date)]
