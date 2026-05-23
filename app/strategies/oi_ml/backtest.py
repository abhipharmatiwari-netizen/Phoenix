"""Spread-aware intraday backtest labels for OI/ML CE seller v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote


IST = ZoneInfo("Asia/Kolkata")


class BearCallSpreadExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    LOSS_STOP = "LOSS_STOP"
    SPOT_STOP = "SPOT_STOP"
    OI_INVALIDATION = "OI_INVALIDATION"
    VOL_STOP = "VOL_STOP"
    TIME_STOP = "TIME_STOP"
    EOD = "EOD"
    NO_FOLLOWUP = "NO_FOLLOWUP"


@dataclass(frozen=True)
class BearCallSpreadLabelConfig:
    take_profit_pct_of_credit: float = 0.60
    stop_loss_mult_credit: float = 1.80
    spot_stop_buffer_points: float = 0.0
    oi_invalidation_drop_pct: float = 0.35
    max_vix: float = 22.0
    time_stop: time = time(14, 55)
    eod_cap: time = time(15, 20)
    lot_size: int = 65
    fees_per_lot: float = 0.0


@dataclass(frozen=True)
class BearCallSpreadLabel:
    decision_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    underlying: str
    expiry: date
    short_strike: int
    long_strike: int
    entry_credit: float
    exit_debit: float
    exit_reason: BearCallSpreadExitReason
    pnl_per_lot: float
    mae_debit: float
    mfe_debit: float
    bars_seen: int
    max_source_ts: datetime | None = None
    max_ingested_at: datetime | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_label(self) -> int:
        return 1 if self.exit_reason == BearCallSpreadExitReason.TAKE_PROFIT else 0

    @property
    def profitable_label(self) -> int:
        return 1 if self.pnl_per_lot > 0 else 0

    def to_training_row(self) -> dict[str, Any]:
        return {
            "decision_ts": self.decision_ts.isoformat(),
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "short_strike": self.short_strike,
            "long_strike": self.long_strike,
            "entry_credit": self.entry_credit,
            "exit_debit": self.exit_debit,
            "exit_reason": self.exit_reason.value,
            "primary_label": self.primary_label,
            "profitable_label": self.profitable_label,
            "pnl_per_lot": self.pnl_per_lot,
            "mae_debit": self.mae_debit,
            "mfe_debit": self.mfe_debit,
            "bars_seen": self.bars_seen,
            "max_source_ts": self.max_source_ts.isoformat() if self.max_source_ts else None,
            "max_ingested_at": self.max_ingested_at.isoformat() if self.max_ingested_at else None,
            "quality_flags": list(self.quality_flags),
        }


def label_bear_call_spread_intraday(
    quotes: Sequence[OptionQuote],
    *,
    decision_ts: datetime,
    short_strike: int,
    long_strike: int,
    config: BearCallSpreadLabelConfig | None = None,
) -> BearCallSpreadLabel:
    cfg = config or BearCallSpreadLabelConfig()
    decision = _aware_utc(decision_ts)
    deadline = _intraday_time(decision, cfg.eod_cap)
    time_stop = _intraday_time(decision, cfg.time_stop)
    rows = sorted((quote.normalized() for quote in quotes), key=lambda quote: quote.snapshot_ts)
    if not rows:
        raise ValueError("cannot label empty spread window")
    _assert_no_lookahead_window(rows, decision=decision, deadline=deadline)

    entry_short = _entry_quote(rows, decision, short_strike)
    entry_long = _entry_quote(rows, decision, long_strike)
    entry_credit = _sell_price(entry_short) - _buy_price(entry_long)
    if entry_credit <= 0:
        raise ValueError("bear-call spread entry credit must be positive")

    exit_short = entry_short
    exit_long = entry_long
    exit_reason = BearCallSpreadExitReason.NO_FOLLOWUP
    debit_history = [entry_credit]
    path_bars = _paired_path(rows, after=entry_short.snapshot_ts, deadline=deadline, short_strike=short_strike, long_strike=long_strike)

    for short_quote, long_quote in path_bars:
        debit = _buy_price(short_quote) - _sell_price(long_quote)
        debit_history.append(debit)
        now = short_quote.snapshot_ts
        if _spot_stop_hit(short_quote, cfg):
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.SPOT_STOP
            break
        if _oi_invalidated(entry_short, short_quote, cfg):
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.OI_INVALIDATION
            break
        if _vol_stop_hit(short_quote, cfg):
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.VOL_STOP
            break
        if debit <= entry_credit * (1.0 - float(cfg.take_profit_pct_of_credit)):
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.TAKE_PROFIT
            break
        if debit >= entry_credit * float(cfg.stop_loss_mult_credit):
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.LOSS_STOP
            break
        if now >= time_stop:
            exit_short, exit_long, exit_reason = short_quote, long_quote, BearCallSpreadExitReason.TIME_STOP
            break

    if exit_reason is BearCallSpreadExitReason.NO_FOLLOWUP and path_bars:
        exit_short, exit_long = path_bars[-1]
        exit_reason = (
            BearCallSpreadExitReason.EOD
            if exit_short.snapshot_ts >= deadline
            else BearCallSpreadExitReason.NO_FOLLOWUP
        )

    exit_debit = _buy_price(exit_short) - _sell_price(exit_long)
    max_debit = max(debit_history)
    min_debit = min(debit_history)
    pnl = ((entry_credit - exit_debit) * int(cfg.lot_size)) - float(cfg.fees_per_lot)
    return BearCallSpreadLabel(
        decision_ts=decision,
        entry_ts=entry_short.snapshot_ts,
        exit_ts=exit_short.snapshot_ts,
        underlying=entry_short.underlying,
        expiry=entry_short.expiry,
        short_strike=int(short_strike),
        long_strike=int(long_strike),
        entry_credit=entry_credit,
        exit_debit=exit_debit,
        exit_reason=exit_reason,
        pnl_per_lot=pnl,
        mae_debit=max(0.0, max_debit - entry_credit),
        mfe_debit=max(0.0, entry_credit - min_debit),
        bars_seen=len(path_bars),
        max_source_ts=_max_dt([row.source_ts for row in rows]),
        max_ingested_at=_max_dt([row.ingested_at for row in rows]),
        quality_flags=tuple(["missing_followup_quotes"] if not path_bars else []),
    )


def _entry_quote(rows: Sequence[OptionQuote], decision: datetime, strike: int) -> OptionQuote:
    candidates = [
        row
        for row in rows
        if row.option_type == "CE"
        and int(row.strike) == int(strike)
        and row.snapshot_ts <= decision
    ]
    if not candidates:
        raise ValueError("missing spread entry quote at or before decision")
    return candidates[-1]


def _paired_path(
    rows: Sequence[OptionQuote],
    *,
    after: datetime,
    deadline: datetime,
    short_strike: int,
    long_strike: int,
) -> list[tuple[OptionQuote, OptionQuote]]:
    by_ts: dict[datetime, dict[int, OptionQuote]] = {}
    for row in rows:
        if row.option_type != "CE" or row.snapshot_ts <= after or row.snapshot_ts > deadline:
            continue
        if row.strike not in {int(short_strike), int(long_strike)}:
            continue
        by_ts.setdefault(row.snapshot_ts, {})[int(row.strike)] = row
    out: list[tuple[OptionQuote, OptionQuote]] = []
    for ts in sorted(by_ts):
        pair = by_ts[ts]
        if int(short_strike) in pair and int(long_strike) in pair:
            out.append((pair[int(short_strike)], pair[int(long_strike)]))
    return out


def _assert_no_lookahead_window(
    rows: Sequence[OptionQuote],
    *,
    decision: datetime,
    deadline: datetime,
) -> None:
    decision_day = decision.astimezone(IST).date()
    for row in rows:
        if row.snapshot_ts.astimezone(IST).date() != decision_day:
            raise ValueError("spread label input spans multiple sessions")
        if row.snapshot_ts > deadline:
            raise ValueError("spread label input extends beyond EOD cap")


def _sell_price(row: OptionQuote) -> float:
    return _first_positive(row.bid, row.ltp) or 0.0


def _buy_price(row: OptionQuote) -> float:
    return _first_positive(row.ask, row.ltp) or 0.0


def _first_positive(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _spot_stop_hit(row: OptionQuote, cfg: BearCallSpreadLabelConfig) -> bool:
    if row.underlying_ltp in (None, ""):
        return False
    return float(row.underlying_ltp) >= (float(row.strike) - float(cfg.spot_stop_buffer_points))


def _oi_invalidated(
    entry_short: OptionQuote,
    current_short: OptionQuote,
    cfg: BearCallSpreadLabelConfig,
) -> bool:
    entry_oi = int(entry_short.oi or 0)
    current_oi = int(current_short.oi or 0)
    if entry_oi <= 0:
        return False
    drop_pct = (entry_oi - current_oi) / entry_oi
    return drop_pct >= float(cfg.oi_invalidation_drop_pct)


def _vol_stop_hit(row: OptionQuote, cfg: BearCallSpreadLabelConfig) -> bool:
    if row.vix in (None, ""):
        return False
    return float(row.vix) > float(cfg.max_vix)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _intraday_time(decision: datetime, tod: time) -> datetime:
    return datetime.combine(decision.astimezone(IST).date(), tod, tzinfo=IST).astimezone(timezone.utc)


def _max_dt(values: Sequence[datetime | None]) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


__all__ = [
    "BearCallSpreadExitReason",
    "BearCallSpreadLabel",
    "BearCallSpreadLabelConfig",
    "label_bear_call_spread_intraday",
]
