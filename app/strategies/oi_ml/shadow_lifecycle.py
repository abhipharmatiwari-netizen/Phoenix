"""Paper-only shadow lifecycle recording for OI/ML order intents."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
import re
from typing import Any, Mapping, Protocol

from app.strategies.oi_ml.order_intents import OiMlOrderIntent


_POSTGRES_NAME_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class OiMlShadowIntentStatus(str, Enum):
    """Allowed shadow lifecycle states."""

    STAGED = "STAGED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class OiMlShadowLifecycleRecord:
    """Persisted paper-only lifecycle record."""

    record_id: int | None
    intent_id: str
    strategy_id: str
    tenant_id: str | None
    broker_account_id: str | None
    status: OiMlShadowIntentStatus
    structure: str
    underlying: str
    expiry: date
    short_strike: int
    quantity: int
    estimated_net_credit_points: float
    estimated_max_loss_rupees: float
    dry_run_only: bool
    decision_reason: str | None
    guard_reasons: tuple[str, ...]
    intent_payload: Mapping[str, Any]
    created_at: datetime
    recorded_at: datetime | None = None


class OiMlShadowLifecycleStore(Protocol):
    """Store protocol used by the strategy staging path."""

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        """Record a dry-run intent and return the stored lifecycle row."""


class InMemoryOiMlShadowLifecycleStore:
    """Test/dry-run store that preserves the same record shape as Postgres."""

    def __init__(self) -> None:
        self.records: list[OiMlShadowLifecycleRecord] = []

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        record = record_from_intent(
            intent,
            record_id=len(self.records) + 1,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            recorded_at=intent.created_at,
        )
        self.records.append(record)
        return record


class PostgresOiMlShadowLifecycleStore:
    """Postgres-backed paper-only shadow lifecycle store."""

    def __init__(
        self,
        conn: Any,
        *,
        table_name: str = "public.oi_ml_shadow_order_intents",
    ) -> None:
        self.conn = conn
        self.table_name = _quoted_table_name(table_name)

    def record_intent(
        self,
        intent: OiMlOrderIntent,
        *,
        decision_reason: str | None = None,
        tenant_id: str | None = None,
        broker_account_id: str | None = None,
    ) -> OiMlShadowLifecycleRecord:
        record = record_from_intent(
            intent,
            record_id=None,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
        )
        params = _record_params(record)
        sql = f"""
            INSERT INTO {self.table_name} (
                intent_id,
                strategy_id,
                tenant_id,
                broker_account_id,
                status,
                structure,
                underlying,
                expiry,
                short_strike,
                quantity,
                estimated_net_credit_points,
                estimated_max_loss_rupees,
                dry_run_only,
                decision_reason,
                guard_reasons,
                intent_payload,
                created_at
            ) VALUES (
                %(intent_id)s,
                %(strategy_id)s,
                %(tenant_id)s,
                %(broker_account_id)s,
                %(status)s,
                %(structure)s,
                %(underlying)s,
                %(expiry)s,
                %(short_strike)s,
                %(quantity)s,
                %(estimated_net_credit_points)s,
                %(estimated_max_loss_rupees)s,
                TRUE,
                %(decision_reason)s,
                %(guard_reasons)s::jsonb,
                %(intent_payload)s::jsonb,
                %(created_at)s
            )
            ON CONFLICT (intent_id) DO UPDATE SET
                status = EXCLUDED.status,
                decision_reason = EXCLUDED.decision_reason,
                guard_reasons = EXCLUDED.guard_reasons,
                intent_payload = EXCLUDED.intent_payload,
                updated_at = NOW()
            RETURNING
                id,
                recorded_at
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        record_id = None
        recorded_at = None
        if row:
            if isinstance(row, Mapping):
                record_id = row.get("id")
                recorded_at = row.get("recorded_at")
            else:
                record_id = row[0]
                recorded_at = row[1] if len(row) > 1 else None
        return record_from_intent(
            intent,
            record_id=int(record_id) if record_id is not None else None,
            decision_reason=decision_reason,
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            recorded_at=recorded_at,
        )


def record_from_intent(
    intent: OiMlOrderIntent,
    *,
    record_id: int | None,
    decision_reason: str | None = None,
    tenant_id: str | None = None,
    broker_account_id: str | None = None,
    recorded_at: datetime | None = None,
) -> OiMlShadowLifecycleRecord:
    """Build a record object from an inert dry-run intent."""

    if not bool(intent.dry_run_only):
        raise ValueError("OI/ML shadow lifecycle accepts dry_run_only intents only")
    payload = intent_payload(intent)
    return OiMlShadowLifecycleRecord(
        record_id=record_id,
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        status=OiMlShadowIntentStatus.STAGED,
        structure=intent.structure.value,
        underlying=intent.underlying,
        expiry=intent.expiry,
        short_strike=int(intent.short_strike),
        quantity=int(intent.quantity),
        estimated_net_credit_points=float(intent.estimated_net_credit_points),
        estimated_max_loss_rupees=float(intent.estimated_max_loss_rupees),
        dry_run_only=True,
        decision_reason=decision_reason,
        guard_reasons=tuple(str(reason) for reason in intent.guard_reasons),
        intent_payload=payload,
        created_at=intent.created_at,
        recorded_at=recorded_at,
    )


def intent_payload(intent: OiMlOrderIntent) -> dict[str, Any]:
    """Return a JSON-safe payload for one dry-run intent."""

    payload = _json_safe(intent)
    if not isinstance(payload, dict):
        raise ValueError("intent payload must serialize to an object")
    payload["dry_run_only"] = True
    return payload


def _record_params(record: OiMlShadowLifecycleRecord) -> dict[str, Any]:
    return {
        "intent_id": record.intent_id,
        "strategy_id": record.strategy_id,
        "tenant_id": record.tenant_id,
        "broker_account_id": record.broker_account_id,
        "status": record.status.value,
        "structure": record.structure,
        "underlying": record.underlying,
        "expiry": record.expiry,
        "short_strike": record.short_strike,
        "quantity": record.quantity,
        "estimated_net_credit_points": record.estimated_net_credit_points,
        "estimated_max_loss_rupees": record.estimated_max_loss_rupees,
        "decision_reason": record.decision_reason,
        "guard_reasons": json.dumps(list(record.guard_reasons), separators=(",", ":")),
        "intent_payload": json.dumps(record.intent_payload, sort_keys=True, separators=(",", ":")),
        "created_at": record.created_at,
    }


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _quoted_table_name(table_name: str) -> str:
    parts = [part.strip() for part in str(table_name).split(".") if part.strip()]
    if not parts:
        raise ValueError("table_name is required")
    for part in parts:
        if not _POSTGRES_NAME_PART.match(part):
            raise ValueError(f"Invalid postgres table identifier: {part!r}")
    return ".".join(f'"{part}"' for part in parts)


__all__ = [
    "InMemoryOiMlShadowLifecycleStore",
    "OiMlShadowIntentStatus",
    "OiMlShadowLifecycleRecord",
    "OiMlShadowLifecycleStore",
    "PostgresOiMlShadowLifecycleStore",
    "intent_payload",
    "record_from_intent",
]
