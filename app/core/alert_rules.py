"""Alert rule evaluators for Architecture S15.2 observability.

Missing alert rules identified in H5:
- Ownership conflict spike detection (scope_serializer contention)
- Deadletter/dead-letter queue growth monitoring
- Leader lease renewal failure alert
- Orders stuck in SUBMITTING state beyond configurable threshold
"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class AlertEvent:
    rule_name: str
    severity: AlertSeverity
    message: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class AlertRule:
    """Base class for alert rules."""

    def __init__(self, name: str, *, enabled: bool = True) -> None:
        self.name = name
        self.enabled = enabled
        self._last_fired_at: Optional[float] = None
        self._cooldown_seconds: float = 60.0

    def evaluate(self) -> Optional[AlertEvent]:
        raise NotImplementedError

    def _should_fire(self) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        if self._last_fired_at and (now - self._last_fired_at) < self._cooldown_seconds:
            return False
        return True

    def _mark_fired(self) -> None:
        self._last_fired_at = time.monotonic()


class OwnershipConflictSpikeRule(AlertRule):
    """Detect ownership conflict spikes from scope_serializer contention."""

    def __init__(
        self,
        *,
        pending_count_fn: Optional[Callable[[], dict[str, int]]] = None,
        threshold: int = 10,
    ) -> None:
        super().__init__("ownership_conflict_spike")
        self._pending_count_fn = pending_count_fn
        self._threshold = int(
            os.environ.get("ALERT_OWNERSHIP_CONFLICT_THRESHOLD", str(threshold))
        )

    def evaluate(self) -> Optional[AlertEvent]:
        if not self._should_fire() or self._pending_count_fn is None:
            return None
        pending = self._pending_count_fn()
        total_pending = sum(pending.values())
        if total_pending >= self._threshold:
            self._mark_fired()
            return AlertEvent(
                rule_name=self.name,
                severity=AlertSeverity.WARNING,
                message=(
                    f"Ownership conflict spike: {total_pending} pending mutations "
                    f"across {len(pending)} scopes (threshold={self._threshold})"
                ),
                metadata={"total_pending": total_pending, "scope_count": len(pending)},
            )
        return None


class DeadletterGrowthRule(AlertRule):
    """Monitor deadletter/dead-letter queue growth."""

    def __init__(
        self,
        *,
        deadletter_count_fn: Optional[Callable[[], int]] = None,
        threshold: int = 5,
    ) -> None:
        super().__init__("deadletter_growth")
        self._deadletter_count_fn = deadletter_count_fn
        self._threshold = int(
            os.environ.get("ALERT_DEADLETTER_THRESHOLD", str(threshold))
        )
        self._last_count = 0

    def evaluate(self) -> Optional[AlertEvent]:
        if not self._should_fire() or self._deadletter_count_fn is None:
            return None
        count = self._deadletter_count_fn()
        if count > self._last_count and count >= self._threshold:
            growth = count - self._last_count
            self._last_count = count
            self._mark_fired()
            return AlertEvent(
                rule_name=self.name,
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"Deadletter queue growth: {count} items "
                    f"(+{growth}, threshold={self._threshold})"
                ),
                metadata={"count": count, "growth": growth},
            )
        self._last_count = count
        return None


class LeaseRenewalFailureRule(AlertRule):
    """Alert on leader lease renewal failure."""

    def __init__(
        self,
        *,
        lease_healthy_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__("lease_renewal_failure")
        self._lease_healthy_fn = lease_healthy_fn
        self._consecutive_failures = 0

    def evaluate(self) -> Optional[AlertEvent]:
        if not self._should_fire() or self._lease_healthy_fn is None:
            return None
        healthy = self._lease_healthy_fn()
        if not healthy:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 2:
                self._mark_fired()
                return AlertEvent(
                    rule_name=self.name,
                    severity=AlertSeverity.CRITICAL,
                    message=(
                        f"Leader lease renewal failure: "
                        f"{self._consecutive_failures} consecutive failures"
                    ),
                    metadata={"consecutive_failures": self._consecutive_failures},
                )
        else:
            self._consecutive_failures = 0
        return None


class StuckOrdersRule(AlertRule):
    """Detect orders stuck in SUBMITTING state beyond threshold."""

    def __init__(
        self,
        *,
        stuck_orders_fn: Optional[Callable[[], list[dict[str, Any]]]] = None,
        threshold_seconds: float = 120.0,
    ) -> None:
        super().__init__("stuck_orders")
        self._stuck_orders_fn = stuck_orders_fn
        self._threshold_seconds = float(
            os.environ.get("ALERT_STUCK_ORDER_THRESHOLD_SECONDS", str(threshold_seconds))
        )

    def evaluate(self) -> Optional[AlertEvent]:
        if not self._should_fire() or self._stuck_orders_fn is None:
            return None
        stuck = self._stuck_orders_fn()
        if stuck:
            self._mark_fired()
            return AlertEvent(
                rule_name=self.name,
                severity=AlertSeverity.WARNING,
                message=(
                    f"Orders stuck in SUBMITTING: {len(stuck)} orders "
                    f"beyond {self._threshold_seconds}s threshold"
                ),
                metadata={"stuck_count": len(stuck), "orders": stuck[:10]},
            )
        return None


class AlertEvaluator:
    """Evaluate all registered alert rules and dispatch events."""

    def __init__(self) -> None:
        self._rules: list[AlertRule] = []
        self._listeners: list[Callable[[AlertEvent], None]] = []
        self._lock = threading.Lock()

    def register_rule(self, rule: AlertRule) -> None:
        with self._lock:
            self._rules.append(rule)

    def register_listener(self, listener: Callable[[AlertEvent], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def evaluate_all(self) -> list[AlertEvent]:
        """Evaluate all rules and dispatch fired alerts. Returns fired alerts."""
        fired: list[AlertEvent] = []
        with self._lock:
            rules = list(self._rules)
            listeners = list(self._listeners)

        for rule in rules:
            try:
                event = rule.evaluate()
                if event is not None:
                    fired.append(event)
                    logger.warning(
                        "ALERT [%s] severity=%s: %s",
                        event.rule_name, event.severity.value, event.message,
                    )
                    for listener in listeners:
                        try:
                            listener(event)
                        except Exception:
                            logger.exception("Alert listener error for %s", event.rule_name)
            except Exception:
                logger.exception("Alert rule evaluation error: %s", rule.name)
        return fired

    def rule_count(self) -> int:
        with self._lock:
            return len(self._rules)


def build_default_alert_evaluator(
    *,
    pending_count_fn: Optional[Callable[[], dict[str, int]]] = None,
    deadletter_count_fn: Optional[Callable[[], int]] = None,
    lease_healthy_fn: Optional[Callable[[], bool]] = None,
    stuck_orders_fn: Optional[Callable[[], list[dict[str, Any]]]] = None,
) -> AlertEvaluator:
    """Build an AlertEvaluator with the default set of rules."""
    evaluator = AlertEvaluator()
    evaluator.register_rule(
        OwnershipConflictSpikeRule(pending_count_fn=pending_count_fn)
    )
    evaluator.register_rule(
        DeadletterGrowthRule(deadletter_count_fn=deadletter_count_fn)
    )
    evaluator.register_rule(
        LeaseRenewalFailureRule(lease_healthy_fn=lease_healthy_fn)
    )
    evaluator.register_rule(
        StuckOrdersRule(stuck_orders_fn=stuck_orders_fn)
    )
    return evaluator


__all__ = [
    "AlertEvaluator",
    "AlertEvent",
    "AlertRule",
    "AlertSeverity",
    "DeadletterGrowthRule",
    "LeaseRenewalFailureRule",
    "OwnershipConflictSpikeRule",
    "StuckOrdersRule",
    "build_default_alert_evaluator",
]
