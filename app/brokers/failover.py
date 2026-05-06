"""Venue/broker failover logic — PHX-EXEC-005.

Defines the failover policy for degraded broker dependencies:
  - Primary broker health tracking
  - Automatic failover to secondary/simulated broker when primary fails
  - Configurable failover behavior: pause, failover, or close-only mode
  - Recovery: automatic re-promotion of primary after health restored

States per broker:
  HEALTHY → DEGRADED → FAILED
  FAILED → RECOVERING → HEALTHY  (on successful health probe)

Failover modes:
  PAUSE          — halt new order submission until primary recovers
  FAILOVER       — route to secondary broker
  CLOSE_ONLY     — allow position-closing orders only (risk-reducing)
  NO_NEW_ORDERS  — block new entries, allow exits
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


class BrokerHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    RECOVERING = "RECOVERING"


class FailoverMode(str, Enum):
    PAUSE = "PAUSE"
    FAILOVER = "FAILOVER"
    CLOSE_ONLY = "CLOSE_ONLY"
    NO_NEW_ORDERS = "NO_NEW_ORDERS"


class FailoverError(Exception):
    """Raised when an order cannot be routed due to failover state."""

    def __init__(self, broker_id: str, mode: FailoverMode, message: str) -> None:
        self.broker_id = broker_id
        self.mode = mode
        super().__init__(f"[{broker_id}] Failover active ({mode.value}): {message}")


@dataclass
class BrokerHealthState:
    broker_id: str
    health: BrokerHealth = BrokerHealth.HEALTHY
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None
    degraded_at: Optional[float] = None
    failed_at: Optional[float] = None
    error_message: str = ""


@dataclass
class BrokerFailoverPolicy:
    broker_id: str
    failover_mode: FailoverMode = FailoverMode.PAUSE
    secondary_broker_id: Optional[str] = None
    # Thresholds
    degrade_after_failures: int = 3
    fail_after_failures: int = 5
    recover_after_successes: int = 3
    # Health probe interval seconds (external caller drives probes)
    probe_interval_seconds: float = 30.0


class BrokerFailoverManager:
    """Thread-safe broker failover manager."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._health: dict[str, BrokerHealthState] = {}
        self._policies: dict[str, BrokerFailoverPolicy] = {}

    def register(self, policy: BrokerFailoverPolicy) -> None:
        with self._lock:
            self._policies[policy.broker_id] = policy
            if policy.broker_id not in self._health:
                self._health[policy.broker_id] = BrokerHealthState(broker_id=policy.broker_id)

    def record_success(self, broker_id: str) -> BrokerHealth:
        with self._lock:
            state = self._health.setdefault(broker_id, BrokerHealthState(broker_id=broker_id))
            policy = self._policies.get(broker_id)
            state.consecutive_failures = 0
            state.consecutive_successes += 1
            state.last_success_at = time.monotonic()
            prev_health = state.health
            recover_after = policy.recover_after_successes if policy else 3
            if state.health in (BrokerHealth.FAILED, BrokerHealth.RECOVERING, BrokerHealth.DEGRADED):
                if state.consecutive_successes >= recover_after:
                    state.health = BrokerHealth.HEALTHY
                    state.degraded_at = None
                    state.failed_at = None
                else:
                    state.health = BrokerHealth.RECOVERING
            new_health = state.health

        if prev_health != new_health:
            emit_audit_event(
                actor="system",
                action="broker_health_changed",
                resource_type="broker",
                resource_id=broker_id,
                before={"health": prev_health.value},
                after={"health": new_health.value},
            )
            logger.info("broker_health_changed broker=%s %s→%s", broker_id, prev_health.value, new_health.value)
        return new_health

    def record_failure(self, broker_id: str, error: str = "") -> BrokerHealth:
        with self._lock:
            state = self._health.setdefault(broker_id, BrokerHealthState(broker_id=broker_id))
            policy = self._policies.get(broker_id)
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            state.last_failure_at = time.monotonic()
            state.error_message = error
            prev_health = state.health
            degrade_after = policy.degrade_after_failures if policy else 3
            fail_after = policy.fail_after_failures if policy else 5
            if state.consecutive_failures >= fail_after:
                if state.health != BrokerHealth.FAILED:
                    state.health = BrokerHealth.FAILED
                    state.failed_at = time.monotonic()
            elif state.consecutive_failures >= degrade_after:
                if state.health == BrokerHealth.HEALTHY:
                    state.health = BrokerHealth.DEGRADED
                    state.degraded_at = time.monotonic()
            new_health = state.health

        if prev_health != new_health:
            emit_audit_event(
                actor="system",
                action="broker_health_changed",
                resource_type="broker",
                resource_id=broker_id,
                before={"health": prev_health.value},
                after={"health": new_health.value, "error": error},
            )
            logger.warning("broker_health_changed broker=%s %s→%s error=%s", broker_id, prev_health.value, new_health.value, error)
        return new_health

    def get_health(self, broker_id: str) -> BrokerHealth:
        with self._lock:
            state = self._health.get(broker_id)
        return state.health if state else BrokerHealth.HEALTHY

    def check_order_allowed(
        self,
        broker_id: str,
        *,
        is_closing: bool = False,
    ) -> Optional[str]:
        """Check if order submission is allowed.

        Returns None if allowed, or the secondary broker_id to route to.
        Raises FailoverError if no routing is possible.
        """
        with self._lock:
            state = self._health.get(broker_id)
            policy = self._policies.get(broker_id)

        if state is None or state.health == BrokerHealth.HEALTHY:
            return None

        if state.health == BrokerHealth.RECOVERING:
            return None  # Allow orders during recovery

        if policy is None:
            # Default: pause
            raise FailoverError(broker_id, FailoverMode.PAUSE, "Broker unhealthy, no policy")

        mode = policy.failover_mode

        if mode == FailoverMode.PAUSE:
            raise FailoverError(broker_id, mode, f"Broker {broker_id} is {state.health.value}")

        if mode == FailoverMode.CLOSE_ONLY:
            if not is_closing:
                raise FailoverError(broker_id, mode, "Only closing orders allowed during failover")
            return None  # Allow on primary (close-only)

        if mode == FailoverMode.NO_NEW_ORDERS:
            if not is_closing:
                raise FailoverError(broker_id, mode, "No new orders during failover")
            return None

        if mode == FailoverMode.FAILOVER:
            if policy.secondary_broker_id:
                logger.warning(
                    "broker_failover_route broker=%s → secondary=%s",
                    broker_id, policy.secondary_broker_id,
                )
                return policy.secondary_broker_id
            raise FailoverError(broker_id, mode, "Failover mode but no secondary configured")

        raise FailoverError(broker_id, mode, f"Broker {broker_id} is {state.health.value}")

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                broker_id: {
                    "health": s.health.value,
                    "consecutive_failures": s.consecutive_failures,
                    "consecutive_successes": s.consecutive_successes,
                    "error_message": s.error_message,
                }
                for broker_id, s in self._health.items()
            }


# Module-level singleton
broker_failover = BrokerFailoverManager()


__all__ = [
    "BrokerFailoverManager",
    "BrokerFailoverPolicy",
    "BrokerHealth",
    "FailoverError",
    "FailoverMode",
    "broker_failover",
]
