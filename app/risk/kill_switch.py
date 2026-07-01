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
    # Issue #220: when True the GlobalKillSwitchInterceptor rejects exit
    # orders too (HARD trip). Default False preserves the historical
    # "block entries only" behaviour (SOFT trip) used by daily-loss /
    # drawdown auto-trips so trailing-lock and operator manual flatten
    # can still close exposure.
    block_exits: bool = False


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


@dataclass(frozen=True)
class KillSwitchDurableRepairResult:
    status: str
    record: KillSwitchRecord


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


def _legacy_repair_reason(legacy_snapshot: Any, fallback_reason: str) -> str:
    legacy_reason: Optional[str] = None
    if isinstance(legacy_snapshot, dict):
        legacy_reason = (
            legacy_snapshot.get("legacy_reason")
            or legacy_snapshot.get("reason")
            or (legacy_snapshot.get("legacy_kill_switch") or {}).get("reason")
        )
    reason_text = str(legacy_reason or fallback_reason or "legacy_active").strip()
    return f"durable_repair_from_legacy: {reason_text}"


# Issue #220 (PR #233 review): cache schema-detection result so we don't
# re-query information_schema on every save_state / load_state call. The
# cache key is (host, db) inferred from the connection's DSN parameters
# where available, falling back to a single-bucket cache for the common
# single-Postgres deployment.

_SCHEMA_DETECTION_CACHE: Dict[str, bool] = {}


def _schema_cache_key(conn) -> str:
    """Best-effort cache key from a psycopg connection. Falls back to a
    single global bucket which is safe for the single-Postgres
    deployment Phoenix uses."""
    try:
        info = getattr(conn, "info", None)
        if info is not None:
            return f"{info.host}:{info.port}:{info.dbname}"
    except Exception:
        pass
    return "default"


