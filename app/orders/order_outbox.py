"""
Durable order submission outbox for restart-safe router recovery.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.data.postgres import connect_with_retry, get_control_plane_dsn
from app.orders.position_ownership import ContractKey

logger = logging.getLogger(__name__)

OUTBOX_STATUS_PENDING = "PENDING"
OUTBOX_STATUS_SUBMITTING = "SUBMITTING"
OUTBOX_STATUS_ACKED = "ACKED"
OUTBOX_STATUS_TERMINAL_FILL = "TERMINAL_FILL"
OUTBOX_STATUS_TERMINAL_NON_FILL = "TERMINAL_NON_FILL"
_ACTIVE_OUTBOX_STATUSES = {
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_SUBMITTING,
    OUTBOX_STATUS_ACKED,
}
_TERMINAL_FILL_STATUSES = {
    "FILLED",
    "FULL",
    "COMPLETE",
    "COMPLETED",
    "EXECUTED",
}
_TERMINAL_NON_FILL_STATUSES = {
    "REJECTED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}


class OrderSubmissionOutbox(Protocol):
    def reserve_submission(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        hub_order_id: str,
        order_req: OrderRequest,
        contract_key: Optional[ContractKey],
        ownership_strategy_id: Optional[StrategyId],
        created_at: datetime,
    ) -> tuple[bool, "OrderOutboxRecord"]: ...

    def mark_submitting(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        updated_at: datetime,
    ) -> "OrderOutboxRecord": ...

    def record_response(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        response: OrderResponse,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> "OrderOutboxRecord": ...

    def record_submit_error(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        error_text: str,
        updated_at: datetime,
    ) -> "OrderOutboxRecord": ...

    def mark_terminal_by_broker_order(
        self,
        *,
        broker_account_id: BrokerAccountId,
        broker_order_id: str,
        terminal_status: str,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> Optional["OrderOutboxRecord"]: ...

    def list_active_submissions(
        self,
        *,
        limit: int = 500,
    ) -> list["OrderOutboxRecord"]: ...


def _is_true(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_identifier(raw: str, default: str) -> str:
    text = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(raw or ""))
    text = text.strip("_")
    return text or default


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return _to_utc(value).isoformat()
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _json_safe(value.model_dump())
    if hasattr(value, "__dict__"):
        return {
            str(k): _json_safe(v)
            for k, v in value.__dict__.items()
            if not str(k).startswith("_")
        }
    return str(value)


def _serialize_contract_key(
    contract_key: Optional[ContractKey],
) -> Optional[dict[str, Any]]:
    if contract_key is None:
        return None
    return {
        "underlying": contract_key.underlying,
        "expiry": contract_key.expiry,
        "strike": contract_key.strike,
        "option_right": contract_key.option_right,
        "product_type": contract_key.product_type,
    }


def _deserialize_contract_key(payload: object) -> Optional[ContractKey]:
    if not isinstance(payload, dict):
        return None
    if not any(str(payload.get(key) or "").strip() for key in payload):
        return None
    try:
        return ContractKey(
            underlying=str(payload.get("underlying") or ""),
            expiry=str(payload.get("expiry") or ""),
            strike=str(payload.get("strike") or ""),
            option_right=str(payload.get("option_right") or ""),
            product_type=str(payload.get("product_type") or ""),
        )
    except Exception:
        return None


def _serialize_order_request(order_req: OrderRequest) -> dict[str, Any]:
    return {
        "symbol": str(order_req.symbol or ""),
        "quantity": int(order_req.quantity or 0),
        "side": str(order_req.side.value),
        "order_type": str(order_req.order_type.value),
        "product_type": str(order_req.product_type.value),
        "time_in_force": str(order_req.time_in_force.value),
        "limit_price": order_req.limit_price,
        "stop_price": order_req.stop_price,
        "tag": order_req.tag,
        "purpose": getattr(order_req.purpose, "value", None) if order_req.purpose else None,
        "exchange": order_req.exchange,
        "symbol_token": order_req.symbol_token,
        "idempotency_key": order_req.idempotency_key,
        "position_ownership_bypass": bool(
            getattr(order_req, "position_ownership_bypass", False)
        ),
        "tick_size": order_req.tick_size,
        "position_label": order_req.position_label,
        "strategy_context": _json_safe(order_req.strategy_context),
        "exit_reason": order_req.exit_reason,
        "position_id": order_req.position_id,
        "contract_key_ref": order_req.contract_key_ref,
        "strategy_id": order_req.strategy_id,
        "account_id": order_req.account_id,
    }


def _deserialize_order_request(payload: dict[str, Any]) -> OrderRequest:
    purpose_raw = str(payload.get("purpose") or "").strip()
    purpose = OrderPurpose(purpose_raw) if purpose_raw else None
    return OrderRequest(
        symbol=str(payload.get("symbol") or ""),
        quantity=int(payload.get("quantity") or 0),
        side=OrderSide(str(payload.get("side") or OrderSide.BUY.value)),
        order_type=OrderType(str(payload.get("order_type") or OrderType.MARKET.value)),
        product_type=ProductType(
            str(payload.get("product_type") or ProductType.INTRADAY.value)
        ),
        time_in_force=TimeInForce(
            str(payload.get("time_in_force") or TimeInForce.DAY.value)
        ),
        limit_price=payload.get("limit_price"),
        stop_price=payload.get("stop_price"),
        tag=payload.get("tag"),
        purpose=purpose,
        exchange=payload.get("exchange"),
        symbol_token=payload.get("symbol_token"),
        idempotency_key=payload.get("idempotency_key"),
        position_ownership_bypass=bool(payload.get("position_ownership_bypass", False)),
        tick_size=payload.get("tick_size"),
        position_label=payload.get("position_label"),
        strategy_context=payload.get("strategy_context"),
        exit_reason=payload.get("exit_reason"),
        position_id=payload.get("position_id"),
        contract_key_ref=payload.get("contract_key_ref"),
        strategy_id=payload.get("strategy_id"),
        account_id=payload.get("account_id"),
    )


def _serialize_order_response(response: OrderResponse) -> dict[str, Any]:
    return {
        "broker_order_id": str(response.broker_order_id or ""),
        "status": str(response.status or ""),
        "message": str(response.message or ""),
        "filled_quantity": int(response.filled_quantity or 0),
        "average_price": response.average_price,
        "requested_quantity": response.requested_quantity,
        "execution_mode": response.execution_mode,
        "virtual": response.virtual,
        "details": _json_safe(response.details),
    }


def _deserialize_order_response(payload: dict[str, Any]) -> OrderResponse:
    return OrderResponse(
        broker_order_id=str(payload.get("broker_order_id") or ""),
        status=str(payload.get("status") or ""),
        message=str(payload.get("message") or ""),
        filled_quantity=int(payload.get("filled_quantity") or 0),
        average_price=payload.get("average_price"),
        requested_quantity=payload.get("requested_quantity"),
        execution_mode=payload.get("execution_mode"),
        virtual=payload.get("virtual"),
        details=payload.get("details"),
    )


def _outbox_status_from_response(response: OrderResponse) -> str:
    status = str(response.status or "").strip().upper()
    details = response.details if isinstance(response.details, dict) else {}
    if status == OUTBOX_STATUS_PENDING or bool(details.get("pending_reconciliation")):
        return OUTBOX_STATUS_PENDING
    if status in _TERMINAL_FILL_STATUSES:
        return OUTBOX_STATUS_TERMINAL_FILL
    if status in _TERMINAL_NON_FILL_STATUSES:
        return OUTBOX_STATUS_TERMINAL_NON_FILL
    return OUTBOX_STATUS_ACKED


def _outbox_status_from_terminal_status(status: str) -> str:
    status_upper = str(status or "").strip().upper()
    if status_upper in _TERMINAL_FILL_STATUSES:
        return OUTBOX_STATUS_TERMINAL_FILL
    return OUTBOX_STATUS_TERMINAL_NON_FILL


def _parse_json_payload(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


@dataclass(frozen=True)
class OrderOutboxRecord:
    tenant_id: str
    broker_account_id: str
    strategy_id: str
    submission_key: str
    hub_order_id: str
    status: str
    order_request: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    broker_order_id: Optional[str] = None
    broker_response: Optional[dict[str, Any]] = None
    submit_attempts: int = 0
    last_error: Optional[str] = None
    recovery_action: Optional[str] = None
    contract_key: Optional[dict[str, Any]] = None
    ownership_strategy_id: Optional[str] = None

    def as_order_request(self) -> OrderRequest:
        return _deserialize_order_request(dict(self.order_request))

    def as_order_response(self) -> Optional[OrderResponse]:
        if not self.broker_response:
            return None
        return _deserialize_order_response(dict(self.broker_response))

    def as_contract_key(self) -> Optional[ContractKey]:
        return _deserialize_contract_key(self.contract_key)


class InMemoryOrderSubmissionOutbox:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str, str, str], OrderOutboxRecord] = {}

    @staticmethod
    def _scope_key(
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
    ) -> tuple[str, str, str, str]:
        return (
            str(tenant_id),
            str(broker_account_id),
            str(strategy_id),
            str(submission_key),
        )

    def reserve_submission(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        hub_order_id: str,
        order_req: OrderRequest,
        contract_key: Optional[ContractKey],
        ownership_strategy_id: Optional[StrategyId],
        created_at: datetime,
    ) -> tuple[bool, OrderOutboxRecord]:
        key = self._scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                return False, existing
            record = OrderOutboxRecord(
                tenant_id=str(tenant_id),
                broker_account_id=str(broker_account_id),
                strategy_id=str(strategy_id),
                submission_key=str(submission_key),
                hub_order_id=str(hub_order_id),
                status=OUTBOX_STATUS_PENDING,
                order_request=_serialize_order_request(order_req),
                created_at=_to_utc(created_at),
                updated_at=_to_utc(created_at),
                contract_key=_serialize_contract_key(contract_key),
                ownership_strategy_id=(
                    str(ownership_strategy_id)
                    if ownership_strategy_id is not None
                    else None
                ),
            )
            self._records[key] = record
            return True, record

    def mark_submitting(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        updated_at: datetime,
    ) -> OrderOutboxRecord:
        key = self._scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        with self._lock:
            record = self._records[key]
            next_record = OrderOutboxRecord(
                **{
                    **record.__dict__,
                    "status": OUTBOX_STATUS_SUBMITTING,
                    "updated_at": _to_utc(updated_at),
                    "submit_attempts": int(record.submit_attempts or 0) + 1,
                    "last_error": None,
                }
            )
            self._records[key] = next_record
            return next_record

    def record_response(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        response: OrderResponse,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> OrderOutboxRecord:
        key = self._scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        with self._lock:
            record = self._records[key]
            next_record = OrderOutboxRecord(
                **{
                    **record.__dict__,
                    "status": _outbox_status_from_response(response),
                    "updated_at": _to_utc(updated_at),
                    "broker_order_id": str(response.broker_order_id or "").strip() or None,
                    "broker_response": _serialize_order_response(response),
                    "last_error": None,
                    "recovery_action": recovery_action,
                }
            )
            self._records[key] = next_record
            return next_record

    def record_submit_error(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        error_text: str,
        updated_at: datetime,
    ) -> OrderOutboxRecord:
        key = self._scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )
        with self._lock:
            record = self._records[key]
            next_record = OrderOutboxRecord(
                **{
                    **record.__dict__,
                    "status": OUTBOX_STATUS_SUBMITTING,
                    "updated_at": _to_utc(updated_at),
                    "last_error": str(error_text or ""),
                }
            )
            self._records[key] = next_record
            return next_record

    def mark_terminal_by_broker_order(
        self,
        *,
        broker_account_id: BrokerAccountId,
        broker_order_id: str,
        terminal_status: str,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> Optional[OrderOutboxRecord]:
        target_account = str(broker_account_id or "")
        target_order_id = str(broker_order_id or "").strip()
        with self._lock:
            for key, record in list(self._records.items()):
                if record.broker_account_id != target_account:
                    continue
                if str(record.broker_order_id or "").strip() != target_order_id:
                    continue
                next_record = OrderOutboxRecord(
                    **{
                        **record.__dict__,
                        "status": _outbox_status_from_terminal_status(terminal_status),
                        "updated_at": _to_utc(updated_at),
                        "recovery_action": recovery_action,
                    }
                )
                self._records[key] = next_record
                return next_record
        return None

    def list_active_submissions(
        self,
        *,
        limit: int = 500,
    ) -> list[OrderOutboxRecord]:
        effective_limit = max(1, int(limit))
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.status in _ACTIVE_OUTBOX_STATUSES
            ]
        records.sort(key=lambda item: item.created_at)
        return records[:effective_limit]


class PostgresOrderSubmissionOutbox:
    def __init__(self, *, dsn: str, table_name: str = "order_submission_outbox") -> None:
        self._dsn = dsn
        self._table = _safe_identifier(table_name, "order_submission_outbox")
        self._ensure_schema()

    def _conn(self):
        return connect_with_retry(self._dsn, autocommit=True)

    def _ensure_schema(self) -> None:
        sql = f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                tenant_id TEXT NOT NULL,
                broker_account_id TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                submission_key TEXT NOT NULL,
                hub_order_id TEXT NOT NULL,
                status TEXT NOT NULL,
                order_request_json JSONB NOT NULL,
                broker_response_json JSONB,
                broker_order_id TEXT,
                submit_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                recovery_action TEXT,
                contract_key_json JSONB,
                ownership_strategy_id TEXT,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (tenant_id, broker_account_id, strategy_id, submission_key)
            )
        """
        idx_sql = [
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_status ON {self._table} (status, updated_at)",
            f"CREATE INDEX IF NOT EXISTS idx_{self._table}_broker_order ON {self._table} (broker_account_id, broker_order_id)",
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                for statement in idx_sql:
                    cur.execute(statement)

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> OrderOutboxRecord:
        (
            tenant_id,
            broker_account_id,
            strategy_id,
            submission_key,
            hub_order_id,
            status,
            order_request_json,
            broker_response_json,
            broker_order_id,
            submit_attempts,
            last_error,
            recovery_action,
            contract_key_json,
            ownership_strategy_id,
            created_at,
            updated_at,
        ) = row
        return OrderOutboxRecord(
            tenant_id=str(tenant_id),
            broker_account_id=str(broker_account_id),
            strategy_id=str(strategy_id),
            submission_key=str(submission_key),
            hub_order_id=str(hub_order_id),
            status=str(status),
            order_request=_parse_json_payload(order_request_json),
            broker_response=(
                _parse_json_payload(broker_response_json)
                if broker_response_json is not None
                else None
            ),
            broker_order_id=str(broker_order_id).strip() or None if broker_order_id else None,
            submit_attempts=int(submit_attempts or 0),
            last_error=str(last_error) if last_error else None,
            recovery_action=str(recovery_action) if recovery_action else None,
            contract_key=(
                _parse_json_payload(contract_key_json)
                if contract_key_json is not None
                else None
            ),
            ownership_strategy_id=(
                str(ownership_strategy_id) if ownership_strategy_id else None
            ),
            created_at=_to_utc(created_at),
            updated_at=_to_utc(updated_at),
        )

    def _select_record(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
    ) -> OrderOutboxRecord:
        sql = f"""
            SELECT
                tenant_id,
                broker_account_id,
                strategy_id,
                submission_key,
                hub_order_id,
                status,
                order_request_json,
                broker_response_json,
                broker_order_id,
                submit_attempts,
                last_error,
                recovery_action,
                contract_key_json,
                ownership_strategy_id,
                created_at,
                updated_at
            FROM {self._table}
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND strategy_id = %(strategy_id)s
              AND submission_key = %(submission_key)s
        """
        payload = {
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "strategy_id": str(strategy_id),
            "submission_key": str(submission_key),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                row = cur.fetchone()
        if row is None:
            raise KeyError(payload)
        return self._row_to_record(row)

    def reserve_submission(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        hub_order_id: str,
        order_req: OrderRequest,
        contract_key: Optional[ContractKey],
        ownership_strategy_id: Optional[StrategyId],
        created_at: datetime,
    ) -> tuple[bool, OrderOutboxRecord]:
        ts = _to_utc(created_at)
        sql = f"""
            INSERT INTO {self._table} (
                tenant_id,
                broker_account_id,
                strategy_id,
                submission_key,
                hub_order_id,
                status,
                order_request_json,
                contract_key_json,
                ownership_strategy_id,
                created_at,
                updated_at
            ) VALUES (
                %(tenant_id)s,
                %(broker_account_id)s,
                %(strategy_id)s,
                %(submission_key)s,
                %(hub_order_id)s,
                %(status)s,
                %(order_request_json)s::jsonb,
                %(contract_key_json)s::jsonb,
                %(ownership_strategy_id)s,
                %(created_at)s,
                %(updated_at)s
            )
            ON CONFLICT (tenant_id, broker_account_id, strategy_id, submission_key) DO NOTHING
        """
        payload = {
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "strategy_id": str(strategy_id),
            "submission_key": str(submission_key),
            "hub_order_id": str(hub_order_id),
            "status": OUTBOX_STATUS_PENDING,
            "order_request_json": json.dumps(_serialize_order_request(order_req)),
            "contract_key_json": json.dumps(_serialize_contract_key(contract_key)),
            "ownership_strategy_id": (
                str(ownership_strategy_id)
                if ownership_strategy_id is not None
                else None
            ),
            "created_at": ts,
            "updated_at": ts,
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                created = cur.rowcount > 0
        return created, self._select_record(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )

    def mark_submitting(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        updated_at: datetime,
    ) -> OrderOutboxRecord:
        sql = f"""
            UPDATE {self._table}
            SET status = %(status)s,
                updated_at = %(updated_at)s,
                submit_attempts = submit_attempts + 1,
                last_error = NULL
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND strategy_id = %(strategy_id)s
              AND submission_key = %(submission_key)s
        """
        payload = {
            "status": OUTBOX_STATUS_SUBMITTING,
            "updated_at": _to_utc(updated_at),
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "strategy_id": str(strategy_id),
            "submission_key": str(submission_key),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
        return self._select_record(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )

    def record_response(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        response: OrderResponse,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> OrderOutboxRecord:
        sql = f"""
            UPDATE {self._table}
            SET status = %(status)s,
                updated_at = %(updated_at)s,
                broker_order_id = %(broker_order_id)s,
                broker_response_json = %(broker_response_json)s::jsonb,
                last_error = NULL,
                recovery_action = %(recovery_action)s
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND strategy_id = %(strategy_id)s
              AND submission_key = %(submission_key)s
        """
        payload = {
            "status": _outbox_status_from_response(response),
            "updated_at": _to_utc(updated_at),
            "broker_order_id": str(response.broker_order_id or "").strip() or None,
            "broker_response_json": json.dumps(_serialize_order_response(response)),
            "recovery_action": recovery_action,
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "strategy_id": str(strategy_id),
            "submission_key": str(submission_key),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
        return self._select_record(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )

    def record_submit_error(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        submission_key: str,
        error_text: str,
        updated_at: datetime,
    ) -> OrderOutboxRecord:
        sql = f"""
            UPDATE {self._table}
            SET status = %(status)s,
                updated_at = %(updated_at)s,
                last_error = %(last_error)s
            WHERE tenant_id = %(tenant_id)s
              AND broker_account_id = %(broker_account_id)s
              AND strategy_id = %(strategy_id)s
              AND submission_key = %(submission_key)s
        """
        payload = {
            "status": OUTBOX_STATUS_SUBMITTING,
            "updated_at": _to_utc(updated_at),
            "last_error": str(error_text or ""),
            "tenant_id": str(tenant_id),
            "broker_account_id": str(broker_account_id),
            "strategy_id": str(strategy_id),
            "submission_key": str(submission_key),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
        return self._select_record(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            submission_key=submission_key,
        )

    def mark_terminal_by_broker_order(
        self,
        *,
        broker_account_id: BrokerAccountId,
        broker_order_id: str,
        terminal_status: str,
        updated_at: datetime,
        recovery_action: Optional[str] = None,
    ) -> Optional[OrderOutboxRecord]:
        sql = f"""
            UPDATE {self._table}
            SET status = %(status)s,
                updated_at = %(updated_at)s,
                recovery_action = %(recovery_action)s
            WHERE broker_account_id = %(broker_account_id)s
              AND broker_order_id = %(broker_order_id)s
            RETURNING
                tenant_id,
                broker_account_id,
                strategy_id,
                submission_key,
                hub_order_id,
                status,
                order_request_json,
                broker_response_json,
                broker_order_id,
                submit_attempts,
                last_error,
                recovery_action,
                contract_key_json,
                ownership_strategy_id,
                created_at,
                updated_at
        """
        payload = {
            "status": _outbox_status_from_terminal_status(terminal_status),
            "updated_at": _to_utc(updated_at),
            "recovery_action": recovery_action,
            "broker_account_id": str(broker_account_id),
            "broker_order_id": str(broker_order_id or "").strip(),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_active_submissions(
        self,
        *,
        limit: int = 500,
    ) -> list[OrderOutboxRecord]:
        sql = f"""
            SELECT
                tenant_id,
                broker_account_id,
                strategy_id,
                submission_key,
                hub_order_id,
                status,
                order_request_json,
                broker_response_json,
                broker_order_id,
                submit_attempts,
                last_error,
                recovery_action,
                contract_key_json,
                ownership_strategy_id,
                created_at,
                updated_at
            FROM {self._table}
            WHERE status = ANY(%(statuses)s)
            ORDER BY created_at ASC
            LIMIT %(limit)s
        """
        payload = {
            "statuses": list(_ACTIVE_OUTBOX_STATUSES),
            "limit": max(1, int(limit)),
        }
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, payload)
                rows = cur.fetchall() or []
        return [self._row_to_record(row) for row in rows]


def force_terminal_outbox_by_symbol_pattern(
    *,
    symbol_pattern: str,
    terminal_status: str = OUTBOX_STATUS_TERMINAL_NON_FILL,
    recovery_action: str = "admin_expired_contract_cleanup",
    min_age_days: int = 7,
) -> int:
    """Force non-terminal outbox records whose symbol matches *symbol_pattern* to a terminal state.

    Intended for operational cleanup of expired-contract outbox records that can never
    be filled or reconciled. Returns the count of rows updated.
    """
    from app.data.postgres import connect_with_retry, get_control_plane_dsn
    table = "order_submission_outbox"
    dsn = get_control_plane_dsn()
    age_days = max(1, int(min_age_days))
    sql = f"""
        UPDATE {table}
        SET status = %(terminal_status)s,
            recovery_action = %(recovery_action)s,
            updated_at = NOW()
        WHERE status = ANY(%(active_statuses)s)
          AND order_request_json->>'symbol' ILIKE %(pattern)s
          AND created_at < NOW() - INTERVAL '{age_days} days'
    """
    with connect_with_retry(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, {
                "terminal_status": terminal_status,
                "recovery_action": recovery_action,
                "active_statuses": list(_ACTIVE_OUTBOX_STATUSES),
                "pattern": symbol_pattern,
            })
            return cur.rowcount or 0


def build_order_submission_outbox() -> OrderSubmissionOutbox:
    settings = get_settings()
    persist_enabled = not (
        str(os.getenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")).strip().lower()
        in {"0", "false", "no"}
    )
    trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    strict_default = trade_mode == "LIVE"
    strict_required = strict_default or _is_true(
        os.getenv(
            "ORDER_SUBMISSION_OUTBOX_REQUIRED",
            "false",
        )
    )
    if not persist_enabled:
        if strict_required:
            raise RuntimeError(
                "ORDER_SUBMISSION_OUTBOX_REQUIRED=true requires "
                "ORDER_SUBMISSION_OUTBOX_ENABLED=true"
            )
        return InMemoryOrderSubmissionOutbox()

    backend = str(
        os.getenv("ORDER_SUBMISSION_OUTBOX_BACKEND", settings.control_plane_backend)
        or ""
    ).strip().lower()
    if backend in {"postgres", "pg", "db", "database"}:
        table_name = os.getenv(
            "ORDER_SUBMISSION_OUTBOX_TABLE",
            "order_submission_outbox",
        )
        try:
            dsn = get_control_plane_dsn(settings)
            return PostgresOrderSubmissionOutbox(dsn=dsn, table_name=table_name)
        except Exception as exc:
            if strict_required:
                logger.error(
                    "Order submission outbox init failed backend=postgres strict=true error=%s",
                    exc,
                )
                raise RuntimeError(
                    f"Order submission outbox initialization failed "
                    f"(backend=postgres): {exc}"
                ) from exc
            logger.warning(
                "Order submission outbox falling back to in-memory (postgres init failed): %s",
                exc,
            )
            return InMemoryOrderSubmissionOutbox()

    if strict_required:
        raise RuntimeError(
            f"Order submission outbox backend unsupported: {backend or '(empty)'}"
        )
    return InMemoryOrderSubmissionOutbox()


__all__ = [
    "OUTBOX_STATUS_PENDING",
    "OUTBOX_STATUS_SUBMITTING",
    "OUTBOX_STATUS_ACKED",
    "OUTBOX_STATUS_TERMINAL_FILL",
    "OUTBOX_STATUS_TERMINAL_NON_FILL",
    "OrderOutboxRecord",
    "OrderSubmissionOutbox",
    "InMemoryOrderSubmissionOutbox",
    "PostgresOrderSubmissionOutbox",
    "build_order_submission_outbox",
]
