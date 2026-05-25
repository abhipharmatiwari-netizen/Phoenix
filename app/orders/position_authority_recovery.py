"""Broker-flat recovery helpers for internal position authority records."""

from __future__ import annotations

import ast
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.core.audit_log import emit_audit_event
from app.orders.position_ownership import (
    ContractKey,
    derive_contract_key_from_position,
    normalize_contract_key,
)

logger = logging.getLogger(__name__)

_QTY_EPSILON = 0.0001
_AUTO_RECOVERY_STATES = {
    "DEGRADED",
    "RECONCILING",
    "RECOVERY_PENDING",
}
_TERMINAL_ORDER_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "COMPLETE",
    "COMPLETED",
    "EXECUTED",
    "FILLED",
    "REJECTED",
    "FAILED",
    "ERROR",
    "EXPIRED",
}


def _position_field(position: object, key: str, default: object = None) -> object:
    if isinstance(position, dict):
        return position.get(key, default)
    return getattr(position, key, default)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_state_text(record: Any) -> str:
    state = getattr(record, "position_state", None)
    return str(getattr(state, "value", state) or "").strip().upper()


def _parse_contract_key_text(value: Any) -> ContractKey | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        return None
    if not isinstance(parsed, (tuple, list)) or len(parsed) < 5:
        return None
    try:
        return normalize_contract_key(
            ContractKey(
                underlying=str(parsed[0]),
                expiry=str(parsed[1]),
                strike=str(parsed[2]),
                option_right=str(parsed[3]),
                product_type=str(parsed[4]),
            )
        )
    except Exception:
        return None


def _contract_storage_key(
    contract_key: ContractKey | None,
) -> tuple[str, str, str, str, str] | None:
    if contract_key is None:
        return None
    try:
        return normalize_contract_key(contract_key).as_storage_key()
    except Exception:
        return None


def _position_quantity(position: Any) -> float | None:
    return _safe_float(
        _position_field(position, "quantity")
        or _position_field(position, "net_qty")
        or _position_field(position, "net_quantity")
        or _position_field(position, "netqty")
    )


def _position_contract_storage_key(
    position: Any,
) -> tuple[str, str, str, str, str] | None:
    explicit = _position_field(position, "contract_key")
    parsed = _parse_contract_key_text(explicit)
    if parsed is not None:
        return parsed.as_storage_key()
    derived, _reason = derive_contract_key_from_position(position)
    return _contract_storage_key(derived)


def _position_matches_contract(
    position: Any,
    *,
    record_contract_text: str,
    record_contract_key: ContractKey | None,
) -> bool:
    expected_key = _contract_storage_key(record_contract_key)
    actual_key = _position_contract_storage_key(position)
    if expected_key is not None and actual_key is not None:
        return actual_key == expected_key

    explicit = _position_field(position, "contract_key")
    explicit_text = str(explicit or "").strip()
    if record_contract_text and explicit_text:
        return explicit_text == record_contract_text or record_contract_text in explicit_text
    return False


def _sanitized_broker_position(position: Any) -> dict[str, Any]:
    return {
        "symbol": str(_position_field(position, "symbol") or ""),
        "product_type": str(_position_field(position, "product_type") or ""),
        "quantity": _position_quantity(position),
        "contract_key": str(_position_field(position, "contract_key") or ""),
    }


def broker_flat_evidence(state_store: Any, record: Any) -> dict[str, Any]:
    broker_account_id = str(getattr(record, "account_id", "") or "")
    record_contract_text = str(getattr(record, "contract_key", "") or "").strip()
    record_contract_key = _parse_contract_key_text(record_contract_text)
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        positions = state_store.get_positions(broker_account_id) or []
    except Exception as exc:
        return {
            "status": "unknown",
            "reason": "broker_positions_unavailable",
            "error": type(exc).__name__,
            "checked_at": checked_at,
            "matched_positions": 0,
            "net_qty": None,
        }

    matches = [
        position
        for position in positions
        if _position_matches_contract(
            position,
            record_contract_text=record_contract_text,
            record_contract_key=record_contract_key,
        )
    ]
    net_qty = sum(
        qty
        for qty in (_position_quantity(position) for position in matches)
        if qty is not None
    )
    flat = abs(float(net_qty or 0.0)) <= _QTY_EPSILON
    return {
        "status": "flat" if flat else "nonzero",
        "reason": "broker_position_flat" if flat else "broker_position_nonzero",
        "checked_at": checked_at,
        "matched_positions": len(matches),
        "net_qty": float(net_qty or 0.0),
        "positions": [_sanitized_broker_position(position) for position in matches],
    }


