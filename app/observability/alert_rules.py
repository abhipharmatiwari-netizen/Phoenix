"""
OBS-6.3: Alert rules and threshold evaluation for top failure modes.

Defines alert thresholds for trading KPIs and evaluates them against
the current metrics snapshot. Designed to be polled by a background task
or triggered from /health/alerts endpoint.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(str, Enum):
    OK = "ok"
    FIRING = "firing"
    RESOLVED = "resolved"


@dataclass
class AlertRule:
    """A single alert rule definition."""

    name: str
    description: str
    severity: AlertSeverity
    # Callable that returns (is_firing: bool, current_value: Any, message: str)
    evaluate_fn: Callable[[], tuple[bool, Any, str]]
    # Minimum seconds an alert must fire before it is emitted (debounce)
    for_seconds: float = 0.0
    # Labels attached to all firings of this rule
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class AlertStatus:
    """Current status of one alert rule."""

    rule_name: str
    state: AlertState
    severity: AlertSeverity
    value: Any = None
    message: str = ""
    fired_at: Optional[float] = None
    resolved_at: Optional[float] = None
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "state": self.state.value,
            "severity": self.severity.value,
            "value": self.value,
            "message": self.message,
            "fired_at": self.fired_at,
            "resolved_at": self.resolved_at,
            "labels": self.labels,
        }


class AlertEvaluator:
    """Evaluates a set of alert rules and tracks their state."""

    def __init__(self, rules: Optional[list[AlertRule]] = None) -> None:
        self._rules: list[AlertRule] = list(rules or [])
        # Tracks when a rule first started firing (for for_seconds debounce)
        self._pending_since: dict[str, float] = {}
        # Current status of each rule
        self._statuses: dict[str, AlertStatus] = {}

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)

    @property
    def rules(self) -> list[AlertRule]:
        return list(self._rules)

    def evaluate_all(self) -> list[AlertStatus]:
        """Evaluate all rules and return current statuses."""
        now = time.monotonic()
        results: list[AlertStatus] = []

        for rule in self._rules:
            try:
                is_firing, value, message = rule.evaluate_fn()
            except Exception as exc:
                logger.warning("Alert rule %s evaluation failed: %s", rule.name, exc)
                is_firing, value, message = False, None, f"evaluation_error: {exc}"

            prev = self._statuses.get(rule.name)

            if is_firing:
                if rule.name not in self._pending_since:
                    self._pending_since[rule.name] = now

                elapsed = now - self._pending_since[rule.name]
                if elapsed >= rule.for_seconds:
                    status = AlertStatus(
                        rule_name=rule.name,
                        state=AlertState.FIRING,
                        severity=rule.severity,
                        value=value,
                        message=message,
                        fired_at=self._pending_since[rule.name],
                        labels=rule.labels,
                    )
                else:
                    # Still in debounce period — keep previous state or OK
                    status = prev or AlertStatus(
                        rule_name=rule.name,
                        state=AlertState.OK,
                        severity=rule.severity,
                        labels=rule.labels,
                    )
            else:
                self._pending_since.pop(rule.name, None)
                if prev and prev.state == AlertState.FIRING:
                    status = AlertStatus(
                        rule_name=rule.name,
                        state=AlertState.RESOLVED,
                        severity=rule.severity,
                        value=value,
                        message="resolved",
                        resolved_at=now,
                        labels=rule.labels,
                    )
                else:
                    status = AlertStatus(
                        rule_name=rule.name,
                        state=AlertState.OK,
                        severity=rule.severity,
                        labels=rule.labels,
                    )

            self._statuses[rule.name] = status
            results.append(status)

        return results

    def get_firing_alerts(self) -> list[AlertStatus]:
        """Return only currently firing alerts."""
        return [s for s in self._statuses.values() if s.state == AlertState.FIRING]

    def get_all_statuses(self) -> list[AlertStatus]:
        return list(self._statuses.values())

    def summary(self) -> dict[str, Any]:
        """Summary suitable for JSON response."""
        statuses = self.evaluate_all()
        firing = [s for s in statuses if s.state == AlertState.FIRING]
        return {
            "total_rules": len(self._rules),
            "firing_count": len(firing),
            "alerts": [s.to_dict() for s in statuses],
        }


# ---------------------------------------------------------------------------
# Built-in alert rules for Phoenix trading platform
# ---------------------------------------------------------------------------


def build_default_alert_rules() -> list[AlertRule]:
    """Create the default set of trading platform alert rules."""
    rules: list[AlertRule] = []

    # 1. Circuit breaker tripped
    def _check_circuit_breaker_trips() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_circuit_breaker_trips_total")
        if not entry:
            return False, 0, ""
        total = sum(v for _, v in entry.samples)
        if total > 0:
            return True, total, f"Circuit breaker tripped {int(total)} time(s)"
        return False, 0, ""

    rules.append(AlertRule(
        name="circuit_breaker_tripped",
        description="One or more circuit breakers have tripped",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_circuit_breaker_trips,
        labels={"component": "risk"},
    ))

    # 2. High order rejection rate
    def _check_order_rejections() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_orders_total")
        if not entry:
            return False, 0.0, ""
        total = sum(v for _, v in entry.samples)
        rejected = sum(v for lbl, v in entry.samples if lbl.get("status") == "rejected")
        if total < 5:
            return False, 0.0, "insufficient_samples"
        rate = rejected / total
        if rate > 0.3:
            return True, round(rate, 3), f"Order rejection rate {rate:.1%} exceeds 30%"
        return False, round(rate, 3), ""

    rules.append(AlertRule(
        name="high_order_rejection_rate",
        description="Order rejection rate exceeds 30%",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_order_rejections,
        for_seconds=0,
        labels={"component": "orders"},
    ))

    # 3. Position sync failures
    def _check_position_sync_failures() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_position_syncs_total")
        if not entry:
            return False, 0, ""
        failures = sum(v for lbl, v in entry.samples if lbl.get("success") == "false")
        if failures >= 3:
            return True, int(failures), f"{int(failures)} position sync failures"
        return False, int(failures), ""

    rules.append(AlertRule(
        name="position_sync_failures",
        description="Multiple broker position sync failures detected",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_position_sync_failures,
        labels={"component": "broker"},
    ))

    # 4. Tick data gaps
    def _check_tick_gaps() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_tick_gaps_total")
        if not entry:
            return False, 0, ""
        total_gaps = sum(v for _, v in entry.samples)
        if total_gaps >= 5:
            return True, int(total_gaps), f"{int(total_gaps)} tick data gaps detected"
        return False, int(total_gaps), ""

    rules.append(AlertRule(
        name="tick_data_gaps",
        description="Market data feed gaps detected",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_tick_gaps,
        labels={"component": "market_data"},
    ))

    # 5. Negative PnL threshold
    def _check_pnl_drawdown() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_realized_pnl")
        if not entry:
            return False, 0.0, ""
        min_pnl = min((v for _, v in entry.samples), default=0.0)
        if min_pnl < -50000:
            return True, min_pnl, f"Realized PnL {min_pnl:,.0f} below -50,000 threshold"
        return False, min_pnl, ""

    rules.append(AlertRule(
        name="pnl_drawdown_critical",
        description="Realized PnL drawdown exceeds critical threshold",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_pnl_drawdown,
        labels={"component": "pnl"},
    ))

    # 6. Stale quote age breach
    def _check_stale_quote_age() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_quote_age_seconds")
        if not entry:
            return False, 0.0, ""
        max_age = max((v for _, v in entry.samples), default=0.0)
        if max_age > 120:
            return True, max_age, f"Quote age {max_age:.1f}s exceeds 120s threshold"
        return False, max_age, ""

    rules.append(AlertRule(
        name="stale_quote_age",
        description="Market quote age exceeds staleness threshold",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_stale_quote_age,
        labels={"component": "market_data"},
    ))

    # 7. Stale broker sync age
    def _check_stale_broker_sync() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_broker_sync_age_seconds")
        if not entry:
            return False, 0.0, ""
        max_age = max((v for _, v in entry.samples), default=0.0)
        if max_age > 180:
            return True, max_age, f"Broker sync age {max_age:.1f}s exceeds 180s threshold"
        return False, max_age, ""

    rules.append(AlertRule(
        name="stale_broker_sync",
        description="Broker position/order sync age exceeds staleness threshold",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_stale_broker_sync,
        labels={"component": "broker"},
    ))

    # 8. Stuck orders
    def _check_stuck_orders() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_stuck_orders_count")
        if not entry:
            return False, 0, ""
        count = sum(v for _, v in entry.samples)
        if count > 0:
            return True, int(count), f"{int(count)} order(s) stuck in transient state"
        return False, 0, ""

    rules.append(AlertRule(
        name="stuck_orders",
        description="Orders stuck in SUBMITTING, OPEN, PARTIAL_FILL, or RECONCILING",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_stuck_orders,
        labels={"component": "orders"},
    ))

    # 9. Reconciliation backlog
    def _check_reconciliation_backlog() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_reconciliation_backlog_count")
        if not entry:
            return False, 0, ""
        count = sum(v for _, v in entry.samples)
        if count > 5:
            return True, int(count), f"Reconciliation backlog at {int(count)} items"
        return False, int(count), ""

    rules.append(AlertRule(
        name="reconciliation_backlog",
        description="Reconciliation or orphan-review backlog growing",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_reconciliation_backlog,
        labels={"component": "reconciliation"},
    ))

    # 10. Outbox replay backlog
    def _check_outbox_replay_backlog() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_outbox_pending_count")
        if not entry:
            return False, 0, ""
        count = sum(v for _, v in entry.samples)
        if count > 10:
            return True, int(count), f"Outbox replay backlog at {int(count)} pending"
        return False, int(count), ""

    rules.append(AlertRule(
        name="outbox_replay_backlog",
        description="Outbox replay backlog after restart exceeds threshold",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_outbox_replay_backlog,
        labels={"component": "outbox"},
    ))

    # 11. Ownership conflict spikes
    def _check_ownership_conflicts() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_ownership_conflicts_total")
        if not entry:
            return False, 0, ""
        total = sum(v for _, v in entry.samples)
        if total > 3:
            return True, int(total), f"Ownership conflicts spiked to {int(total)}"
        return False, int(total), ""

    rules.append(AlertRule(
        name="ownership_conflicts",
        description="Position ownership conflict spike detected",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_ownership_conflicts,
        labels={"component": "ownership"},
    ))

    # 12. Deadletter growth
    def _check_deadletter_growth() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_deadletter_count")
        if not entry:
            return False, 0, ""
        count = sum(v for _, v in entry.samples)
        if count > 0:
            return True, int(count), f"Deadletter queue has {int(count)} message(s)"
        return False, 0, ""

    rules.append(AlertRule(
        name="deadletter_growth",
        description="Deadletter queue is non-empty",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_deadletter_growth,
        labels={"component": "messaging"},
    ))

    # 13. Dashboard freshness lag
    def _check_dashboard_freshness_lag() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_dashboard_lag_seconds")
        if not entry:
            return False, 0.0, ""
        max_lag = max((v for _, v in entry.samples), default=0.0)
        if max_lag > 10:
            return True, max_lag, f"Dashboard freshness lag {max_lag:.1f}s exceeds 10s"
        return False, max_lag, ""

    rules.append(AlertRule(
        name="dashboard_freshness_lag",
        description="Dashboard data freshness lag while runtime is healthy",
        severity=AlertSeverity.INFO,
        evaluate_fn=_check_dashboard_freshness_lag,
        labels={"component": "dashboard"},
    ))

    # 14. Leader lease renewal failure
    def _check_leader_lease_failure() -> tuple[bool, Any, str]:
        from app.observability.prometheus_metrics import _METRICS_STORE

        entry = _METRICS_STORE.get("phoenix_lease_renewal_failures_total")
        if not entry:
            return False, 0, ""
        total = sum(v for _, v in entry.samples)
        if total > 0:
            return True, int(total), f"Leader lease renewal failed {int(total)} time(s)"
        return False, 0, ""

    rules.append(AlertRule(
        name="leader_lease_failure",
        description="Leader lease renewal failure or fencing event",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_leader_lease_failure,
        labels={"component": "leader_election"},
    ))

    # 15. Readiness-critical position authority blockers.
    def _check_position_authority_degraded() -> tuple[bool, Any, str]:
        if str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() != "LIVE":
            return False, 0, ""
        degraded_scopes = 0
        try:
            from app.core.degraded_scope_manager import degraded_scope_manager
            degraded_scopes = len(degraded_scope_manager.active_scopes())
        except Exception:
            degraded_scopes = 0

        degraded_positions = 0
        reconciling_positions = 0
        try:
            from app.hub.runtime import get_hub_runtime
            runtime = get_hub_runtime()
            order_lifecycle = getattr(runtime, "order_lifecycle", None)
            count_fn = getattr(order_lifecycle, "count_positions_by_state", None)
            if callable(count_fn):
                counts = count_fn() or {}
                degraded_positions = int(counts.get("DEGRADED", 0) or 0)
                reconciling_positions = int(counts.get("RECONCILING", 0) or 0)
        except Exception:
            degraded_positions = 0
            reconciling_positions = 0

        blockers = degraded_scopes + degraded_positions + reconciling_positions
        if blockers > 0:
            return (
                True,
                blockers,
                (
                    "Readiness blocked by position authority state: "
                    f"degraded_scopes={degraded_scopes} "
                    f"degraded_positions={degraded_positions} "
                    f"reconciling_positions={reconciling_positions}"
                ),
            )
        return False, 0, ""

    rules.append(AlertRule(
        name="readiness_position_authority_degraded",
        description="LIVE readiness is blocked by degraded/reconciling position authority",
        severity=AlertSeverity.CRITICAL,
        evaluate_fn=_check_position_authority_degraded,
        labels={"component": "readiness"},
    ))

    # 16. OI/ML shadow ingestion visibility.
    def _check_oi_ml_shadow_ingestion() -> tuple[bool, Any, str]:
        try:
            from app.strategies.oi_ml.shadow_health import collect_shadow_ingestion_status

            status = collect_shadow_ingestion_status()
        except Exception as exc:
            return False, 0, f"evaluation_error:{type(exc).__name__}"
        if not status.get("enabled"):
            return False, 0, ""
        state = str(status.get("status") or "").strip().lower()
        if state not in {"degraded", "unknown"}:
            return False, 0, ""
        option_chain = status.get("option_chain") if isinstance(status.get("option_chain"), dict) else {}
        row_count = int(option_chain.get("today_row_count") or 0)
        reason = str(status.get("reason") or state)
        return (
            True,
            row_count,
            (
                "OI/ML shadow ingestion degraded: "
                f"reason={reason} today_option_rows={row_count}"
            ),
        )

    rules.append(AlertRule(
        name="oi_ml_shadow_ingestion_degraded",
        description="OI/ML shadow sidecar is enabled but ingestion evidence is missing or stale",
        severity=AlertSeverity.WARNING,
        evaluate_fn=_check_oi_ml_shadow_ingestion,
        labels={"component": "oi_ml_shadow"},
    ))

    return rules


# Singleton evaluator
_default_evaluator: Optional[AlertEvaluator] = None


def get_alert_evaluator() -> AlertEvaluator:
    """Get or create the default alert evaluator with built-in rules."""
    global _default_evaluator
    if _default_evaluator is None:
        _default_evaluator = AlertEvaluator(build_default_alert_rules())
    return _default_evaluator


def reset_alert_evaluator() -> None:
    """Reset the global evaluator (for testing)."""
    global _default_evaluator
    _default_evaluator = None


__all__ = [
    "AlertSeverity",
    "AlertState",
    "AlertRule",
    "AlertStatus",
    "AlertEvaluator",
    "build_default_alert_rules",
    "get_alert_evaluator",
    "reset_alert_evaluator",
]
