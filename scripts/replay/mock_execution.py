"""
Mock execution layer for replay mode.

Intercepts place_order_via_bridge calls and records replay-local fills without
touching broker or hub infrastructure.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from app.brokers.base import OrderPurpose, OrderResponse

from scripts.replay.execution_models import (
    ExecutionConfig,
    FILL_MODE_NEXT_BAR_OPEN,
    fill_slippage_amount,
    normalize_execution_config,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class ReplayFill:
    """A single simulated fill produced by replay-local execution."""

    timestamp: datetime
    strategy_id: str
    underlying: str
    side: str
    purpose: str
    tag: str
    quantity: int
    fill_price: float
    strategy_context: Dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "replay_local"
    phase: str = "bar_close"
    reference_price: Optional[float] = None
    fill_note: str = ""
    timeframe_seconds: Optional[int] = None
    symbol: str = ""


@dataclass(frozen=True)
class ReplayGateDecision:
    """A single gate decision emitted by a strategy during replay."""

    strategy_id: str
    underlying: str
    gate: str
    passed: bool
    reason: str
    timeframe_seconds: Optional[int]
    bar_start_ts: Optional[str]
    regime: str = ""
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayMarketContext:
    """Current deterministic market context used for replay fills."""

    timestamp: datetime
    underlying: str
    current_price: float
    phase: str
    timeframe_seconds: Optional[int] = None
    bar_start_ts: Optional[datetime] = None
    bar_end_ts: Optional[datetime] = None
    price_open: Optional[float] = None
    price_high: Optional[float] = None
    price_low: Optional[float] = None
    price_close: Optional[float] = None
    next_open_price: Optional[float] = None
    next_open_ts: Optional[datetime] = None
    indicators: Dict[str, Any] = field(default_factory=dict)
    instrument_prices: Dict[str, float] = field(default_factory=dict)
    next_instrument_prices: Dict[str, float] = field(default_factory=dict)


class MockExecutionRecorder:
    """Accumulates fills, gate decisions, and execution diagnostics."""

    def __init__(self, *, execution_config: Optional[ExecutionConfig] = None) -> None:
        self.execution_config = normalize_execution_config(execution_config)
        self.fills: List[ReplayFill] = []
        self.gate_decisions: List[ReplayGateDecision] = []
        self.db_bar_count: int = 0
        self.db_bar_count_by_timeframe: Dict[int, int] = {}
        self.replay_first_date: Optional[date] = None
        self.replay_last_date: Optional[date] = None
        self.replay_trading_days: int = 0
        self.data_profile: Dict[str, Any] = {}
        self.finalization_events: List[Dict[str, Any]] = []
        self.session_events: List[Dict[str, Any]] = []
        self._current_context: Optional[ReplayMarketContext] = None

    def set_db_bar_counts(self, counts_by_timeframe: Dict[int, int]) -> None:
        """Persist actual Postgres bar counts for reporting."""

        normalized = {
            int(tf): int(count)
            for tf, count in (counts_by_timeframe or {}).items()
            if int(count) >= 0
        }
        self.db_bar_count_by_timeframe = dict(sorted(normalized.items()))
        self.db_bar_count = sum(self.db_bar_count_by_timeframe.values())

    def set_replay_window(
        self,
        *,
        trading_dates: List[date],
    ) -> None:
        """Persist the actual replay bar-window dates for reporting."""

        normalized = sorted({d for d in trading_dates if isinstance(d, date)})
        self.replay_first_date = normalized[0] if normalized else None
        self.replay_last_date = normalized[-1] if normalized else None
        self.replay_trading_days = len(normalized)

    def set_data_profile(self, profile: Dict[str, Any]) -> None:
        self.data_profile = dict(profile or {})

    def add_finalization_event(self, **payload: Any) -> None:
        self.finalization_events.append({str(k): v for k, v in payload.items()})

    def add_session_event(self, **payload: Any) -> None:
        self.session_events.append({str(k): v for k, v in payload.items()})

    def set_market_context(self, context: ReplayMarketContext) -> None:
        self._current_context = context

    def set_bar_context(self, close: float, ts: datetime, underlying: str) -> None:
        """Backward-compatible shim used by older tests."""

        self._current_context = ReplayMarketContext(
            timestamp=ts,
            underlying=underlying,
            current_price=float(close),
            phase="bar_close",
            price_close=float(close),
        )

    def set_tick_context(self, price: float, ts: datetime, underlying: str) -> None:
        """Backward-compatible shim used by older tests."""

        self._current_context = ReplayMarketContext(
            timestamp=ts,
            underlying=underlying,
            current_price=float(price),
            phase="tick",
        )

    def record_gate_decision(
        self,
        *,
        strategy_id: str,
        payload: Dict[str, Any],
    ) -> None:
        """Persist a single gate decision emitted by a strategy."""

        raw_payload = dict(payload or {})
        context = {
            str(key): value
            for key, value in raw_payload.items()
            if key
            not in {
                "underlying",
                "gate",
                "passed",
                "reason",
                "bar_start_ts",
                "timeframe_seconds",
                "regime",
            }
            and value is not None
        }
        timeframe_raw = raw_payload.get("timeframe_seconds")
        try:
            timeframe_seconds = (
                int(timeframe_raw) if timeframe_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            timeframe_seconds = None

        underlying = str(raw_payload.get("underlying") or "")
        if not underlying and self._current_context is not None:
            underlying = self._current_context.underlying
        regime = str(raw_payload.get("regime") or "")
        if not regime and self._current_context is not None:
            regime = str(self._current_context.indicators.get("regime") or "")

        decision = ReplayGateDecision(
            strategy_id=str(strategy_id),
            underlying=underlying,
            gate=str(raw_payload.get("gate") or "unknown"),
            passed=bool(raw_payload.get("passed")),
            reason=str(raw_payload.get("reason") or "unspecified"),
            timeframe_seconds=timeframe_seconds,
            bar_start_ts=(
                str(raw_payload.get("bar_start_ts"))
                if raw_payload.get("bar_start_ts") not in (None, "")
                else None
            ),
            regime=regime or "unknown",
            context=context,
        )
        self.gate_decisions.append(decision)

    def gate_summary_rows(self) -> List[Dict[str, Any]]:
        """Aggregate gate decisions for report-friendly output."""

        buckets: Dict[tuple[str, str, bool, Optional[int], str], int] = defaultdict(int)
        for decision in self.gate_decisions:
            buckets[
                (
                    decision.gate,
                    decision.reason,
                    bool(decision.passed),
                    decision.timeframe_seconds,
                    decision.regime or "unknown",
                )
            ] += 1

        rows = [
            {
                "gate": gate,
                "reason": reason,
                "passed": passed,
                "timeframe_seconds": timeframe_seconds,
                "regime": regime,
                "count": count,
            }
            for (gate, reason, passed, timeframe_seconds, regime), count in buckets.items()
        ]
        rows.sort(
            key=lambda row: (
                1 if row["passed"] else 0,
                -int(row["count"]),
                str(row["gate"]),
                str(row["reason"]),
                str(row["regime"]),
                (
                    int(row["timeframe_seconds"])
                    if row["timeframe_seconds"] not in (None, "")
                    else -1
                ),
            )
        )
        return rows

    def record_order(
        self,
        *,
        strategy_id: str,
        order_req: Any,
        underlying: Optional[str] = None,
    ) -> OrderResponse:
        """Record a replay-local fill and return a bridge-compatible response."""

        if self._current_context is None:
            raise RuntimeError(
                "Replay order sink is active but no replay market context is set. "
                "ReplayEngine must update execution context before strategy execution."
            )

        context = self._current_context
        side = str(getattr(order_req, "side", "UNKNOWN")).upper()
        if hasattr(order_req.side, "value"):
            side = order_req.side.value

        purpose_raw = getattr(order_req, "purpose", None)
        purpose = "UNKNOWN"
        if purpose_raw is not None:
            purpose = (
                purpose_raw.value if hasattr(purpose_raw, "value") else str(purpose_raw)
            )

        qty = int(getattr(order_req, "quantity", 1))
        tag = str(getattr(order_req, "tag", "") or "")
        symbol = str(getattr(order_req, "symbol", "") or "")
        position_label = str(getattr(order_req, "position_label", "") or "")
        fill_ts, reference_price, fill_note = self._resolve_reference_price(
            order_req=order_req,
            context=context,
        )
        if reference_price is None:
            return OrderResponse(
                broker_order_id="",
                status="FAILED",
                message="replay_next_bar_open_unavailable",
                filled_quantity=0,
                average_price=None,
                requested_quantity=qty,
                execution_mode="replay_local",
                virtual=True,
                details={
                    "replay_phase": context.phase,
                    "replay_underlying": underlying or context.underlying,
                },
            )

        slippage_amount = fill_slippage_amount(reference_price, self.execution_config)
        adjusted_fill_price = (
            float(reference_price) + slippage_amount
            if side == "BUY"
            else float(reference_price) - slippage_amount
        )
        adjusted_fill_price = max(0.01, adjusted_fill_price)

        raw_ctx = dict(getattr(order_req, "strategy_context", None) or {})
        replay_ctx = {
            "phase": context.phase,
            "fill_mode": self.execution_config.fill_mode,
            "timeframe_seconds": context.timeframe_seconds,
            "bar_start_ts": (
                context.bar_start_ts.isoformat() if context.bar_start_ts else None
            ),
            "bar_end_ts": (
                context.bar_end_ts.isoformat() if context.bar_end_ts else None
            ),
            "close": context.price_close,
            "open": context.price_open,
            "high": context.price_high,
            "low": context.price_low,
            "current_price": context.current_price,
            "reference_price": reference_price,
            "time_bucket": fill_ts.astimezone(IST).strftime("%H:%M") if fill_ts.tzinfo else fill_ts.isoformat(),
            "time_of_day": fill_ts.astimezone(IST).strftime("%H:%M") if fill_ts.tzinfo else "",
            "atr": context.indicators.get("atr"),
            "rsi": context.indicators.get("rsi"),
            "adx": context.indicators.get("adx"),
            "di_spread": context.indicators.get("di_spread"),
            "ema_20": context.indicators.get("ema_20"),
            "ema_30": context.indicators.get("ema_30"),
            "ema_50": context.indicators.get("ema_50"),
            "regime": context.indicators.get("regime"),
            "slippage_abs": slippage_amount,
            "fill_note": fill_note,
            "symbol": symbol,
            "position_label": position_label,
        }
        strategy_context = dict(raw_ctx)
        strategy_context["_replay"] = replay_ctx

        fill = ReplayFill(
            timestamp=fill_ts,
            strategy_id=str(strategy_id),
            underlying=underlying or context.underlying,
            side=side,
            purpose=purpose,
            tag=tag,
            quantity=qty,
            fill_price=adjusted_fill_price,
            strategy_context=strategy_context,
            execution_mode="replay_local",
            phase=context.phase,
            reference_price=reference_price,
            fill_note=fill_note,
            timeframe_seconds=context.timeframe_seconds,
            symbol=symbol,
        )
        self.fills.append(fill)
        logger.debug(
            "REPLAY FILL | %s %s %s qty=%d price=%.2f ref=%.2f mode=%s phase=%s note=%s",
            strategy_id,
            side,
            purpose,
            qty,
            fill.fill_price,
            float(reference_price),
            self.execution_config.fill_mode,
            context.phase,
            fill_note,
        )
        return OrderResponse(
            broker_order_id=f"REPLAY_{len(self.fills)}",
            status="COMPLETE",
            message="replay_local_fill",
            filled_quantity=qty,
            average_price=fill.fill_price,
            requested_quantity=qty,
            execution_mode="replay_local",
            virtual=True,
            details={
                "replay_underlying": fill.underlying,
                "replay_timestamp": fill.timestamp.isoformat(),
                "replay_phase": context.phase,
                "replay_reference_price": reference_price,
                "replay_fill_note": fill_note,
            },
        )

    def _resolve_reference_price(
        self,
        *,
        order_req: Any,
        context: ReplayMarketContext,
    ) -> tuple[datetime, Optional[float], str]:
        symbol = str(getattr(order_req, "symbol", "") or "")
        position_label = str(getattr(order_req, "position_label", "") or "")
        purpose_raw = getattr(order_req, "purpose", None)
        purpose = (
            purpose_raw.value if hasattr(purpose_raw, "value") else str(purpose_raw or "")
        ).upper()

        price = context.current_price
        instrument_price_matched = False
        instrument_key = ""
        if position_label and position_label in context.instrument_prices:
            instrument_key = position_label
            price = context.instrument_prices[position_label]
            instrument_price_matched = True
        elif symbol and symbol in context.instrument_prices:
            instrument_key = symbol
            price = context.instrument_prices[symbol]
            instrument_price_matched = _looks_like_option_symbol(symbol)

        use_future_open = (
            self.execution_config.fill_mode == FILL_MODE_NEXT_BAR_OPEN
            and context.phase == "bar_close"
            and purpose == OrderPurpose.ENTRY.value
        )
        if use_future_open:
            if context.next_open_price is None:
                if self.execution_config.reject_entries_without_future_bar:
                    return context.timestamp, None, "missing_future_bar"
                return context.timestamp, price, "bar_close_fallback"
            if instrument_price_matched:
                next_price = context.next_instrument_prices.get(instrument_key)
                return (
                    context.next_open_ts or context.timestamp,
                    float(next_price if next_price is not None else price),
                    "next_bar_open_option_proxy",
                )
            return (
                context.next_open_ts or context.timestamp,
                float(context.next_open_price),
                "next_bar_open",
            )
        return context.timestamp, float(price), "current_market"

    def mock_place_order(
        self,
        strategy_id: str,
        order_req: Any,
        **kwargs: Any,
    ) -> OrderResponse:
        """Drop-in replacement for place_order_via_bridge."""

        return self.record_order(
            strategy_id=strategy_id,
            order_req=order_req,
            underlying=kwargs.get("underlying"),
        )

    def patch_bridge(self) -> Any:
        """Return a context manager that patches place_order_via_bridge."""

        return patch(
            "app.orders.strategy_bridge.place_order_via_bridge",
            side_effect=self.mock_place_order,
        )


def _looks_like_option_symbol(value: str) -> bool:
    text = str(value or "").strip().upper()
    return bool("_CE_" in text or "_PE_" in text or text.endswith("CE") or text.endswith("PE"))


class MockRiskManager:
    """Minimal risk manager stub satisfying strategy constructors."""

    def get_open_position(self, *args: Any, **kwargs: Any) -> None:
        return None

    def register_position(self, *args: Any, **kwargs: Any) -> None:
        pass

    def clear_position(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_positions(self, *args: Any, **kwargs: Any) -> List:
        return []

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **kw: None


class MockOrderClient:
    """Minimal order client stub satisfying strategy constructors."""

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **kw: None


def build_mock_instrument_meta(
    underlying_label: str,
    lot_size: int = 1,
) -> Dict[str, Dict[str, Any]]:
    """Build a minimal instrument_meta dict for strategy construction."""

    key = underlying_label.replace("_IDX", "")
    option_key = key.replace("_FUT", "")
    base_label = (
        underlying_label
        if underlying_label.endswith(("_IDX", "_FUT"))
        else f"{key}_IDX"
    )

    entries: Dict[str, Dict[str, Any]] = {}
    entries[base_label] = {
        "label": base_label,
        "broker_symbol": base_label,
        "exchange": "NSE",
        "symbol_token": "99999",
        "lot_size": lot_size,
        "instrument_type": "INDEX",
    }
    ce_label = f"{option_key}_ATM_CE"
    entries[ce_label] = {
        "label": ce_label,
        "broker_symbol": f"{option_key}CEXXXXXX",
        "exchange": "NFO",
        "symbol_token": "88888",
        "lot_size": lot_size,
        "instrument_type": "OPTIDX",
    }
    pe_label = f"{option_key}_ATM_PE"
    entries[pe_label] = {
        "label": pe_label,
        "broker_symbol": f"{option_key}PEXXXXXX",
        "exchange": "NFO",
        "symbol_token": "77777",
        "lot_size": lot_size,
        "instrument_type": "OPTIDX",
    }
    return entries


__all__ = [
    "MockExecutionRecorder",
    "MockOrderClient",
    "MockRiskManager",
    "ReplayFill",
    "ReplayGateDecision",
    "ReplayMarketContext",
    "build_mock_instrument_meta",
]