def _iter_lifecycle_position_records(lifecycle: Any) -> list[Any]:
    records_method = getattr(lifecycle, "list_position_records", None)
    if callable(records_method):
        return list(records_method())
    records = getattr(lifecycle, "_position_records", {})
    lock = getattr(lifecycle, "_recent_entry_lock", None)
    if lock is not None:
        with lock:
            return list(records.values())
    return list(records.values())


def _order_status_text(order: Any) -> str:
    if isinstance(order, dict):
        value = order.get("status") or order.get("order_status")
    else:
        value = getattr(order, "status", None) or getattr(order, "order_status", None)
    return str(getattr(value, "value", value) or "").strip().upper()


def _snapshot_age_limit_seconds(env_name: str, default_seconds: float) -> float:
    try:
        interval = float(os.getenv(env_name, str(default_seconds)) or default_seconds)
    except (TypeError, ValueError):
        interval = default_seconds
    return max(interval * 2.0, 120.0)


def _positions_snapshot_fresh(
    state_store: Any,
    broker_account_id: str,
) -> tuple[bool, str | None, datetime | None]:
    get_positions_status = getattr(state_store, "get_positions_status", None)
    if not callable(get_positions_status):
        return False, "positions_status_unavailable", None
    try:
        status = get_positions_status(broker_account_id) or {}
    except Exception as exc:
        return False, f"positions_status_unavailable:{type(exc).__name__}", None
    current_status = str(status.get("status") or "").strip().upper()
    if current_status != "OK":
        return False, "positions_snapshot_not_ok", None
    last_ok_iso = status.get("last_ok_ts")
    if not last_ok_iso:
        return False, "positions_snapshot_not_synced", None
    try:
        last_ok = datetime.fromisoformat(str(last_ok_iso).replace("Z", "+00:00"))
    except ValueError:
        return False, "positions_snapshot_timestamp_invalid", None
    if last_ok.tzinfo is None:
        last_ok = last_ok.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - last_ok).total_seconds()
    if age_seconds > _snapshot_age_limit_seconds("POSITION_SYNC_INTERVAL_SECONDS", 30.0):
        return False, "positions_snapshot_stale", last_ok
    return True, None, last_ok


def _orders_snapshot_fresh(
    state_store: Any,
    broker_account_id: str,
) -> tuple[bool, str | None, datetime | None]:
    get_orders_status = getattr(state_store, "get_orders_status", None)
    if not callable(get_orders_status):
        return False, "orders_status_unavailable", None
    try:
        status = get_orders_status(broker_account_id) or {}
    except Exception as exc:
        return False, f"orders_status_unavailable:{type(exc).__name__}", None
    last_ok_iso = status.get("orders_last_ok_ts")
    if not last_ok_iso:
        return False, "orders_snapshot_not_synced", None
    try:
        last_ok = datetime.fromisoformat(str(last_ok_iso).replace("Z", "+00:00"))
    except ValueError:
        return False, "orders_snapshot_timestamp_invalid", None
    if last_ok.tzinfo is None:
        last_ok = last_ok.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - last_ok).total_seconds()
    if age_seconds > _snapshot_age_limit_seconds("ORDERS_SYNC_INTERVAL_SECONDS", 90.0):
        return False, "orders_snapshot_stale", last_ok
    return True, None, last_ok


def _order_matches_record_contract(order: Any, record: Any) -> bool:
    record_contract_text = str(getattr(record, "contract_key", "") or "").strip()
    record_contract_key = _parse_contract_key_text(record_contract_text)
    expected_key = _contract_storage_key(record_contract_key)
    if expected_key is not None:
        try:
            actual_key = _position_contract_storage_key(order)
        except Exception:
            actual_key = None
        if actual_key is not None:
            return actual_key == expected_key

    order_symbol = str(_position_field(order, "symbol") or "").strip()
    if not order_symbol:
        return False
    if order_symbol == record_contract_text:
        return True
    return bool(record_contract_text and order_symbol in record_contract_text)


