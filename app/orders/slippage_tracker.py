"""Slippage and fill-quality measurement — PHX-EXEC-004.

Captures per-order execution quality metrics:
  - Arrival price (mid-price at time of order submission)
  - Fill price
  - Implementation shortfall = (fill_price - arrival_price) / arrival_price * 10000 bps
  - Fill ratio (fill_qty / intended_qty)
  - Time-to-fill (seconds from submission to terminal fill)

Metrics are stored in-memory (with Prometheus export) and persisted to
Postgres for dashboard queries.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_MAX_RECORDS = 5000


@dataclass
class SlippageRecord:
    record_id: str
    order_intent_id: str
    symbol: str
    side: str                       # "BUY" | "SELL"
    strategy_id: str
    account_id: str
    intended_qty: int
    fill_qty: int
    arrival_price: Optional[float]  # mid-price at submission
    fill_price: Optional[float]     # actual fill price
    slippage_bps: Optional[float]   # implementation shortfall in bps
    fill_ratio: float               # fill_qty / intended_qty
    submitted_at: float             # monotonic timestamp
    filled_at: Optional[float]      # monotonic timestamp
    time_to_fill_seconds: Optional[float]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "order_intent_id": self.order_intent_id,
            "symbol": self.symbol,
            "side": self.side,
            "strategy_id": self.strategy_id,
            "account_id": self.account_id,
            "intended_qty": self.intended_qty,
            "fill_qty": self.fill_qty,
            "arrival_price": self.arrival_price,
            "fill_price": self.fill_price,
            "slippage_bps": self.slippage_bps,
            "fill_ratio": self.fill_ratio,
            "time_to_fill_seconds": self.time_to_fill_seconds,
            "created_at": self.created_at,
        }


class SlippageTracker:
    """Thread-safe slippage and fill-quality tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, SlippageRecord] = {}   # intent_id -> record
        self._completed: list[SlippageRecord] = []

    def record_submission(
        self,
        *,
        order_intent_id: str,
        symbol: str,
        side: str,
        strategy_id: str,
        account_id: str,
        intended_qty: int,
        arrival_price: Optional[float],
    ) -> SlippageRecord:
        """Call when an order is submitted to the broker."""
        rec = SlippageRecord(
            record_id=uuid4().hex,
            order_intent_id=order_intent_id,
            symbol=symbol,
            side=side,
            strategy_id=strategy_id,
            account_id=account_id,
            intended_qty=intended_qty,
            fill_qty=0,
            arrival_price=arrival_price,
            fill_price=None,
            slippage_bps=None,
            fill_ratio=0.0,
            submitted_at=time.monotonic(),
            filled_at=None,
            time_to_fill_seconds=None,
        )
        with self._lock:
            self._pending[order_intent_id] = rec
        return rec

    def record_fill(
        self,
        *,
        order_intent_id: str,
        fill_price: float,
        fill_qty: int,
    ) -> Optional[SlippageRecord]:
        """Call when an order reaches a FILLED terminal state."""
        with self._lock:
            rec = self._pending.pop(order_intent_id, None)
        if rec is None:
            logger.debug("slippage_record_not_found intent_id=%s", order_intent_id)
            return None

        now_mono = time.monotonic()
        rec.fill_price = fill_price
        rec.fill_qty = fill_qty
        rec.filled_at = now_mono
        rec.time_to_fill_seconds = round(now_mono - rec.submitted_at, 3)
        rec.fill_ratio = fill_qty / rec.intended_qty if rec.intended_qty > 0 else 0.0

        if rec.arrival_price and rec.arrival_price > 0 and fill_price:
            raw_slip = (fill_price - rec.arrival_price) / rec.arrival_price * 10_000
            # For sells, slippage is negative if we got a better price
            rec.slippage_bps = round(raw_slip if rec.side.upper() == "BUY" else -raw_slip, 2)

        with self._lock:
            self._completed.append(rec)
            if len(self._completed) > _MAX_RECORDS:
                self._completed = self._completed[-int(_MAX_RECORDS * 0.8):]

        self._emit_prometheus(rec)
        self._try_postgres_persist(rec)

        logger.info(
            "slippage_recorded symbol=%s side=%s fill_price=%.2f slippage_bps=%s ttf=%.3fs",
            rec.symbol, rec.side, fill_price,
            rec.slippage_bps, rec.time_to_fill_seconds,
        )
        return rec

    def record_non_fill(self, *, order_intent_id: str, final_state: str) -> None:
        """Call when an order reaches a non-fill terminal state (REJECTED/CANCELLED/EXPIRED)."""
        with self._lock:
            rec = self._pending.pop(order_intent_id, None)
        if rec is None:
            return
        rec.fill_ratio = 0.0
        rec.time_to_fill_seconds = round(time.monotonic() - rec.submitted_at, 3)
        with self._lock:
            self._completed.append(rec)

    def summary(
        self,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 200,
    ) -> dict:
        """Return slippage summary statistics."""
        with self._lock:
            records = list(self._completed)

        filtered = [
            r for r in records
            if (strategy_id is None or r.strategy_id == strategy_id)
            and (symbol is None or r.symbol == symbol)
            and r.slippage_bps is not None
        ]

        if not filtered:
            return {"count": 0}

        slippages = [r.slippage_bps for r in filtered if r.slippage_bps is not None]
        ttfs = [r.time_to_fill_seconds for r in filtered if r.time_to_fill_seconds is not None]
        fill_ratios = [r.fill_ratio for r in filtered]

        return {
            "count": len(filtered),
            "slippage_bps": {
                "mean": round(sum(slippages) / len(slippages), 2),
                "min": round(min(slippages), 2),
                "max": round(max(slippages), 2),
                "p95": round(sorted(slippages)[int(len(slippages) * 0.95)], 2),
            },
            "time_to_fill_seconds": {
                "mean": round(sum(ttfs) / len(ttfs), 3) if ttfs else None,
                "p95": round(sorted(ttfs)[int(len(ttfs) * 0.95)], 3) if ttfs else None,
            },
            "fill_ratio_mean": round(sum(fill_ratios) / len(fill_ratios), 4),
            "recent": [r.to_dict() for r in reversed(filtered[-min(limit, len(filtered)):])],
        }

    @staticmethod
    def _emit_prometheus(rec: SlippageRecord) -> None:
        try:
            # Lazy-register per-strategy slippage histogram
            # (reuse existing metrics module patterns)
            pass  # Prometheus metric emission handled in metrics.py
        except Exception:
            pass

    @staticmethod
    def _try_postgres_persist(rec: SlippageRecord) -> None:
        try:
            from app.data.postgres import connect_with_retry, get_control_plane_dsn
            dsn = get_control_plane_dsn()
            with connect_with_retry(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO slippage_records
                            (record_id, order_intent_id, symbol, side, strategy_id,
                             account_id, intended_qty, fill_qty, arrival_price,
                             fill_price, slippage_bps, fill_ratio,
                             time_to_fill_seconds, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (record_id) DO NOTHING
                        """,
                        (
                            rec.record_id, rec.order_intent_id, rec.symbol, rec.side,
                            rec.strategy_id, rec.account_id, rec.intended_qty, rec.fill_qty,
                            rec.arrival_price, rec.fill_price, rec.slippage_bps,
                            rec.fill_ratio, rec.time_to_fill_seconds, rec.created_at,
                        ),
                    )
        except Exception:
            logger.debug("Postgres slippage persist unavailable", exc_info=True)


# Module-level singleton
slippage_tracker = SlippageTracker()


__all__ = ["SlippageRecord", "SlippageTracker", "slippage_tracker"]
