"""Intraday labels for short option candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from app.data.option_chain_provider import OptionQuote

IST = ZoneInfo("Asia/Kolkata")


class IntradayExitReason(str, Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    PREMIUM_STOP = "PREMIUM_STOP"
    SPOT_STOP = "SPOT_STOP"
    EOD = "EOD"
    NO_FOLLOWUP = "NO_FOLLOWUP"


@dataclass(frozen=True)
class IntradayLabelConfig:
    take_profit_decay_pct: float = 0.50
    premium_stop_multiple: float = 1.80
    force_exit_time: time = time(15, 20)
    max_entry_quote_age_seconds: int = 120
    lot_size: int = 65
    fees_per_lot: float = 0.0
    spot_stop_buffer_points: float | None = None


@dataclass(frozen=True)
class IntradayShortOptionLabel:
    decision_ts: datetime
    entry_ts: datetime
    exit_ts: datetime
    underlying: str
    expiry: date
    strike: int
    option_type: str
    entry_premium: float
    exit_premium: float
    entry_trigger_premium: float
    exit_trigger_premium: float
    exit_reason: IntradayExitReason
    pnl_per_lot: float
    mae_premium: float
    mae_multiple: float
    mfe_premium: float
    bars_seen: int
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def primary_label(self) -> int:
        return 1 if self.exit_reason == IntradayExitReason.TAKE_PROFIT else 0

    @property
    def profitable_label(self) -> int:
        return 1 if self.pnl_per_lot > 0 else 0

    @property
    def tail_label(self) -> float:
        return self.mae_premium

    def to_training_row(self) -> dict[str, Any]:
        return {
            "decision_ts": self.decision_ts.isoformat(),
            "entry_ts": self.entry_ts.isoformat(),
            "exit_ts": self.exit_ts.isoformat(),
            "underlying": self.underlying,
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": self.option_type,
            "entry_premium": self.entry_premium,
            "exit_premium": self.exit_premium,
            "exit_reason": self.exit_reason.value,
            "primary_label": self.primary_label,
            "profitable_label": self.profitable_label,
            "pnl_per_lot": self.pnl_per_lot,
            "mae_premium": self.mae_premium,
            "mae_multiple": self.mae_multiple,
            "mfe_premium": self.mfe_premium,
            "bars_seen": self.bars_seen,
            "quality_flags": list(self.quality_flags),
        }


def label_short_option_intraday(
    quotes: Sequence[OptionQuote],
    *,
    decision_ts: datetime,
    config: IntradayLabelConfig | None = None,
) -> IntradayShortOptionLabel:
    cfg = config or IntradayLabelConfig()
    rows = sorted((quote.normalized() for quote in quotes), key=lambda q: q.snapshot_ts)
    if not rows:
        raise ValueError("cannot label empty option-chain window")

    decision = _aware_utc(decision_ts)
    deadline = _intraday_deadline(decision, cfg.force_exit_time)
    _assert_single_intraday_window(rows, decision=decision, deadline=deadline)

    entry_quote = _entry_quote(rows, decision, cfg)
    entry_trigger = _trigger_premium(entry_quote)
    entry_premium = _entry_premium(entry_quote)
    if entry_trigger <= 0 or entry_premium <= 0:
        raise ValueError("entry quote has no usable premium")

    take_profit_trigger = entry_trigger * (1.0 - float(cfg.take_profit_decay_pct))
    premium_stop_trigger = entry_trigger * float(cfg.premium_stop_multiple)
    path = [row for row in rows if entry_quote.snapshot_ts < row.snapshot_ts <= deadline]

    exit_quote = entry_quote
    exit_reason = IntradayExitReason.NO_FOLLOWUP
    trigger_history = [entry_trigger]
    for row in path:
        trigger_premium = _trigger_premium(row)
        trigger_history.append(trigger_premium)
        if _spot_stop_hit(row, cfg):
            exit_quote = row
            exit_reason = IntradayExitReason.SPOT_STOP
            break
        if trigger_premium >= premium_stop_trigger:
            exit_quote = row
            exit_reason = IntradayExitReason.PREMIUM_STOP
            break
        if trigger_premium <= take_profit_trigger:
            exit_quote = row
            exit_reason = IntradayExitReason.TAKE_PROFIT
            break

    if exit_reason == IntradayExitReason.NO_FOLLOWUP and path:
        exit_quote = path[-1]
        exit_reason = IntradayExitReason.EOD

    exit_trigger = _trigger_premium(exit_quote)
    exit_premium = _exit_premium(exit_quote)
    max_premium = max(trigger_history) if trigger_history else entry_trigger
    min_premium = min(trigger_history) if trigger_history else entry_trigger
    mae = max(0.0, max_premium - entry_trigger)
    mfe = max(0.0, entry_trigger - min_premium)
    quality_flags = tuple(_quality_flags(entry_quote, path, cfg, decision))
    pnl = ((entry_premium - exit_premium) * int(cfg.lot_size)) - float(cfg.fees_per_lot)

    return IntradayShortOptionLabel(
        decision_ts=decision,
        entry_ts=entry_quote.snapshot_ts,
        exit_ts=exit_quote.snapshot_ts,
        underlying=entry_quote.underlying,
        expiry=entry_quote.expiry,
        strike=entry_quote.strike,
        option_type=entry_quote.option_type,
        entry_premium=entry_premium,
        exit_premium=exit_premium,
        entry_trigger_premium=entry_trigger,
        exit_trigger_premium=exit_trigger,
        exit_reason=exit_reason,
        pnl_per_lot=pnl,
        mae_premium=mae,
        mae_multiple=(max_premium / entry_trigger) if entry_trigger > 0 else 0.0,
        mfe_premium=mfe,
        bars_seen=len(path),
        quality_flags=quality_flags,
    )


def label_candidate_from_repository(
    repository: Any,
    *,
    underlying: str,
    expiry: date,
    strike: int,
    option_type: str,
    decision_ts: datetime,
    provider: str | None = None,
    config: IntradayLabelConfig | None = None,
) -> IntradayShortOptionLabel:
    cfg = config or IntradayLabelConfig()
    decision = _aware_utc(decision_ts)
    deadline = _intraday_deadline(decision, cfg.force_exit_time)
    start = decision - timedelta(seconds=max(0, int(cfg.max_entry_quote_age_seconds)))
    quotes = repository.fetch_candidate_window(
        underlying=underlying,
        expiry=expiry,
        strike=int(strike),
        option_type=str(option_type).strip().upper(),
        start_ts=start,
        end_ts=deadline,
        provider=provider,
    )
    return label_short_option_intraday(quotes, decision_ts=decision, config=cfg)


def _entry_quote(
    rows: Sequence[OptionQuote],
    decision: datetime,
    cfg: IntradayLabelConfig,
) -> OptionQuote:
    candidates = [row for row in rows if row.snapshot_ts <= decision]
    if not candidates:
        raise ValueError("missing entry quote at or before decision time")
    entry = candidates[-1]
    age = (decision - entry.snapshot_ts).total_seconds()
    if age > int(cfg.max_entry_quote_age_seconds):
        raise ValueError("entry quote is stale for decision time")
    return entry


def _quality_flags(
    entry_quote: OptionQuote,
    path: Sequence[OptionQuote],
    cfg: IntradayLabelConfig,
    decision: datetime,
) -> list[str]:
    flags: list[str] = []
    if not path:
        flags.append("missing_followup_quotes")
    age = (decision - entry_quote.snapshot_ts).total_seconds()
    if age > 0:
        flags.append("entry_quote_before_decision")
    if cfg.fees_per_lot <= 0:
        flags.append("fees_not_applied")
    return flags


def _assert_single_intraday_window(
    rows: Sequence[OptionQuote],
    *,
    decision: datetime,
    deadline: datetime,
) -> None:
    decision_day = decision.astimezone(IST).date()
    for row in rows:
        row_day = row.snapshot_ts.astimezone(IST).date()
        if row_day != decision_day:
            raise ValueError("label input spans multiple trading sessions")
        if row.snapshot_ts > deadline:
            raise ValueError("label input extends beyond intraday deadline")


def _spot_stop_hit(row: OptionQuote, cfg: IntradayLabelConfig) -> bool:
    if cfg.spot_stop_buffer_points is None:
        return False
    spot = _float(row.underlying_ltp)
    if spot is None:
        return False
    return spot >= (float(row.strike) - float(cfg.spot_stop_buffer_points))


def _entry_premium(row: OptionQuote) -> float:
    return _first_positive(row.bid, row.ltp, _mid(row)) or 0.0


def _exit_premium(row: OptionQuote) -> float:
    return _first_positive(row.ask, row.ltp, _mid(row)) or 0.0


def _trigger_premium(row: OptionQuote) -> float:
    return _first_positive(row.ltp, _mid(row), row.ask, row.bid) or 0.0


def _mid(row: OptionQuote) -> float | None:
    bid = _float(row.bid)
    ask = _float(row.ask)
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (bid + ask) / 2.0


def _first_positive(*values: object) -> float | None:
    for value in values:
        parsed = _float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _intraday_deadline(decision: datetime, exit_time: time) -> datetime:
    decision_ist = decision.astimezone(IST)
    deadline_ist = datetime.combine(decision_ist.date(), exit_time, tzinfo=IST)
    return deadline_ist.astimezone(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "IST",
    "IntradayExitReason",
    "IntradayLabelConfig",
    "IntradayShortOptionLabel",
    "label_candidate_from_repository",
    "label_short_option_intraday",
]
