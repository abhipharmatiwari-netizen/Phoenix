"""Degraded scope management per Architecture §13.1-13.3.

Tracks scopes (strategy/account/contract) that have entered DEGRADED state
and enforces entry/exit/recovery criteria.

Entry criteria (§13.1) — enters DEGRADED when:
- ATM remap fails or remains ambiguous
- Internal position state and broker evidence diverge
- Ownership cannot be derived for a live contract
- Lifecycle state stuck in UNKNOWN/RECONCILING
- Quote freshness insufficient for safe entry

In DEGRADED:
- Fresh entries blocked for affected scope
- Risk-reducing exits allowed if ownership/routing still safe
- Manual review and break-glass available

Exit criteria (§13.2) — restricts exits when:
- Cannot map exit to normalized OwnershipKey
- Exit would increase net exposure
- Broker route too ambiguous

Recovery criteria (§13.3) — leaves DEGRADED only when ALL:
- OwnershipKey derivation restored
- Broker evidence fresh and consistent
- Lifecycle state terminal or reconciled
- Position state OPEN/PARTIALLY_EXITED/FLAT
- Recovery durably recorded
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DegradedReason(str, Enum):
    ATM_REMAP_FAILED = "ATM_REMAP_FAILED"
    BROKER_EVIDENCE_DIVERGENCE = "BROKER_EVIDENCE_DIVERGENCE"
    OWNERSHIP_DERIVATION_FAILED = "OWNERSHIP_DERIVATION_FAILED"
    LIFECYCLE_STUCK = "LIFECYCLE_STUCK"
    QUOTE_FRESHNESS_INSUFFICIENT = "QUOTE_FRESHNESS_INSUFFICIENT"
    RECONCILIATION_TIMEOUT = "RECONCILIATION_TIMEOUT"
    MANUAL_DEGRADATION = "MANUAL_DEGRADATION"


@dataclass
class DegradedScope:
    scope_key: str  # ownership_key or account:contract composite
    reason: DegradedReason
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_checked_at: Optional[datetime] = None
    recovery_attempts: int = 0
    exit_restricted: bool = False  # True when even exits are blocked per §13.2
    exit_restriction_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class DegradedScopeManager:
    """Thread-safe manager for degraded scopes."""

    def __init__(self) -> None:
        self._scopes: dict[str, DegradedScope] = {}
        self._lock = threading.Lock()

    def enter_degraded(
        self,
        *,
        scope_key: str,
        reason: DegradedReason,
        metadata: Optional[dict[str, Any]] = None,
    ) -> DegradedScope:
        """Mark a scope as DEGRADED per §13.1 criteria."""
        with self._lock:
            existing = self._scopes.get(scope_key)
            if existing is not None:
                existing.last_checked_at = datetime.now(timezone.utc)
                logger.info(
                    "degraded_scope_already_tracked scope_key=%s reason=%s",
                    scope_key, reason.value,
                )
                return existing
            scope = DegradedScope(
                scope_key=scope_key,
                reason=reason,
                metadata=dict(metadata or {}),
            )
            self._scopes[scope_key] = scope
            logger.warning(
                "degraded_scope_entered scope_key=%s reason=%s",
                scope_key, reason.value,
            )
            return scope

    def restrict_exits(
        self,
        *,
        scope_key: str,
        reason: str,
    ) -> bool:
        """Mark exits restricted for a scope per §13.2."""
        with self._lock:
            scope = self._scopes.get(scope_key)
            if scope is None:
                return False
            scope.exit_restricted = True
            scope.exit_restriction_reason = reason
            logger.warning(
                "degraded_scope_exits_restricted scope_key=%s reason=%s",
                scope_key, reason,
            )
            return True

    def is_entry_blocked(self, scope_key: str) -> bool:
        """Check if fresh entries are blocked for this scope."""
        with self._lock:
            return scope_key in self._scopes

    def is_exit_restricted(self, scope_key: str) -> bool:
        """Check if exits are restricted per §13.2."""
        with self._lock:
            scope = self._scopes.get(scope_key)
            return scope is not None and scope.exit_restricted

    def try_recover(
        self,
        *,
        scope_key: str,
        ownership_key_valid: bool,
        broker_evidence_fresh: bool,
        lifecycle_resolved: bool,
        position_state_clean: bool,
        actor: str = "system",
        require_operator_approval: bool = False,
        operator_approved: bool = False,
    ) -> bool:
        """Attempt recovery per §13.3 criteria. Returns True if recovered.

        When ``require_operator_approval`` is True, automatic recovery is
        blocked unless ``operator_approved`` is also True.

        The recovery decision is durably recorded via ``_record_recovery_decision``
        for auditability (Architecture §13.3, M4).
        """
        with self._lock:
            scope = self._scopes.get(scope_key)
            if scope is None:
                return True  # Not degraded
            scope.recovery_attempts += 1
            scope.last_checked_at = datetime.now(timezone.utc)

            criteria = {
                "ownership_key_valid": ownership_key_valid,
                "broker_evidence_fresh": broker_evidence_fresh,
                "lifecycle_resolved": lifecycle_resolved,
                "position_state_clean": position_state_clean,
            }
            all_criteria_met = all(criteria.values())

            if require_operator_approval and not operator_approved:
                self._record_recovery_decision(
                    scope_key=scope_key,
                    recovered=False,
                    actor=actor,
                    reason="operator_approval_required",
                    criteria=criteria,
                    attempts=scope.recovery_attempts,
                )
                logger.info(
                    "degraded_scope_recovery_blocked scope_key=%s reason=operator_approval_required",
                    scope_key,
                )
                return False

            if all_criteria_met:
                attempts = scope.recovery_attempts
                del self._scopes[scope_key]
                self._record_recovery_decision(
                    scope_key=scope_key,
                    recovered=True,
                    actor=actor,
                    reason="all_criteria_met",
                    criteria=criteria,
                    attempts=attempts,
                )
                logger.info(
                    "degraded_scope_recovered scope_key=%s after_attempts=%d actor=%s",
                    scope_key, attempts, actor,
                )
                return True

            missing = [k for k, v in criteria.items() if not v]
            self._record_recovery_decision(
                scope_key=scope_key,
                recovered=False,
                actor=actor,
                reason=f"missing_criteria: {','.join(missing)}",
                criteria=criteria,
                attempts=scope.recovery_attempts,
            )
            logger.debug(
                "degraded_scope_recovery_failed scope_key=%s missing=%s attempts=%d",
                scope_key, ",".join(missing), scope.recovery_attempts,
            )
            return False

    def _record_recovery_decision(
        self,
        *,
        scope_key: str,
        recovered: bool,
        actor: str,
        reason: str,
        criteria: dict[str, bool],
        attempts: int,
    ) -> None:
        """Durably record recovery decision for audit trail (M4).

        Emits an audit event and logs the decision. In production,
        this should also persist to Postgres for queryability.
        """
        try:
            from app.core.audit_log import emit_audit_event
            emit_audit_event(
                actor=actor,
                action="degraded_recovery_decision",
                resource_type="degraded_scope",
                resource_id=scope_key,
                after={
                    "recovered": recovered,
                    "reason": reason,
                    "criteria": criteria,
                    "recovery_attempts": attempts,
                },
            )
        except Exception:
            logger.exception(
                "Failed to emit audit event for degraded recovery decision "
                "scope_key=%s recovered=%s",
                scope_key, recovered,
            )

    def active_scopes(self) -> list[DegradedScope]:
        """Return all currently degraded scopes."""
        with self._lock:
            return list(self._scopes.values())

    def scope_count(self) -> int:
        with self._lock:
            return len(self._scopes)

    def status_snapshot(self) -> dict[str, Any]:
        """Return a dashboard-friendly snapshot."""
        with self._lock:
            return {
                "degraded_count": len(self._scopes),
                "scopes": {
                    k: {
                        "reason": v.reason.value,
                        "entered_at": v.entered_at.isoformat(),
                        "exit_restricted": v.exit_restricted,
                        "recovery_attempts": v.recovery_attempts,
                    }
                    for k, v in self._scopes.items()
                },
            }


# Module-level singleton
degraded_scope_manager = DegradedScopeManager()
