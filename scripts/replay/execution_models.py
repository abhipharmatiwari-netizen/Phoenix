"""Deterministic replay execution and synthetic tick helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

FILL_MODE_BAR_CLOSE = "bar_close_fill"
FILL_MODE_NEXT_BAR_OPEN = "next_bar_open_fill"
TICK_MODEL_OPEN_CLOSE = "open_close"
TICK_MODEL_OHLC = "ohlc"

# Issue #216: end-of-replay-window / end-of-session position handling.
#   force_exit  - legacy: replay closes any open position at every session
#                 boundary AND at window end. Inflates losses on strategies
#                 with multi-bar holds because positions that would have
#                 hit TP overnight are booked as session-close losses.
#   carry_over  - DEFAULT. Replay does NOT close at session boundaries;
#                 positions carry across days and the strategy's own logic
#                 (TP / SL / EOD square-off / explicit-exit) drives exits.
#                 At window end, any still-open position is recorded as an
#                 unrealised last-bar mark, NOT folded into realized net_pnl
#                 or win/loss stats.
#   daily_mtm   - same carry-over behaviour as carry_over, additionally
#                 records a daily mark-to-market snapshot per session close.
#                 Window-end marks remain unrealised for realized metrics.
END_POLICY_FORCE_EXIT = "force_exit"
END_POLICY_CARRY_OVER = "carry_over"
END_POLICY_DAILY_MTM = "daily_mtm"
_END_POLICY_VALUES = {
    END_POLICY_FORCE_EXIT,
    END_POLICY_CARRY_OVER,
    END_POLICY_DAILY_MTM,
}


@dataclass(frozen=True)
class ExecutionConfig:
    """Replay-local execution assumptions."""

    fill_mode: str = FILL_MODE_BAR_CLOSE
    tick_model: str = TICK_MODEL_OHLC
    slippage_bps: float = 0.0
    fixed_slippage: float = 0.0
    spread_bps: float = 0.0
    latency_bars: int = 0
    reject_entries_without_future_bar: bool = True
    end_policy: str = END_POLICY_CARRY_OVER

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fill_mode": str(self.fill_mode),
            "tick_model": str(self.tick_model),
            "slippage_bps": float(self.slippage_bps),
            "fixed_slippage": float(self.fixed_slippage),
            "spread_bps": float(self.spread_bps),
            "latency_bars": int(self.latency_bars),
            "reject_entries_without_future_bar": bool(self.reject_entries_without_future_bar),
            "end_policy": str(self.end_policy),
        }


@dataclass(frozen=True)
class TickEvent:
    """Synthetic tick emitted during replay."""

    timestamp: datetime
    price: float
    phase: str


def normalize_execution_config(
    config: Optional[ExecutionConfig] = None,
    **overrides: Any,
) -> ExecutionConfig:
    """Return a validated execution config with optional overrides."""

    current = config or ExecutionConfig()
    fill_mode = str(overrides.get("fill_mode", current.fill_mode) or current.fill_mode)
    if fill_mode not in {FILL_MODE_BAR_CLOSE, FILL_MODE_NEXT_BAR_OPEN}:
        raise ValueError(f"Unsupported replay fill_mode: {fill_mode!r}")

    tick_model = str(overrides.get("tick_model", current.tick_model) or current.tick_model)
    if tick_model not in {TICK_MODEL_OPEN_CLOSE, TICK_MODEL_OHLC}:
        raise ValueError(f"Unsupported replay tick_model: {tick_model!r}")

    end_policy = str(overrides.get("end_policy", current.end_policy) or current.end_policy)
    if end_policy not in _END_POLICY_VALUES:
        raise ValueError(
            f"Unsupported replay end_policy: {end_policy!r} "
            f"(expected one of {sorted(_END_POLICY_VALUES)})"
        )

    return ExecutionConfig(
        fill_mode=fill_mode,
        tick_model=tick_model,
        slippage_bps=max(0.0, float(overrides.get("slippage_bps", current.slippage_bps) or 0.0)),
        fixed_slippage=max(0.0, float(overrides.get("fixed_slippage", current.fixed_slippage) or 0.0)),
        spread_bps=max(0.0, float(overrides.get("spread_bps", current.spread_bps) or 0.0)),
        latency_bars=max(0, int(overrides.get("latency_bars", current.latency_bars) or 0)),
        reject_entries_without_future_bar=bool(
            overrides.get(
                "reject_entries_without_future_bar",
                current.reject_entries_without_future_bar,
            )
        ),
        end_policy=end_policy,
    )


def build_tick_path(
    *,
    ts_start: datetime,
    ts_end: datetime,
    o: float,
    h: float,
    l: float,  # noqa: E741
    c: float,
    tick_model: str,
) -> List[TickEvent]:
    """Create a deterministic intra-bar tick path."""

    start = _coerce_utc(ts_start)
    end = _coerce_utc(ts_end)
    if end <= start:
        end = start + timedelta(seconds=1)

    events: List[TickEvent] = [
        TickEvent(timestamp=start, price=float(o), phase="bar_open"),
    ]
    if tick_model == TICK_MODEL_OHLC:
        first_offset = start + ((end - start) / 3)
        second_offset = start + (((end - start) * 2) / 3)
        if float(c) >= float(o):
            mids = [
                TickEvent(timestamp=first_offset, price=float(l), phase="bar_low"),
                TickEvent(timestamp=second_offset, price=float(h), phase="bar_high"),
            ]
        else:
            mids = [
                TickEvent(timestamp=first_offset, price=float(h), phase="bar_high"),
                TickEvent(timestamp=second_offset, price=float(l), phase="bar_low"),
            ]
        events.extend(mids)
    events.append(TickEvent(timestamp=end, price=float(c), phase="bar_close_tick"))
    return _dedupe_ticks(events)


def fill_slippage_amount(price: float, config: ExecutionConfig) -> float:
    """Convert configured slippage/spread penalties into an absolute price amount."""

    base = abs(float(price))
    bps_penalty = base * ((float(config.slippage_bps) + (float(config.spread_bps) / 2.0)) / 10000.0)
    return max(0.0, bps_penalty + float(config.fixed_slippage))


def time_bucket_ist(value: datetime) -> str:
    """Bucket a replay timestamp into a simple intraday segment."""

    ts = _coerce_utc(value).astimezone(IST)
    clock = ts.time()
    if clock < ts.replace(hour=10, minute=30, second=0, microsecond=0).time():
        return "open"
    if clock < ts.replace(hour=12, minute=0, second=0, microsecond=0).time():
        return "morning"
    if clock < ts.replace(hour=14, minute=0, second=0, microsecond=0).time():
        return "midday"
    if clock < ts.replace(hour=15, minute=15, second=0, microsecond=0).time():
        return "afternoon"
    return "late_session"


def adx_regime(adx: Optional[float]) -> str:
    if adx is None:
        return "unknown"
    if adx < 18:
        return "weak_trend"
    if adx < 25:
        return "moderate_trend"
    return "strong_trend"


def atr_regime(atr: Optional[float], close: Optional[float]) -> str:
    if atr is None or close in (None, 0):
        return "unknown"
    ratio = abs(float(atr)) / max(abs(float(close)), 0.000001)
    if ratio < 0.001:
        return "low_vol"
    if ratio < 0.0025:
        return "normal_vol"
    return "high_vol"


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe_ticks(events: Iterable[TickEvent]) -> List[TickEvent]:
    out: List[TickEvent] = []
    for event in events:
        if out and out[-1].price == event.price and out[-1].timestamp == event.timestamp:
            continue
        out.append(event)
    return out


__all__ = [
    "ExecutionConfig",
    "FILL_MODE_BAR_CLOSE",
    "FILL_MODE_NEXT_BAR_OPEN",
    "TICK_MODEL_OHLC",
    "TICK_MODEL_OPEN_CLOSE",
    "END_POLICY_FORCE_EXIT",
    "END_POLICY_CARRY_OVER",
    "END_POLICY_DAILY_MTM",
    "TickEvent",
    "adx_regime",
    "atr_regime",
    "build_tick_path",
    "fill_slippage_amount",
    "normalize_execution_config",
    "time_bucket_ist",
]
