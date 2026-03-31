"""
Architecture 12.1: Kill-switch clear/re-arm state machine.

Provides scoped kill switches (global, tenant, account, strategy) with an
explicit operator-driven clear workflow.  Clearing never happens implicitly
on a profitable tick, broker poll, or process restart.

State transitions
-----------------
    INACTIVE  --trip-->      TRIPPED
    TRIPPED   --request_clear--> CLEAR_PENDING   (validation must pass)
    CLEAR_PENDING --confirm_clear--> CLEARED
    CLEARED   --rearm-->     INACTIVE

Every mutation is durably audited via ``emit_audit_event``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from app.core.audit_log import emit_audit_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class KillSwitchState(str, Enum):
    INACTIVE = "INACTIVE"
    TRIPPED = "TRIPPED"
    CLEAR_PENDING = "CLEAR_PENDING"
    CLEARED = "CLEARED"


class KillSwitchScope(str, Enum):
    GLOBAL = "GLOBAL"
    TENANT = "TENANT"
    ACCOUNT = "ACCOUNT"
    STRATEGY = "STRATEGY"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class KillSwitchRecord:
    id: str
    state: KillSwitchState
    scope: KillSwitchScope
    scope_id: str
    tripped_at: Optional[str] = None
    tripped_by: Optional[str] = None
    trip_reason: Optional[str] = None
    cleared_at: Optional[str] = None
    cleared_by: Optional[str] = None
    clear_reason: Optional[str] = None
    clear_request_id: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class KillSwitchClearRequest:
    scope: KillSwitchScope
    scope_id: str
    actor: str
    reason_code: str
    request_id: str
    break_glass: bool = False


@dataclass
class KillSwitchClearValidation:
    passed: bool
    failures: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Allowed transitions
# ---------------------------------------------------------------------------

_TRANSITIONS: Dict[Tuple[KillSwitchState, str], KillSwitchState] = {
    (KillSwitchState.INACTIVE, "trip"): KillSwitchState.TRIPPED,
    (KillSwitchState.TRIPPED, "request_clear"): KillSwitchState.CLEAR_PENDING,
    (KillSwitchState.CLEAR_PENDING, "confirm_clear"): KillSwitchState.CLEARED,
    (KillSwitchState.CLEARED, "rearm"): KillSwitchState.INACTIVE,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class KillSwitchManager:
    """Thread-safe kill-switch state machine with scoped hierarchical checks."""

    def __init__(self, *, audit_fn: Optional[Callable[..., Any]] = None) -> None:
        self._records: Dict[Tuple[KillSwitchScope, str], KillSwitchRecord] = {}
        self._lock = RLock()
        self._audit_fn = audit_fn or emit_audit_event

    # -- helpers -------------------------------------------------------------

    def _key(self, scope: KillSwitchScope, scope_id: str) -> Tuple[KillSwitchScope, str]:
        return (scope, scope_id)

    def _assert_transition(
        self, record: KillSwitchRecord, action: str,
    ) -> KillSwitchState:
        target = _TRANSITIONS.get((record.state, action))
        if target is None:
            raise ValueError(
                f"Invalid transition: {record.state.value} --{action}--> ? "
                f"(scope={record.scope.value}, scope_id={record.scope_id})"
            )
        return target

    def _emit_audit(
        self,
        *,
        actor: str,
        action: str,
        record: KillSwitchRecord,
        before_state: KillSwitchState,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._audit_fn(
            actor=actor,
            action=action,
            resource_type="kill_switch",
            resource_id=record.id,
            before={"state": before_state.value, "scope": record.scope.value, "scope_id": record.scope_id},
            after={"state": record.state.value, "scope": record.scope.value, "scope_id": record.scope_id},
            request_id=request_id,
            metadata=metadata,
        )

    # -- mutations -----------------------------------------------------------

    def trip(
        self,
        scope: KillSwitchScope,
        scope_id: str,
        reason: str,
        actor: str,
    ) -> KillSwitchRecord:
        """INACTIVE -> TRIPPED.  Creates a new record if none exists."""
        with self._lock:
            key = self._key(scope, scope_id)
            existing = self._records.get(key)

            if existing is not None and existing.state != KillSwitchState.INACTIVE:
                raise ValueError(
                    f"Cannot trip kill switch: current state is "
                    f"{existing.state.value} (scope={scope.value}, scope_id={scope_id})"
                )

            now = _now_iso()
            record = KillSwitchRecord(
                id=uuid4().hex,
                state=KillSwitchState.TRIPPED,
                scope=scope,
                scope_id=scope_id,
                tripped_at=now,
                tripped_by=actor,
                trip_reason=reason,
                updated_at=now,
            )
            self._records[key] = record

            self._emit_audit(
                actor=actor,
                action="kill_switch.trip",
                record=record,
                before_state=KillSwitchState.INACTIVE,
                metadata={"reason": reason},
            )
            logger.warning(
                "Kill switch TRIPPED scope=%s scope_id=%s reason=%s actor=%s",
                scope.value, scope_id, reason, actor,
            )
            return record

    def request_clear(
        self,
        clear_request: KillSwitchClearRequest,
        validation_fn: Callable[[KillSwitchClearRequest], KillSwitchClearValidation],
    ) -> Tuple[KillSwitchRecord, KillSwitchClearValidation]:
        """TRIPPED -> CLEAR_PENDING if validation passes.

        ``validation_fn`` is called with the clear request and must return a
        ``KillSwitchClearValidation``.  The function should check that:
        - Required control inputs are fresh and available.
        - No unresolved RECONCILING / ORPHAN_REVIEW or other operator-blocking
          authoritative position states exist for the same scope (unless
          ``break_glass`` is set).
        """
        with self._lock:
            key = self._key(clear_request.scope, clear_request.scope_id)
            record = self._records.get(key)
            if record is None:
                raise KeyError(
                    f"No kill switch record for scope={clear_request.scope.value}, "
                    f"scope_id={clear_request.scope_id}"
                )
            self._assert_transition(record, "request_clear")

            validation = validation_fn(clear_request)
            if not validation.passed:
                logger.info(
                    "Kill switch clear DENIED scope=%s scope_id=%s failures=%s",
                    clear_request.scope.value, clear_request.scope_id,
                    validation.failures,
                )
                return record, validation

            before_state = record.state
            record.state = KillSwitchState.CLEAR_PENDING
            record.clear_request_id = clear_request.request_id
            record.clear_reason = clear_request.reason_code
            record.cleared_by = clear_request.actor
            record.updated_at = _now_iso()

            self._emit_audit(
                actor=clear_request.actor,
                action="kill_switch.request_clear",
                record=record,
                before_state=before_state,
                request_id=clear_request.request_id,
                metadata={
                    "reason_code": clear_request.reason_code,
                    "break_glass": clear_request.break_glass,
                },
            )
            logger.info(
                "Kill switch CLEAR_PENDING scope=%s scope_id=%s actor=%s",
                clear_request.scope.value, clear_request.scope_id,
                clear_request.actor,
            )
            return record, validation

    def confirm_clear(
        self,
        scope: KillSwitchScope,
        scope_id: str,
    ) -> KillSwitchRecord:
        """CLEAR_PENDING -> CLEARED.  Called once fresh-data validation succeeds."""
        with self._lock:
            key = self._key(scope, scope_id)
            record = self._records.get(key)
            if record is None:
                raise KeyError(
                    f"No kill switch record for scope={scope.value}, scope_id={scope_id}"
                )
            before_state = record.state
            self._assert_transition(record, "confirm_clear")

            record.state = KillSwitchState.CLEARED
            record.cleared_at = _now_iso()
            record.updated_at = _now_iso()

            self._emit_audit(
                actor=record.cleared_by or "system",
                action="kill_switch.confirm_clear",
                record=record,
                before_state=before_state,
                request_id=record.clear_request_id,
            )
            logger.info(
                "Kill switch CLEARED scope=%s scope_id=%s",
                scope.value, scope_id,
            )
            return record

    def rearm(
        self,
        scope: KillSwitchScope,
        scope_id: str,
        actor: str,
    ) -> KillSwitchRecord:
        """CLEARED -> INACTIVE.  Explicit re-arm after validation succeeds."""
        with self._lock:
            key = self._key(scope, scope_id)
            record = self._records.get(key)
            if record is None:
                raise KeyError(
                    f"No kill switch record for scope={scope.value}, scope_id={scope_id}"
                )
            before_state = record.state
            self._assert_transition(record, "rearm")

            record.state = KillSwitchState.INACTIVE
            record.updated_at = _now_iso()

            self._emit_audit(
                actor=actor,
                action="kill_switch.rearm",
                record=record,
                before_state=before_state,
            )
            logger.info(
                "Kill switch RE-ARMED (INACTIVE) scope=%s scope_id=%s actor=%s",
                scope.value, scope_id, actor,
            )
            return record

    # -- queries -------------------------------------------------------------

    def is_tripped(self, scope: KillSwitchScope, scope_id: str) -> bool:
        """True when the switch is TRIPPED or CLEAR_PENDING (entry still blocked)."""
        with self._lock:
            record = self._records.get(self._key(scope, scope_id))
            if record is None:
                return False
            return record.state in (
                KillSwitchState.TRIPPED,
                KillSwitchState.CLEAR_PENDING,
            )

    def is_tripped_for_scope(
        self,
        tenant_id: Optional[str] = None,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> bool:
        """Hierarchical check: global -> tenant -> account -> strategy.

        Returns True if *any* level in the hierarchy is tripped.
        """
        with self._lock:
            # Global always checked first
            if self.is_tripped(KillSwitchScope.GLOBAL, "GLOBAL"):
                return True
            if tenant_id and self.is_tripped(KillSwitchScope.TENANT, tenant_id):
                return True
            if account_id and self.is_tripped(KillSwitchScope.ACCOUNT, account_id):
                return True
            if strategy_id and self.is_tripped(KillSwitchScope.STRATEGY, strategy_id):
                return True
            return False

    def get_record(
        self, scope: KillSwitchScope, scope_id: str,
    ) -> Optional[KillSwitchRecord]:
        with self._lock:
            return self._records.get(self._key(scope, scope_id))

    def get_all_active(self) -> List[KillSwitchRecord]:
        """Return all records whose state is not INACTIVE."""
        with self._lock:
            return [
                r for r in self._records.values()
                if r.state != KillSwitchState.INACTIVE
            ]

    # -- persistence ---------------------------------------------------------

    def to_persistence_dict(self) -> List[Dict[str, Any]]:
        """Serialize all records for Postgres / JSON storage."""
        with self._lock:
            results: List[Dict[str, Any]] = []
            for record in self._records.values():
                d = asdict(record)
                d["state"] = record.state.value
                d["scope"] = record.scope.value
                results.append(d)
            return results

    @classmethod
    def from_persistence_dict(
        cls,
        rows: List[Dict[str, Any]],
        *,
        audit_fn: Optional[Callable[..., Any]] = None,
    ) -> KillSwitchManager:
        """Restore manager state from persisted records."""
        manager = cls(audit_fn=audit_fn)
        for row in rows:
            state = KillSwitchState(row["state"])
            scope = KillSwitchScope(row["scope"])
            record = KillSwitchRecord(
                id=row["id"],
                state=state,
                scope=scope,
                scope_id=row["scope_id"],
                tripped_at=row.get("tripped_at"),
                tripped_by=row.get("tripped_by"),
                trip_reason=row.get("trip_reason"),
                cleared_at=row.get("cleared_at"),
                cleared_by=row.get("cleared_by"),
                clear_reason=row.get("clear_reason"),
                clear_request_id=row.get("clear_request_id"),
                updated_at=row.get("updated_at"),
            )
            manager._records[manager._key(scope, row["scope_id"])] = record
        return manager


__all__ = [
    "KillSwitchClearRequest",
    "KillSwitchClearValidation",
    "KillSwitchManager",
    "KillSwitchRecord",
    "KillSwitchScope",
    "KillSwitchState",
]
