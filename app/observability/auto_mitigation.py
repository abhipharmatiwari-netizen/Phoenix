"""
OPS-6.5: Auto-mitigation actions â€” disable strategies on repeated faults.

Monitors fault counters and automatically disables strategies or accounts
when fault thresholds are exceeded. All mitigations are logged for audit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class MitigationAction(str, Enum):
    DISABLE_STRATEGY = "disable_strategy"
    DISABLE_ACCOUNT = "disable_account"
    TRIP_CIRCUIT_BREAKER = "trip_circuit_breaker"
    LOG_ONLY = "log_only"


class FaultCategory(str, Enum):
    ORDER_REJECTION = "order_rejection"
    BROKER_TIMEOUT = "broker_timeout"
    POSITION_SYNC_FAILURE = "position_sync_failure"
    TICK_DATA_GAP = "tick_data_gap"
    UNHANDLED_EXCEPTION = "unhandled_exception"


@dataclass
class MitigationRule:
    """Rule that triggers mitigation when fault threshold is exceeded."""

    name: str
    fault_category: FaultCategory
    action: MitigationAction
    threshold: int  # Number of faults within window
    window_seconds: float  # Time window for counting faults
    cooldown_seconds: float = 300.0  # Min time between mitigations
    scope_key: str = ""  # e.g. strategy_id, broker_account_id (empty = global)


@dataclass
class MitigationEvent:
    """Record of a mitigation action taken."""

    rule_name: str
    action: MitigationAction
    scope_key: str
    scope_value: str
    fault_count: int
    timestamp: float
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "action": self.action.value,
            "scope_key": self.scope_key,
            "scope_value": self.scope_value,
            "fault_count": self.fault_count,
            "timestamp": self.timestamp,
            "details": self.details,
        }


class FaultTracker:
    """Tracks faults per scope and evaluates mitigation rules."""

    def __init__(self, rules: Optional[list[MitigationRule]] = None) -> None:
        self._rules: list[MitigationRule] = list(rules or [])
        # faults[category][scope_value] = list of timestamps
        self._faults: dict[str, dict[str, list[float]]] = {}
        # Last mitigation time per rule+scope to enforce cooldown
        self._last_mitigation: dict[str, float] = {}
        # History of all mitigation events
        self._events: list[MitigationEvent] = []
        # Callbacks for executing mitigation actions
        self._action_handlers: dict[MitigationAction, Callable[..., None]] = {}
        self.enabled: bool = True

    def set_action_handler(
        self, action: MitigationAction, handler: Callable[..., None]
    ) -> None:
        """Register a callback for a mitigation action."""
        self._action_handlers[action] = handler

    def add_rule(self, rule: MitigationRule) -> None:
        self._rules.append(rule)

    @property
    def rules(self) -> list[MitigationRule]:
        return list(self._rules)

    def record_fault(
        self,
        category: FaultCategory,
        scope_value: str = "",
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> list[MitigationEvent]:
        """Record a fault and return any mitigation events triggered."""
        now = time.monotonic()
        cat_key = category.value

        if cat_key not in self._faults:
            self._faults[cat_key] = {}
        scope_faults = self._faults[cat_key]
        if scope_value not in scope_faults:
            scope_faults[scope_value] = []
        scope_faults[scope_value].append(now)

        if not self.enabled:
            return []

        triggered: list[MitigationEvent] = []
        for rule in self._rules:
            if rule.fault_category != category:
                continue
            # Check scope match
            rule_scope = scope_value if rule.scope_key else ""
            # Count faults within window
            cutoff = now - rule.window_seconds
            faults_in_scope = scope_faults.get(rule_scope, scope_faults.get(scope_value, []))
            recent = [t for t in faults_in_scope if t >= cutoff]
            # Prune old entries
            if rule_scope in scope_faults:
                scope_faults[rule_scope] = recent

            if len(recent) < rule.threshold:
                continue

            # Check cooldown
            cooldown_key = f"{rule.name}:{rule_scope}"
            last = self._last_mitigation.get(cooldown_key)
            if last is not None and now - last < rule.cooldown_seconds:
                continue

            # Fire mitigation
            event = MitigationEvent(
                rule_name=rule.name,
                action=rule.action,
                scope_key=rule.scope_key,
                scope_value=scope_value,
                fault_count=len(recent),
                timestamp=now,
                details=details or {},
            )
            self._events.append(event)
            self._last_mitigation[cooldown_key] = now

            logger.warning(
                "auto_mitigation rule=%s action=%s scope=%s:%s faults=%d",
                rule.name,
                rule.action.value,
                rule.scope_key,
                scope_value,
                len(recent),
            )

            # Execute handler if registered
            handler = self._action_handlers.get(rule.action)
            if handler is not None:
                try:
                    handler(event)
                except Exception as exc:
                    logger.error(
                        "Mitigation handler for %s failed: %s", rule.action.value, exc
                    )

            triggered.append(event)

        return triggered

    def get_events(self, limit: int = 50) -> list[MitigationEvent]:
        """Return recent mitigation events."""
        return list(reversed(self._events[-limit:]))

    def get_fault_counts(self) -> dict[str, dict[str, int]]:
        """Return current fault counts by category and scope."""
        result: dict[str, dict[str, int]] = {}
        for cat, scopes in self._faults.items():
            result[cat] = {scope: len(ts) for scope, ts in scopes.items()}
        return result

    def reset(self) -> None:
        """Clear all fault tracking state."""
        self._faults.clear()
        self._last_mitigation.clear()
        self._events.clear()

    def summary(self) -> dict[str, Any]:
        """Summary suitable for JSON response."""
        return {
            "enabled": self.enabled,
            "rule_count": len(self._rules),
            "total_events": len(self._events),
            "recent_events": [e.to_dict() for e in self.get_events(10)],
            "fault_counts": self.get_fault_counts(),
        }


# ---------------------------------------------------------------------------
# Default rules for Phoenix trading platform
# ---------------------------------------------------------------------------


def build_default_mitigation_rules() -> list[MitigationRule]:
    """Standard mitigation rules for production trading."""
    return [
        MitigationRule(
            name="strategy_rejection_storm",
            fault_category=FaultCategory.ORDER_REJECTION,
            action=MitigationAction.DISABLE_STRATEGY,
            threshold=5,
            window_seconds=300.0,
            cooldown_seconds=600.0,
            scope_key="strategy_id",
        ),
        MitigationRule(
            name="broker_timeout_breaker",
            fault_category=FaultCategory.BROKER_TIMEOUT,
            action=MitigationAction.TRIP_CIRCUIT_BREAKER,
            threshold=3,
            window_seconds=120.0,
            cooldown_seconds=300.0,
            scope_key="broker_account_id",
        ),
        MitigationRule(
            name="position_sync_breaker",
            fault_category=FaultCategory.POSITION_SYNC_FAILURE,
            action=MitigationAction.TRIP_CIRCUIT_BREAKER,
            threshold=3,
            window_seconds=300.0,
            cooldown_seconds=600.0,
            scope_key="broker_account_id",
        ),
        MitigationRule(
            name="unhandled_exception_disable",
            fault_category=FaultCategory.UNHANDLED_EXCEPTION,
            action=MitigationAction.DISABLE_STRATEGY,
            threshold=3,
            window_seconds=60.0,
            cooldown_seconds=600.0,
            scope_key="strategy_id",
        ),
    ]


# Singleton
_default_tracker: Optional[FaultTracker] = None


def get_fault_tracker() -> FaultTracker:
    """Get or create the default fault tracker with built-in rules."""
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = FaultTracker(build_default_mitigation_rules())
    return _default_tracker


def reset_fault_tracker() -> None:
    """Reset the global fault tracker (for testing)."""
    global _default_tracker
    _default_tracker = None


__all__ = [
    "MitigationAction",
    "FaultCategory",
    "MitigationRule",
    "MitigationEvent",
    "FaultTracker",
    "build_default_mitigation_rules",
    "get_fault_tracker",
    "reset_fault_tracker",
]
