"""Reconciliation hardening — PHX-RISK-006.

Position/cash/order reconciliations run on schedule; mismatches are
classified by severity; critical mismatches automatically freeze or
throttle trading for the affected account/strategy scope.

Mismatch severities:
  - INFO: within noise threshold (normal rounding)
  - WARNING: non-trivial gap; alert operator
  - CRITICAL: breaches absolute or relative threshold; auto-freeze

Auto-freeze:
  When a critical mismatch is detected the engine calls into the
  kill-switch and/or degraded-scope manager for the affected scope,
  emits an audit event, and raises ReconciliationFreezeError so that
  the caller can propagate the halt.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


class MismatchSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ReconciliationFreezeError(Exception):
    """Raised when a critical reconciliation mismatch triggers auto-freeze."""

    def __init__(self, scope: str, mismatch_type: str, gap: float) -> None:
        self.scope = scope
        self.mismatch_type = mismatch_type
        self.gap = gap
        super().__init__(
            f"Critical reconciliation mismatch for scope={scope} type={mismatch_type} gap={gap}"
        )


@dataclass
class ReconciliationThresholds:
    """Thresholds for a particular mismatch type."""
    mismatch_type: str
    warn_abs: float = 0.0         # absolute gap above which → WARNING
    critical_abs: float = 0.0     # absolute gap above which → CRITICAL
    warn_pct: float = 0.0         # percentage gap → WARNING (0.0 = disabled)
    critical_pct: float = 0.0     # percentage gap → CRITICAL


@dataclass
class MismatchRecord:
    scope: str                    # e.g. account_id or "tenant:account:strategy"
    mismatch_type: str            # "position_qty", "pnl", "order_count"
    expected: Any
    actual: Any
    gap: float
    severity: MismatchSeverity
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    resolved: bool = False
    resolved_at: Optional[str] = None


_DEFAULT_THRESHOLDS: dict[str, ReconciliationThresholds] = {
    "position_qty": ReconciliationThresholds(
        "position_qty", warn_abs=1, critical_abs=5, warn_pct=0.05, critical_pct=0.20
    ),
    "pnl": ReconciliationThresholds(
        "pnl", warn_abs=500, critical_abs=5000, warn_pct=0.02, critical_pct=0.10
    ),
    "order_count": ReconciliationThresholds(
        "order_count", warn_abs=2, critical_abs=10
    ),
    "cash_balance": ReconciliationThresholds(
        "cash_balance", warn_abs=1000, critical_abs=10000, warn_pct=0.01, critical_pct=0.05
    ),
}


class ReconciliationPolicy:
    """Evaluates reconciliation results and enforces freeze policy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thresholds: dict[str, ReconciliationThresholds] = dict(_DEFAULT_THRESHOLDS)
        self._history: list[MismatchRecord] = []
        self._frozen_scopes: set[str] = set()

    def set_threshold(self, threshold: ReconciliationThresholds) -> None:
        with self._lock:
            self._thresholds[threshold.mismatch_type] = threshold

    def is_frozen(self, scope: str) -> bool:
        with self._lock:
            return scope in self._frozen_scopes

    def unfreeze(self, scope: str, actor: str = "operator") -> None:
        with self._lock:
            self._frozen_scopes.discard(scope)
        emit_audit_event(
            actor=actor,
            action="reconciliation_unfreeze",
            resource_type="reconciliation",
            resource_id=scope,
        )
        logger.warning("reconciliation_unfreeze scope=%s actor=%s", scope, actor)

    def evaluate(
        self,
        *,
        scope: str,
        mismatch_type: str,
        expected: Any,
        actual: Any,
        reference_value: float = 0.0,
    ) -> MismatchRecord:
        """Evaluate a reconciliation result.

        Classifies severity and auto-freezes on CRITICAL.
        Raises ReconciliationFreezeError for CRITICAL mismatches.
        """
        gap = abs(float(actual or 0) - float(expected or 0))
        with self._lock:
            thresh = self._thresholds.get(mismatch_type)

        severity = self._classify(gap, thresh, reference_value)

        rec = MismatchRecord(
            scope=scope,
            mismatch_type=mismatch_type,
            expected=expected,
            actual=actual,
            gap=gap,
            severity=severity,
        )

        with self._lock:
            self._history.append(rec)
            if len(self._history) > 5000:
                self._history = self._history[-4000:]

        if severity == MismatchSeverity.INFO:
            logger.debug(
                "recon_info scope=%s type=%s gap=%s", scope, mismatch_type, gap
            )
            return rec

        emit_audit_event(
            actor="system",
            action=f"reconciliation_mismatch_{severity.value.lower()}",
            resource_type="reconciliation",
            resource_id=scope,
            after={
                "mismatch_type": mismatch_type,
                "expected": expected,
                "actual": actual,
                "gap": gap,
                "severity": severity.value,
            },
        )

        if severity == MismatchSeverity.WARNING:
            logger.warning(
                "recon_warning scope=%s type=%s expected=%s actual=%s gap=%s",
                scope, mismatch_type, expected, actual, gap,
            )
            return rec

        # CRITICAL — auto-freeze
        with self._lock:
            self._frozen_scopes.add(scope)

        logger.error(
            "recon_critical_freeze scope=%s type=%s expected=%s actual=%s gap=%s",
            scope, mismatch_type, expected, actual, gap,
        )

        # Attempt to enter degraded scope
        try:
            from app.core.degraded_scope_manager import (
                DegradedReason,
                degraded_scope_manager,
            )
            degraded_scope_manager.enter_degraded(
                scope_key=scope,
                reason=DegradedReason.BROKER_EVIDENCE_DIVERGENCE,
                metadata={"mismatch_type": mismatch_type, "gap": gap},
            )
        except Exception:
            logger.debug("Could not enter degraded scope", exc_info=True)

        raise ReconciliationFreezeError(scope, mismatch_type, gap)

    @staticmethod
    def _classify(
        gap: float,
        thresh: Optional[ReconciliationThresholds],
        reference: float,
    ) -> MismatchSeverity:
        if thresh is None:
            return MismatchSeverity.INFO if gap == 0 else MismatchSeverity.WARNING
        # Check absolute
        if thresh.critical_abs > 0 and gap >= thresh.critical_abs:
            return MismatchSeverity.CRITICAL
        # Check percentage
        if reference > 0 and thresh.critical_pct > 0:
            pct = gap / reference
            if pct >= thresh.critical_pct:
                return MismatchSeverity.CRITICAL
        if thresh.warn_abs > 0 and gap >= thresh.warn_abs:
            return MismatchSeverity.WARNING
        if reference > 0 and thresh.warn_pct > 0 and gap / reference >= thresh.warn_pct:
            return MismatchSeverity.WARNING
        return MismatchSeverity.INFO

    def history(
        self,
        *,
        scope: Optional[str] = None,
        severity: Optional[MismatchSeverity] = None,
        limit: int = 100,
    ) -> list[dict]:
        with self._lock:
            records = list(self._history)
        filtered = [
            r for r in reversed(records)
            if (scope is None or r.scope == scope)
            and (severity is None or r.severity == severity)
        ]
        return [
            {
                "scope": r.scope,
                "mismatch_type": r.mismatch_type,
                "expected": r.expected,
                "actual": r.actual,
                "gap": r.gap,
                "severity": r.severity.value,
                "detected_at": r.detected_at,
            }
            for r in filtered[:limit]
        ]

    def frozen_scopes(self) -> list[str]:
        with self._lock:
            return list(self._frozen_scopes)


# Module-level singleton
reconciliation_policy = ReconciliationPolicy()


__all__ = [
    "MismatchSeverity",
    "ReconciliationFreezeError",
    "ReconciliationPolicy",
    "ReconciliationThresholds",
    "reconciliation_policy",
]