def _kill_switch_state_table_has_block_exits_column(conn) -> bool:
    """Return True if the ``kill_switch_state`` table has the
    ``block_exits`` column (i.e. migration 017 has been applied).

    Cached per-connection-target so the per-call overhead is one
    information_schema query the first time only. Cache survives until
    process restart; if the migration is applied while the process is
    running, the cache misses the schema change — operators must restart
    the process to pick up the new column.
    """
    cache_key = _schema_cache_key(conn)
    cached = _SCHEMA_DETECTION_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'kill_switch_state' "
                "AND column_name = 'block_exits' LIMIT 1"
            )
            row = cur.fetchone()
        present = row is not None
    except Exception as exc:
        # If the information_schema query itself fails, default to
        # assuming the column is absent (legacy SQL is the safer
        # fallback). Log so it is observable.
        logger.warning(
            "kill_switch.schema_detect: information_schema query failed "
            "(%s); falling back to legacy SQL without block_exits", exc,
        )
        present = False
    _SCHEMA_DETECTION_CACHE[cache_key] = present
    return present


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
        *,
        block_exits: bool = False,
    ) -> KillSwitchRecord:
        """INACTIVE -> TRIPPED.  Creates a new record if none exists.

        Args:
            block_exits: Issue #220. When True (HARD trip), the
                GlobalKillSwitchInterceptor rejects exit orders too while
                this record is TRIPPED / CLEAR_PENDING. Default False
                (SOFT trip) preserves the historical "block entries only"
                behaviour used by daily-loss / drawdown auto-trips so
                trailing-lock and operator manual flatten can still close
                exposure.
        """
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
                block_exits=bool(block_exits),
            )
            self._records[key] = record

            self._emit_audit(
                actor=actor,
                action="kill_switch.trip",
                record=record,
                before_state=KillSwitchState.INACTIVE,
                metadata={
                    "reason": reason,
                    "block_exits": bool(block_exits),
                },
            )
            logger.warning(
                "Kill switch TRIPPED scope=%s scope_id=%s reason=%s "
                "actor=%s block_exits=%s",
                scope.value, scope_id, reason, actor, bool(block_exits),
            )
            return record

    def set_block_exits(
        self,
        scope: KillSwitchScope,
        scope_id: str,
        *,
        block_exits: bool,
        actor: str,
        reason: str,
    ) -> KillSwitchRecord:
        """Update ``block_exits`` on an existing TRIPPED / CLEAR_PENDING record.

        Issue #220 (PR #233 review). Without this method, an operator who
        needs to escalate a SOFT auto-trip (e.g. daily-loss propagated from
        the legacy RiskManager bridge) to a HARD panic stop has to first
        clear and rearm the kill switch — defeating the purpose of the
        HARD trip. This method allows in-place upgrade or downgrade of
        the flag while preserving the underlying TRIPPED state. Audited
        as ``kill_switch.set_block_exits``.

        Raises:
            KeyError: if no record exists for the (scope, scope_id).
            ValueError: if the existing record is not TRIPPED or
                CLEAR_PENDING (e.g. INACTIVE / CLEARED).
        """
        with self._lock:
            key = self._key(scope, scope_id)
            record = self._records.get(key)
            if record is None:
                raise KeyError(
                    f"No kill switch record for scope={scope.value}, "
                    f"scope_id={scope_id}"
                )
            if record.state not in (
                KillSwitchState.TRIPPED,
                KillSwitchState.CLEAR_PENDING,
            ):
                raise ValueError(
                    f"Cannot set block_exits: current state is "
                    f"{record.state.value} (must be TRIPPED or CLEAR_PENDING)"
                )
            previous = bool(record.block_exits)
            record.block_exits = bool(block_exits)
            record.updated_at = _now_iso()

            self._emit_audit(
                actor=actor,
                action="kill_switch.set_block_exits",
                record=record,
                before_state=record.state,
                metadata={
                    "reason": reason,
                    "previous_block_exits": previous,
                    "new_block_exits": bool(block_exits),
                },
            )
            logger.warning(
                "Kill switch BLOCK_EXITS UPDATED scope=%s scope_id=%s "
                "previous=%s new=%s actor=%s reason=%s",
                scope.value, scope_id, previous, bool(block_exits),
                actor, reason,
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

    def is_tripped_for_scope_with_block_exits(
        self,
        tenant_id: Optional[str] = None,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> Tuple[bool, bool]:
        """Hierarchical check returning (tripped, any_record_blocks_exits).

        Issue #220. The second element is True if ANY active record at
        any matching scope has ``block_exits=True``. Caller (typically
        ``GlobalKillSwitchInterceptor``) uses this to decide whether to
        reject exit orders too (HARD trip) or only entries (SOFT trip).
        """
        scopes_to_check: List[Tuple[KillSwitchScope, str]] = [
            (KillSwitchScope.GLOBAL, "GLOBAL"),
        ]
        if tenant_id:
            scopes_to_check.append((KillSwitchScope.TENANT, tenant_id))
        if account_id:
            scopes_to_check.append((KillSwitchScope.ACCOUNT, account_id))
        if strategy_id:
            scopes_to_check.append((KillSwitchScope.STRATEGY, strategy_id))

        with self._lock:
            tripped = False
            block_exits = False
            for scope, scope_id in scopes_to_check:
                record = self._records.get(self._key(scope, scope_id))
                if record is None:
                    continue
                if record.state in (
                    KillSwitchState.TRIPPED,
                    KillSwitchState.CLEAR_PENDING,
                ):
                    tripped = True
                    if record.block_exits:
                        block_exits = True
            return tripped, block_exits

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
                # Issue #220: optional column; tolerate absence for rows
                # written before migration 017.
                block_exits=bool(row.get("block_exits", False)),
            )
            manager._records[manager._key(scope, row["scope_id"])] = record
        return manager

    def save_state(self, conn) -> None:
        """Upsert all kill switch records to the kill_switch_state Postgres table.

        Issue #220 (PR #233 review): tolerates the pre-migration schema
        (migration 017 not yet applied, ``block_exits`` column absent).
        Detects column presence via ``information_schema`` and falls back
        to the legacy SQL when missing. Caches the detection result so
        the per-call overhead is one query the first time only.

        IMPORTANT: when running on the pre-017 schema, ``block_exits`` is
        not persisted — a HARD trip survives in-memory but reverts to
        SOFT on restart. Operators MUST run migration 017 in the same
        deployment window as this code; the legacy fallback exists ONLY
        to keep the service booting through the brief deploy gap.
        """
        rows = self.to_persistence_dict()
        if not rows:
            return
        has_block_exits = _kill_switch_state_table_has_block_exits_column(conn)
        if has_block_exits:
            sql = """
                INSERT INTO kill_switch_state
                    (id, scope, scope_id, state, tripped_at, tripped_by, trip_reason,
                     cleared_at, cleared_by, clear_reason, clear_request_id, updated_at,
                     block_exits)
                VALUES
                    (%(id)s, %(scope)s, %(scope_id)s, %(state)s, %(tripped_at)s,
                     %(tripped_by)s, %(trip_reason)s, %(cleared_at)s, %(cleared_by)s,
                     %(clear_reason)s, %(clear_request_id)s, %(updated_at)s,
                     %(block_exits)s)
                ON CONFLICT (scope, scope_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    tripped_at = EXCLUDED.tripped_at,
                    tripped_by = EXCLUDED.tripped_by,
                    trip_reason = EXCLUDED.trip_reason,
                    cleared_at = EXCLUDED.cleared_at,
                    cleared_by = EXCLUDED.cleared_by,
                    clear_reason = EXCLUDED.clear_reason,
                    clear_request_id = EXCLUDED.clear_request_id,
                    updated_at = EXCLUDED.updated_at,
                    block_exits = EXCLUDED.block_exits
            """
        else:
            # Legacy SQL — pre-migration-017 schema. Loud WARNING so any
            # HARD-trip persistence loss is observable.
            logger.warning(
                "kill_switch.save_state: kill_switch_state.block_exits column "
                "missing (migration 017 not yet applied) — HARD trip "
                "(block_exits=True) will NOT survive a restart until the "
                "migration is run.",
            )
            sql = """
                INSERT INTO kill_switch_state
                    (id, scope, scope_id, state, tripped_at, tripped_by, trip_reason,
                     cleared_at, cleared_by, clear_reason, clear_request_id, updated_at)
                VALUES
                    (%(id)s, %(scope)s, %(scope_id)s, %(state)s, %(tripped_at)s,
                     %(tripped_by)s, %(trip_reason)s, %(cleared_at)s, %(cleared_by)s,
                     %(clear_reason)s, %(clear_request_id)s, %(updated_at)s)
                ON CONFLICT (scope, scope_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    tripped_at = EXCLUDED.tripped_at,
                    tripped_by = EXCLUDED.tripped_by,
                    trip_reason = EXCLUDED.trip_reason,
                    cleared_at = EXCLUDED.cleared_at,
                    cleared_by = EXCLUDED.cleared_by,
                    clear_reason = EXCLUDED.clear_reason,
                    clear_request_id = EXCLUDED.clear_request_id,
                    updated_at = EXCLUDED.updated_at
            """
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(sql, row)

    @classmethod
    def load_state(
        cls,
        conn,
        *,
        audit_fn: Optional[Callable[..., Any]] = None,
    ) -> KillSwitchManager:
        """Restore manager state from the kill_switch_state Postgres table."""
        # Issue #220 (PR #233 review): tolerate pre-migration-017 schema.
        # AppRuntime treats load_state failures as fatal in LIVE; we MUST
        # not break boot during the deploy window between code rollout
        # and migration application.
        if _kill_switch_state_table_has_block_exits_column(conn):
            sql = """
                SELECT id, scope, scope_id, state, tripped_at, tripped_by, trip_reason,
                       cleared_at, cleared_by, clear_reason, clear_request_id, updated_at,
                       block_exits
                FROM kill_switch_state
                WHERE state != 'INACTIVE'
            """
        else:
            logger.warning(
                "kill_switch.load_state: kill_switch_state.block_exits column "
                "missing (migration 017 not yet applied) — restored records "
                "will default to block_exits=False until the migration is run.",
            )
            sql = """
                SELECT id, scope, scope_id, state, tripped_at, tripped_by, trip_reason,
                       cleared_at, cleared_by, clear_reason, clear_request_id, updated_at
                FROM kill_switch_state
                WHERE state != 'INACTIVE'
            """
        with conn.cursor() as cur:
            cur.execute(sql)
            col_names = [desc[0] for desc in cur.description]
            rows = [dict(zip(col_names, row)) for row in (cur.fetchall() or [])]
        manager = cls.from_persistence_dict(rows, audit_fn=audit_fn)
        if rows:
            logger.info(
                "kill_switch.load_state: restored %d non-INACTIVE records from Postgres",
                len(rows),
            )
        return manager


def repair_durable_global_from_legacy(
    ksm: KillSwitchManager,
    legacy_snapshot: Any,
    actor: str,
    reason: str,
    block_exits: bool = False,
) -> KillSwitchDurableRepairResult:
    """Repair GLOBAL/GLOBAL durable state after a legacy-active divergence.

    This is intentionally scoped to the single authoritative GLOBAL record.
    Calling it repeatedly will not create duplicate active rows: once the
    durable GLOBAL record is TRIPPED or CLEAR_PENDING, the existing record is
    returned unchanged with ``status="already_durable_active"``.
    """
    if ksm is None:
        raise ValueError("KillSwitchManager is required")

    repair_reason = _legacy_repair_reason(legacy_snapshot, reason)
    now = _now_iso()
    with ksm._lock:
        key = ksm._key(KillSwitchScope.GLOBAL, "GLOBAL")
        existing = ksm._records.get(key)
        if existing is not None and existing.state in (
            KillSwitchState.TRIPPED,
            KillSwitchState.CLEAR_PENDING,
        ):
            return KillSwitchDurableRepairResult(
                status="already_durable_active",
                record=existing,
            )

        before_state = (
            existing.state if existing is not None else KillSwitchState.INACTIVE
        )
        if existing is None:
            record = KillSwitchRecord(
                id=uuid4().hex,
                state=KillSwitchState.TRIPPED,
                scope=KillSwitchScope.GLOBAL,
                scope_id="GLOBAL",
                tripped_at=now,
                tripped_by=actor,
                trip_reason=repair_reason,
                updated_at=now,
                block_exits=bool(block_exits),
            )
            ksm._records[key] = record
        else:
            record = existing
            record.state = KillSwitchState.TRIPPED
            record.tripped_at = now
            record.tripped_by = actor
            record.trip_reason = repair_reason
            record.cleared_at = None
            record.cleared_by = None
            record.clear_reason = None
            record.clear_request_id = None
            record.updated_at = now
            record.block_exits = bool(block_exits)

        ksm._emit_audit(
            actor=actor,
            action="kill_switch.durable_repair",
            record=record,
            before_state=before_state,
            metadata={
                "reason": repair_reason,
                "operator_reason": reason,
                "legacy_snapshot": legacy_snapshot if isinstance(legacy_snapshot, dict) else {},
                "block_exits": bool(block_exits),
                "status": "repaired",
            },
        )
        logger.warning(
            "Kill switch DURABLE REPAIR scope=GLOBAL scope_id=GLOBAL "
            "actor=%s reason=%s block_exits=%s",
            actor,
            repair_reason,
            bool(block_exits),
        )
        return KillSwitchDurableRepairResult(status="repaired", record=record)


def get_kill_switch_state() -> dict:
    """Return a serializable snapshot of the current kill switch state.

    Pulls from the hub runtime's KillSwitchManager if one is wired; falls back
    to the legacy risk_manager flag so the incident snapshot always has data.

    Issue #222: also includes ``legacy_active`` / ``divergence`` fields so
    operators can see the legacy stream-path state and any divergence with
    the durable manager from a single ``GET /admin/kill-switch/state`` call.
    """
    try:
        from app.hub.runtime import get_hub_runtime
        runtime = get_hub_runtime()
        ksm = getattr(runtime, "kill_switch_manager", None)
        if ksm is not None and hasattr(ksm, "to_persistence_dict"):
            records = ksm.to_persistence_dict()
            payload: dict = {
                "source": "kill_switch_manager",
                "records": records,
                "active_count": sum(
                    1 for r in records if r.get("state") not in ("INACTIVE", "CLEARED")
                ),
            }
            # Issue #222: enrich with legacy state + divergence detection.
            try:
                divergence = runtime.compute_kill_switch_divergence()
                payload["legacy_kill_switch"] = {
                    "publisher_seen": bool(divergence.get("publisher_seen", False)),
                    "active": bool(divergence.get("legacy_active", False)),
                    "reason": divergence.get("legacy_reason"),
                    "divergence_age_seconds": divergence.get(
                        "divergence_age_seconds"
                    ),
                    "registered_risk_managers": (
                        runtime.get_legacy_kill_switch_snapshot().get(
                            "registered_risk_managers", []
                        )
                        if callable(
                            getattr(runtime, "get_legacy_kill_switch_snapshot", None)
                        )
                        else []
                    ),
                }
                payload["divergence"] = {
                    "divergent": bool(divergence.get("divergent", False)),
                    "durable_global_active": divergence.get(
                        "durable_global_active"
                    ),
                }
            except Exception:
                # Older runtimes without compute_kill_switch_divergence —
                # don't break get_kill_switch_state.
                pass
            return payload
    except Exception:
        pass

    try:
        from app.hub.runtime import get_hub_runtime
        runtime = get_hub_runtime()
        hub = getattr(runtime, "hub", None)
        if hub is not None:
            for runner in getattr(hub, "_runners", {}).values():
                rm = getattr(runner, "_risk_manager", None)
                if rm is not None:
                    return {
                        "source": "risk_manager",
                        "kill_switch_activated": bool(getattr(rm, "kill_switch_activated", False)),
                        "kill_switch_date": str(getattr(rm, "kill_switch_date", None)),
                    }
    except Exception:
        pass

    return {"source": "unavailable"}


__all__ = [
    "KillSwitchClearRequest",
    "KillSwitchClearValidation",
    "KillSwitchDurableRepairResult",
    "KillSwitchManager",
    "KillSwitchRecord",
    "KillSwitchScope",
    "KillSwitchState",
    "get_kill_switch_state",
    "repair_durable_global_from_legacy",
]
