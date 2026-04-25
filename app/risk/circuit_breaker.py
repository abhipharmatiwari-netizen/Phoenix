"""
RISK-5.4: Trading circuit breakers.

Auto-disable new entries when loss streak, volatility spikes, or broker
reject rate exceeds configurable thresholds. Auto-recovery with bounded
cooldown.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Optional

from app.risk.circuit_breaker_store import (
    CircuitBreakerStateStore,
    build_circuit_breaker_state_store,
)

logger = logging.getLogger(__name__)


class BreakerType(str, Enum):
    LOSS_STREAK = "loss_streak"
    REJECT_RATE = "reject_rate"
    VOLATILITY = "volatility"


class BreakerState(str, Enum):
    CLOSED = "closed"  # Normal — entries allowed
    OPEN = "open"  # Tripped — entries blocked


@dataclass
class BreakerTrip:
    breaker_type: BreakerType
    tripped_at: float  # time.monotonic()
    cooldown_seconds: float
    reason: str

    @property
    def recovers_at(self) -> float:
        return self.tripped_at + self.cooldown_seconds

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.monotonic()) >= self.recovers_at


@dataclass
class CircuitBreakerConfig:
    # Loss streak
    loss_streak_enabled: bool = True
    loss_streak_threshold: int = 5
    loss_streak_cooldown_seconds: float = 900.0  # 15 min

    # Broker reject rate
    reject_rate_enabled: bool = True
    reject_rate_threshold: float = 0.5  # 50% rejects in window
    reject_rate_window_seconds: float = 300.0  # 5 min window
    reject_rate_min_samples: int = 5
    reject_rate_cooldown_seconds: float = 600.0  # 10 min

    # Volatility
    volatility_enabled: bool = False
    volatility_threshold: float = 30.0  # Proxy VIX level
    volatility_cooldown_seconds: float = 1800.0  # 30 min

    @classmethod
    def from_env(cls) -> "CircuitBreakerConfig":
        """Build config from environment variables with sensible defaults."""
        import os

        def _bool_env(name: str, default: bool) -> bool:
            raw = str(os.getenv(name, str(default))).strip().lower()
            return raw in {"1", "true", "yes", "on"}

        def _float_env(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        def _int_env(name: str, default: int) -> int:
            try:
                return int(os.getenv(name, str(default)))
            except (TypeError, ValueError):
                return default

        return cls(
            loss_streak_enabled=_bool_env("CB_LOSS_STREAK_ENABLED", True),
            loss_streak_threshold=_int_env("CB_LOSS_STREAK_THRESHOLD", 5),
            loss_streak_cooldown_seconds=_float_env("CB_LOSS_STREAK_COOLDOWN_SECONDS", 900.0),
            reject_rate_enabled=_bool_env("CB_REJECT_RATE_ENABLED", True),
            reject_rate_threshold=_float_env("CB_REJECT_RATE_THRESHOLD", 0.5),
            reject_rate_window_seconds=_float_env("CB_REJECT_RATE_WINDOW_SECONDS", 300.0),
            reject_rate_min_samples=_int_env("CB_REJECT_RATE_MIN_SAMPLES", 5),
            reject_rate_cooldown_seconds=_float_env("CB_REJECT_RATE_COOLDOWN_SECONDS", 600.0),
            volatility_enabled=_bool_env("CB_VOLATILITY_ENABLED", False),
            volatility_threshold=_float_env("CB_VOLATILITY_THRESHOLD", 30.0),
            volatility_cooldown_seconds=_float_env("CB_VOLATILITY_COOLDOWN_SECONDS", 1800.0),
        )


@dataclass
class CircuitBreakerStatus:
    state: BreakerState
    active_trips: list[BreakerTrip] = field(default_factory=list)
    entries_blocked: bool = False
    reason: Optional[str] = None
    next_recovery_at: Optional[float] = None


class TradingCircuitBreaker:
    """
    Composite circuit breaker that aggregates multiple breaker types.
    Blocks new entries when ANY breaker is tripped; exits always pass.
    """

    def __init__(
        self,
        config: Optional[CircuitBreakerConfig] = None,
        *,
        state_store: Optional[CircuitBreakerStateStore] = None,
    ) -> None:
        self._config = config or CircuitBreakerConfig()
        self._lock = RLock()
        resolved_state_store = state_store
        if resolved_state_store is None:
            try:
                resolved_state_store = build_circuit_breaker_state_store()
            except Exception as exc:
                import os as _os
                _trade_mode = str(
                    _os.getenv("TRADE_MODE", "PAPER") or "PAPER"
                ).strip().upper()
                if _trade_mode == "LIVE":
                    # §111: In LIVE, circuit breaker state must be durable.
                    # An in-memory-only CB resets on every restart, allowing an
                    # order burst after repeated broker failures on reconnect.
                    raise RuntimeError(
                        "circuit_breaker: Postgres state store initialization failed in "
                        f"LIVE mode — cannot proceed without durable circuit breaker state: {exc}"
                    ) from exc
                logger.warning(
                    "circuit_breaker_state_store_init_failed error=%s "
                    "(non-fatal in PAPER/SHADOW — running in-memory)",
                    exc,
                )
                resolved_state_store = None
        self._state_store = resolved_state_store

        # State
        self._active_trips: dict[BreakerType, BreakerTrip] = {}
        self._consecutive_losses: int = 0
        self._order_results: deque[tuple[float, bool]] = deque(
            maxlen=200
        )  # (timestamp, is_reject)
        self._current_volatility: float = 0.0
        self._hydrate_state_from_store()

    # ------------------------------------------------------------------ #
    # Public query
    # ------------------------------------------------------------------ #

    def is_entry_allowed(self) -> tuple[bool, Optional[str]]:
        """Check if new entry orders are allowed. Returns (allowed, reason)."""
        with self._lock:
            self._expire_trips()
            if not self._active_trips:
                return True, None
            reasons = [t.reason for t in self._active_trips.values()]
            return False, "; ".join(reasons)

    def get_status(self) -> CircuitBreakerStatus:
        """Return current breaker status for dashboard/metrics."""
        with self._lock:
            self._expire_trips()
            trips = list(self._active_trips.values())
            if not trips:
                return CircuitBreakerStatus(
                    state=BreakerState.CLOSED, entries_blocked=False
                )
            next_recovery = min(t.recovers_at for t in trips)
            return CircuitBreakerStatus(
                state=BreakerState.OPEN,
                active_trips=trips,
                entries_blocked=True,
                reason="; ".join(t.reason for t in trips),
                next_recovery_at=next_recovery,
            )

    # ------------------------------------------------------------------ #
    # Event ingestion
    # ------------------------------------------------------------------ #

    def record_trade_result(self, is_loss: bool) -> None:
        """Call after each trade to update loss streak tracking."""
        with self._lock:
            if is_loss:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0
            self._check_loss_streak()
            self._persist_state_locked()

    def record_order_result(self, is_reject: bool) -> None:
        """Call after each order submission to update reject rate tracking."""
        with self._lock:
            self._order_results.append((time.monotonic(), is_reject))
            self._check_reject_rate()
            self._persist_state_locked()

    def update_volatility(self, volatility_proxy: float) -> None:
        """Update current volatility level (e.g. India VIX)."""
        with self._lock:
            self._current_volatility = volatility_proxy
            self._check_volatility()
            self._persist_state_locked()

    def reset(self) -> None:
        """Reset all breakers (manual recovery)."""
        with self._lock:
            self._active_trips.clear()
            self._consecutive_losses = 0
            self._order_results.clear()
            self._current_volatility = 0.0
            self._clear_persisted_state_locked()
            logger.info("circuit_breakers_reset manual=true")

    def reset_daily(self) -> None:
        """Reset counters for new trading day."""
        with self._lock:
            self._consecutive_losses = 0
            self._order_results.clear()
            self._active_trips.clear()
            self._current_volatility = 0.0
            self._clear_persisted_state_locked()
            logger.info("circuit_breakers_daily_reset")

    # ------------------------------------------------------------------ #
    # Internal checks
    # ------------------------------------------------------------------ #

    def _check_loss_streak(self) -> None:
        cfg = self._config
        if not cfg.loss_streak_enabled:
            return
        if self._consecutive_losses >= cfg.loss_streak_threshold:
            if BreakerType.LOSS_STREAK not in self._active_trips:
                trip = BreakerTrip(
                    breaker_type=BreakerType.LOSS_STREAK,
                    tripped_at=time.monotonic(),
                    cooldown_seconds=cfg.loss_streak_cooldown_seconds,
                    reason=f"loss_streak={self._consecutive_losses} >= {cfg.loss_streak_threshold}",
                )
                self._active_trips[BreakerType.LOSS_STREAK] = trip
                logger.warning(
                    "circuit_breaker_tripped type=loss_streak streak=%d threshold=%d cooldown=%ds",
                    self._consecutive_losses,
                    cfg.loss_streak_threshold,
                    int(cfg.loss_streak_cooldown_seconds),
                )

    def _check_reject_rate(self) -> None:
        cfg = self._config
        if not cfg.reject_rate_enabled:
            return
        now = time.monotonic()
        cutoff = now - cfg.reject_rate_window_seconds
        # Prune old entries
        while self._order_results and self._order_results[0][0] < cutoff:
            self._order_results.popleft()
        if len(self._order_results) < cfg.reject_rate_min_samples:
            return
        rejects = sum(1 for _, is_rej in self._order_results if is_rej)
        rate = rejects / len(self._order_results)
        if rate >= cfg.reject_rate_threshold:
            if BreakerType.REJECT_RATE not in self._active_trips:
                trip = BreakerTrip(
                    breaker_type=BreakerType.REJECT_RATE,
                    tripped_at=now,
                    cooldown_seconds=cfg.reject_rate_cooldown_seconds,
                    reason=f"reject_rate={rate:.0%} >= {cfg.reject_rate_threshold:.0%} ({rejects}/{len(self._order_results)})",
                )
                self._active_trips[BreakerType.REJECT_RATE] = trip
                logger.warning(
                    "circuit_breaker_tripped type=reject_rate rate=%.2f threshold=%.2f samples=%d cooldown=%ds",
                    rate,
                    cfg.reject_rate_threshold,
                    len(self._order_results),
                    int(cfg.reject_rate_cooldown_seconds),
                )

    def _check_volatility(self) -> None:
        cfg = self._config
        if not cfg.volatility_enabled:
            return
        if self._current_volatility >= cfg.volatility_threshold:
            if BreakerType.VOLATILITY not in self._active_trips:
                trip = BreakerTrip(
                    breaker_type=BreakerType.VOLATILITY,
                    tripped_at=time.monotonic(),
                    cooldown_seconds=cfg.volatility_cooldown_seconds,
                    reason=f"volatility={self._current_volatility:.1f} >= {cfg.volatility_threshold:.1f}",
                )
                self._active_trips[BreakerType.VOLATILITY] = trip
                logger.warning(
                    "circuit_breaker_tripped type=volatility level=%.1f threshold=%.1f cooldown=%ds",
                    self._current_volatility,
                    cfg.volatility_threshold,
                    int(cfg.volatility_cooldown_seconds),
                )

    def _expire_trips(self) -> None:
        now = time.monotonic()
        expired = [bt for bt, trip in self._active_trips.items() if trip.is_expired(now)]
        for bt in expired:
            logger.info("circuit_breaker_recovered type=%s", bt.value)
            del self._active_trips[bt]
        if expired:
            self._persist_state_locked()

    def _hydrate_state_from_store(self) -> None:
        if self._state_store is None:
            return
        try:
            payload = self._state_store.load_state()
        except Exception as exc:
            logger.warning(
                "circuit_breaker_state_hydrate_failed error=%s",
                exc,
            )
            return
        if not payload:
            return
        now_mono = time.monotonic()
        now_epoch = time.time()
        try:
            with self._lock:
                self._active_trips.clear()
                self._order_results.clear()
                self._consecutive_losses = int(payload.get("consecutive_losses") or 0)
                self._current_volatility = float(
                    payload.get("current_volatility") or 0.0
                )
                for item in payload.get("order_results") or []:
                    observed_at_epoch = float(item.get("observed_at_epoch") or 0.0)
                    if observed_at_epoch <= 0.0:
                        continue
                    age_seconds = max(0.0, now_epoch - observed_at_epoch)
                    self._order_results.append(
                        (now_mono - age_seconds, bool(item.get("is_reject", False)))
                    )
                for item in payload.get("active_trips") or []:
                    breaker_type_raw = str(item.get("breaker_type") or "").strip()
                    if not breaker_type_raw:
                        continue
                    expires_at_epoch = float(item.get("expires_at_epoch") or 0.0)
                    remaining_seconds = max(0.0, expires_at_epoch - now_epoch)
                    if remaining_seconds <= 0.0:
                        continue
                    breaker_type = BreakerType(breaker_type_raw)
                    self._active_trips[breaker_type] = BreakerTrip(
                        breaker_type=breaker_type,
                        tripped_at=now_mono,
                        cooldown_seconds=remaining_seconds,
                        reason=str(item.get("reason") or ""),
                    )
            logger.info(
                "circuit_breaker_state_hydrated active_trips=%d consecutive_losses=%d order_results=%d",
                len(self._active_trips),
                self._consecutive_losses,
                len(self._order_results),
            )
        except Exception as exc:
            logger.warning(
                "circuit_breaker_state_hydrate_invalid error=%s",
                exc,
            )

    def _serialize_state_locked(self) -> dict:
        now_mono = time.monotonic()
        now_epoch = time.time()
        return {
            "version": 1,
            "consecutive_losses": int(self._consecutive_losses),
            "current_volatility": float(self._current_volatility or 0.0),
            "order_results": [
                {
                    "observed_at_epoch": now_epoch - max(0.0, now_mono - observed_at),
                    "is_reject": bool(is_reject),
                }
                for observed_at, is_reject in self._order_results
            ],
            "active_trips": [
                {
                    "breaker_type": breaker_type.value,
                    "reason": trip.reason,
                    "expires_at_epoch": now_epoch
                    + max(0.0, trip.recovers_at - now_mono),
                }
                for breaker_type, trip in self._active_trips.items()
            ],
        }

    def _persist_state_locked(self) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.save_state(self._serialize_state_locked())
        except Exception as exc:
            logger.warning(
                "circuit_breaker_state_persist_failed error=%s",
                exc,
            )

    def _clear_persisted_state_locked(self) -> None:
        if self._state_store is None:
            return
        try:
            self._state_store.clear_state()
        except Exception as exc:
            logger.warning(
                "circuit_breaker_state_clear_failed error=%s",
                exc,
            )


__all__ = [
    "BreakerType",
    "BreakerState",
    "BreakerTrip",
    "CircuitBreakerConfig",
    "CircuitBreakerStatus",
    "TradingCircuitBreaker",
]