def _matching_active_orders(
    state_store: Any,
    record: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    broker_account_id = str(getattr(record, "account_id", "") or "")
    try:
        get_order_snapshot = getattr(state_store, "get_order_snapshot", None)
        if callable(get_order_snapshot):
            orders = list(get_order_snapshot(broker_account_id) or [])
        else:
            orders = []
        if not orders:
            get_orders = getattr(state_store, "get_orders", None)
            if callable(get_orders):
                orders = list(get_orders(broker_account_id) or [])
    except Exception as exc:
        return [], f"orders_unavailable:{type(exc).__name__}"

    active: list[dict[str, Any]] = []
    for order in orders:
        status_text = _order_status_text(order)
        if status_text in _TERMINAL_ORDER_STATUSES:
            continue
        if not _order_matches_record_contract(order, record):
            continue
        active.append(
            {
                "order_id": str(_position_field(order, "order_id") or ""),
                "symbol": str(_position_field(order, "symbol") or ""),
                "side": str(_position_field(order, "side") or ""),
                "status": status_text,
                "quantity": _position_field(order, "quantity"),
            }
        )
    return active, None


def _persist_flat_clear(
    *,
    prior: Any,
    scope_key: str,
    broker_account_id: str,
    reason: str,
) -> int:
    from app.data.postgres import connect_with_retry, get_control_plane_dsn

    ledger_deleted = 0
    with connect_with_retry(get_control_plane_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE internal_position_records "
                "SET position_state = 'FLAT', "
                "    net_qty = 0, "
                "    unrealized_pnl = 0, "
                "    state_reason = %s, "
                "    last_reconciled_at = NOW(), "
                "    updated_at = NOW() "
                "WHERE scope_key = %s",
                (reason, scope_key),
            )
            contract_key = _parse_contract_key_text(getattr(prior, "contract_key", ""))
            storage_key = _contract_storage_key(contract_key)
            if storage_key is not None:
                underlying, expiry, strike, option_right, _product_type = storage_key
                cur.execute(
                    "DELETE FROM position_ownership_ledger "
                    "WHERE broker_account_id = %s AND underlying = %s "
                    "AND expiry = %s AND strike = %s "
                    "AND option_right = %s",
                    (
                        broker_account_id,
                        underlying,
                        expiry,
                        strike,
                        option_right,
                    ),
                )
                ledger_deleted = int(cur.rowcount or 0)
        conn.commit()
    return ledger_deleted


def auto_recover_broker_flat_zero_qty_records(
    *,
    lifecycle: Any,
    state_store: Any,
    broker_account_id: Any | None = None,
    actor: str = "system:broker_flat_auto_recovery",
    reason: str = "broker_flat_auto_recovery",
    persist: bool = True,
) -> dict[str, Any]:
    """Clear stale zero-quantity authority records after flat broker evidence.

    This is intentionally conservative: it only clears records already in a
    recovery/degraded state, whose internal net quantity is zero, whose current
    broker snapshot is flat for the same contract, and whose current order
    snapshot has no active matching order.
    """

    if lifecycle is None or state_store is None:
        return {
            "status": "noop",
            "reason": "missing_lifecycle_or_state_store",
            "recovered": 0,
        }

    account_filter = str(broker_account_id or "").strip()
    recovered: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for record in _iter_lifecycle_position_records(lifecycle):
        scope_key = str(getattr(record, "ownership_key", "") or "")
        if not scope_key:
            continue
        record_account_id = str(getattr(record, "account_id", "") or "")
        if account_filter and record_account_id != account_filter:
            continue
        state_text = _position_state_text(record)
        if state_text not in _AUTO_RECOVERY_STATES:
            continue
        net_qty = _safe_float(getattr(record, "net_qty", None))
        if net_qty is None or abs(net_qty) > _QTY_EPSILON:
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": "internal_position_nonzero",
                    "position_state": state_text,
                    "net_qty": net_qty,
                }
            )
            continue

        positions_fresh, positions_fresh_reason, positions_checked_at = (
            _positions_snapshot_fresh(state_store, record_account_id)
        )
        if not positions_fresh:
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": positions_fresh_reason or "positions_snapshot_not_fresh",
                    "position_state": state_text,
                }
            )
            continue

        evidence = broker_flat_evidence(state_store, record)
        if evidence.get("status") != "flat":
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": evidence.get("reason", "broker_not_flat"),
                    "position_state": state_text,
                    "broker_evidence": evidence,
                }
            )
            continue

        orders_fresh, orders_fresh_reason, orders_checked_at = _orders_snapshot_fresh(
            state_store,
            record_account_id,
        )
        if not orders_fresh:
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": orders_fresh_reason or "orders_snapshot_not_fresh",
                    "position_state": state_text,
                }
            )
            continue
        if (
            positions_checked_at is not None
            and orders_checked_at is not None
            and orders_checked_at < positions_checked_at
        ):
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": "orders_snapshot_older_than_positions",
                    "position_state": state_text,
                }
            )
            continue

        active_orders, order_error = _matching_active_orders(state_store, record)
        if order_error is not None:
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": order_error,
                    "position_state": state_text,
                }
            )
            continue
        if active_orders:
            skipped.append(
                {
                    "scope_key": scope_key,
                    "reason": "active_matching_order",
                    "position_state": state_text,
                    "active_orders": active_orders,
                }
            )
            continue

        prior = lifecycle.force_clear_position_record(
            scope_key=scope_key,
            reason=reason,
        )
        if prior is None:
            skipped.append({"scope_key": scope_key, "reason": "record_disappeared"})
            continue

        ledger_deleted = 0
        persist_error: str | None = None
        if persist:
            try:
                ledger_deleted = _persist_flat_clear(
                    prior=prior,
                    scope_key=scope_key,
                    broker_account_id=record_account_id,
                    reason=reason,
                )
            except Exception as exc:  # noqa: BLE001
                persist_error = repr(exc)
                logger.warning(
                    "position_authority_auto_recovery persist failed scope=%s: %s",
                    scope_key,
                    exc,
                )
                emit_audit_event(
                    actor=actor,
                    action="broker_flat_auto_recovery_persist_failed",
                    resource_type="internal_position_record",
                    resource_id=scope_key,
                    after={"error": persist_error},
                )

        degraded_scope_recovered: bool | None = None
        degraded_scope_recovery_error: str | None = None
        try:
            from app.core.degraded_scope_manager import degraded_scope_manager

            degraded_scope_recovered = degraded_scope_manager.try_recover(
                scope_key=scope_key,
                ownership_key_valid=True,
                broker_evidence_fresh=True,
                lifecycle_resolved=True,
                position_state_clean=True,
                actor=actor,
                require_operator_approval=False,
                operator_approved=True,
            )
        except Exception as exc:  # noqa: BLE001
            degraded_scope_recovery_error = repr(exc)

        emit_audit_event(
            actor=actor,
            action="broker_flat_auto_recovery_clear_position_record",
            resource_type="internal_position_record",
            resource_id=scope_key,
            before={
                "position_state": state_text,
                "state_reason": str(getattr(prior, "state_reason", "") or ""),
                "side": str(getattr(prior, "side", "") or ""),
                "net_qty": _safe_float(getattr(prior, "net_qty", None)),
                "broker_account_id": record_account_id,
                "tenant_id": str(getattr(prior, "tenant_id", "") or ""),
                "contract_key": str(getattr(prior, "contract_key", "") or ""),
            },
            after={
                "position_state": "FLAT",
                "net_qty": 0,
                "state_reason": reason,
                "broker_evidence": evidence,
                "ledger_rows_deleted": ledger_deleted,
                "persist_error": persist_error,
                "degraded_scope_recovered": degraded_scope_recovered,
                "degraded_scope_recovery_error": degraded_scope_recovery_error,
            },
        )
        recovered.append(
            {
                "scope_key": scope_key,
                "prior_state": state_text,
                "broker_account_id": record_account_id,
                "broker_evidence": evidence,
                "ledger_rows_deleted": ledger_deleted,
                "persist_error": persist_error,
                "degraded_scope_recovered": degraded_scope_recovered,
                "degraded_scope_recovery_error": degraded_scope_recovery_error,
            }
        )

    status = "recovered" if recovered else "noop"
    return {
        "status": status,
        "recovered": len(recovered),
        "records": recovered,
        "skipped": skipped,
    }
