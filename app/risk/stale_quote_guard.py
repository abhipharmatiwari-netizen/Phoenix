"""Stale-quote safety circuit — PHX-RISK-004.

Orders are blocked when market data is stale or degraded.
Configurable per-instrument-class thresholds; degrade-to-no-new-orders
mode is supported.

Integration: call `stale_quote_guard.check_or_raise(symbol)` in the
order router before submitting. Call `record_quote(symbol)` whenever
a fresh quote arrives.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


@dataclass
class QuoteStalenessPolicy:
    """Per-instrument-class staleness policy."""
    instrument_class: str       # e.g. "equity", "option", "index"
    warn_seconds: float = 10.0  # emit warning after this many seconds
    block_seconds: float = 30.0 # block orders after this many seconds
    degrade_seconds: float = 60.0  # enter DEGRADED mode after this many seconds


# Default policies
_DEFAULT_POLICIES: dict[str, QuoteStalenessPolicy] = {
    "equity": QuoteStalenessPolicy("equity", warn_seconds=5.0, block_seconds=15.0, degrade_seconds=30.0),
    "option": QuoteStalenessPolicy("option", warn_seconds=3.0, block_seconds=10.0, degrade_seconds=20.0),
    "index": QuoteStalenessPolicy("index", warn_seconds=5.0, block_seconds=15.0, degrade_seconds=30.0),
    "default": QuoteStalenessPolicy("default", warn_seconds=10.0, block_seconds=30.0, degrade_seconds=60.0),
}


class StaleQuoteViolation(Exception):
    """Raised when an order is blocked due to stale market data."""

    def __init__(self, symbol: str, age_seconds: float, threshold: float) -> None:
        self.symbol = symbol
        self.age_seconds = age_seconds
        self.threshold = threshold
        super().__init__(
            f"Stale quote for {symbol}: {age_seconds:.1f}s old (limit {threshold:.1f}s)"
        )


class StaleQuoteGuard:
    """Thread-safe stale-quote detection and circuit breaker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_quote_ts: dict[str, float] = {}
        self._symbol_class: dict[str, str] = {}  # symbol -> instrument_class
        self._policies: dict[str, QuoteStalenessPolicy] = dict(_DEFAULT_POLICIES)
        self._degraded_symbols: set[str] = set()
        self._global_no_new_orders: bool = False

    def set_policy(self, policy: QuoteStalenessPolicy) -> None:
        with self._lock:
            self._policies[policy.instrument_class] = policy

    def register_symbol(self, symbol: str, instrument_class: str = "default") -> None:
        with self._lock:
            self._symbol_class[symbol] = instrument_class

    def record_quote(self, symbol: str) -> None:
        """Record that a fresh quote arrived for symbol."""
        now = time.monotonic()
        with self._lock:
            self._last_quote_ts[symbol] = now
            if symbol in self._degraded_symbols:
                self._degraded_symbols.discard(symbol)
                logger.info("stale_quote_recovered symbol=%s", symbol)

    def get_age_seconds(self, symbol: str) -> Optional[float]:
        """Return age of last quote in seconds, or None if never received."""
        with self._lock:
            ts = self._last_quote_ts.get(symbol)
        if ts is None:
            return None
        return time.monotonic() - ts

    def _get_policy(self, symbol: str) -> QuoteStalenessPolicy:
        with self._lock:
            cls = self._symbol_class.get(symbol, "default")
            return self._policies.get(cls, self._policies["default"])

    def check_or_raise(self, symbol: str) -> None:
        """Check if a quote is fresh enough to allow order submission.

        Raises StaleQuoteViolation if blocked.
        """
        with self._lock:
            global_blocked = self._global_no_new_orders
            degraded = symbol in self._degraded_symbols
            ts = self._last_quote_ts.get(symbol)

        if global_blocked:
            raise StaleQuoteViolation(symbol, float("inf"), 0.0)

        if degraded:
            raise StaleQuoteViolation(symbol, float("inf"), 0.0)

        if ts is None:
            # Never received a quote — block
            raise StaleQuoteViolation(symbol, float("inf"), 0.0)

        age = time.monotonic() - ts
        policy = self._get_policy(symbol)

        if age >= policy.degrade_seconds:
            with self._lock:
                self._degraded_symbols.add(symbol)
            emit_audit_event(
                actor="system",
                action="quote_degraded",
                resource_type="symbol",
                resource_id=symbol,
                after={"age_seconds": age, "threshold": policy.degrade_seconds},
            )
            logger.error(
                "stale_quote_degraded symbol=%s age=%.1fs threshold=%.1fs",
                symbol, age, policy.degrade_seconds,
            )
            raise StaleQuoteViolation(symbol, age, policy.block_seconds)

        if age >= policy.block_seconds:
            logger.warning(
                "stale_quote_blocked symbol=%s age=%.1fs threshold=%.1fs",
                symbol, age, policy.block_seconds,
            )
            raise StaleQuoteViolation(symbol, age, policy.block_seconds)

        if age >= policy.warn_seconds:
            logger.warning(
                "stale_quote_warning symbol=%s age=%.1fs warn_threshold=%.1fs",
                symbol, age, policy.warn_seconds,
            )

    def set_global_no_new_orders(self, enabled: bool, actor: str = "system") -> None:
        """Enable/disable global no-new-orders mode."""
        with self._lock:
            self._global_no_new_orders = enabled
        emit_audit_event(
            actor=actor,
            action="global_no_new_orders_changed",
            resource_type="stale_quote_guard",
            resource_id="global",
            after={"enabled": enabled},
        )
        logger.warning("global_no_new_orders=%s actor=%s", enabled, actor)

    def status_snapshot(self) -> dict:
        with self._lock:
            degraded = list(self._degraded_symbols)
            global_blocked = self._global_no_new_orders
            ages = {
                sym: time.monotonic() - ts
                for sym, ts in self._last_quote_ts.items()
            }
        return {
            "global_no_new_orders": global_blocked,
            "degraded_symbols": degraded,
            "quote_ages_seconds": {k: round(v, 2) for k, v in ages.items()},
        }


# Module-level singleton
stale_quote_guard = StaleQuoteGuard()


__all__ = [
    "QuoteStalenessPolicy",
    "StaleQuoteGuard",
    "StaleQuoteViolation",
    "stale_quote_guard",
]
