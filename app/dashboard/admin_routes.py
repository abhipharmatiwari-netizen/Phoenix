"""Admin CRUD endpoints for the control plane (A8 dashboard/admin)."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.config.settings import get_settings
from app.core.audit_log import emit_audit_event, list_audit_events as query_audit_events
from app.core.rate_limit_middleware import check_rate_limit
from app.dashboard.auth import AdminContext, AdminRole, get_admin_context
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.hub.hub import pick_active_subscription
from app.hub.exit_engines import PositionExitPlan, build_position_exit_plan
from app.hub.runtime import get_hub_runtime
from app.orders.position_ownership import ContractKey, normalize_contract_key
from app.orders.router import OrderRouter
from app.tenants.firestore_client import (
    get_broker_account,
    get_all_broker_accounts,
    get_all_tenants,
    get_subscriptions_for_account,
    get_tenant,
    upsert_broker_account,
    upsert_subscription,
    upsert_tenant,
)
from app.tenants.subscription_service import compute_account_runtime_mode
from app.tenants.models import BrokerAccountModel, SubscriptionModel, TenantModel

router = APIRouter(prefix="/admin", tags=["admin"])


# Payload for tenant upsert operations.
class TenantUpsertRequest(BaseModel):
    tenant_id: TenantId
    name: str
    email: str
    phone: Optional[str] = None
    status: str = "active"
    notes: Optional[str] = None


# Payload for broker account upsert operations.
class BrokerAccountUpsertRequest(BaseModel):
    broker_account_id: BrokerAccountId
    tenant_id: TenantId
    broker_type: str
    display_name: str
    client_code: str
    secret_ref: str
    trading_mode: str = "PAPER"
    enabled: bool = True
    default_strategies: list[StrategyId] = Field(default_factory=list)


# Payload for subscription upsert operations.
class SubscriptionUpsertRequest(BaseModel):
    subscription_id: str
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    mode: str
    start_at: datetime
    end_at: datetime


class AdminTestOrderRequest(BaseModel):
    tenant_id: str
    broker_account_id: str
    strategy_id: str
    symbol: str
    quantity: int
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    product_type: ProductType = ProductType.INTRADAY
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: float | None = None
    stop_price: float | None = None
    tag: str | None = None
    exchange: str | None = None
    symbol_token: str | None = None


class ManualSweepRequest(BaseModel):
    tenant_id: str
    broker_account_id: str
    reason: str


class ManualEodExitRequest(BaseModel):
    tenant_id: str
    broker_account_id: str
    reason: str


class BreakGlassFlattenRequest(BaseModel):
    """Payload for break-glass manual flatten (Architecture S1 rule 3-4).

    step_up_token is required in LIVE mode (ARCHITECTURE §15.4 / issue #113).
    The current repo contains the step-up token service but does not expose an
    HTTP issuer route. Operators must not use this endpoint for LIVE unless a
    valid token has been issued through an approved operator process.
    """
    tenant_id: str
    broker_account_id: str
    underlying: str
    expiry: str
    strike: str
    option_right: str
    product_type: str
    reason: str
    step_up_token: Optional[str] = None  # Required in LIVE; optional in PAPER/SHADOW


class ResolveOrphanReviewRequest(BaseModel):
    tenant_id: str
    broker_account_id: str
    underlying: str
    expiry: str
    strike: str
    option_right: str
    product_type: str
    decision: str  # ADOPT, FLATTEN, SUPPRESS, CONTINUE_OBSERVING
    reason: str


class ClearPositionRecordRequest(BaseModel):
    """Force-clear a stuck internal_position_records row to FLAT.

    Use ONLY after the broker side has been confirmed flat (manually squared
    or via reconciliation). The endpoint refuses by default if the broker
    still reports non-zero qty for the contract; pass force=True to override.
    """
    scope_key: str  # full scope key as stored in internal_position_records
    reason: str
    force: bool = False  # bypass the broker-flat safety check


class AuditEventListResponse(BaseModel):
    count: int
    events: list[dict[str, Any]] = Field(default_factory=list)


def _request_id_from_request(request: Request) -> str | None:
    return request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")


def _position_field(position: object, key: str, default: object = None) -> object:
    if isinstance(position, dict):
        return position.get(key, default)
    return getattr(position, key, default)


def _ownership_record_snapshot(record: Any) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "ownership_key": str(getattr(record, "ownership_key", "") or ""),
        "state": (
            getattr(getattr(record, "state", None), "value", None)
            or str(getattr(record, "state", "") or "")
        ),
        "state_reason": str(getattr(record, "state_reason", "") or ""),
        "owner_strategy_id": getattr(record, "owner_strategy_id", None),
        "authority_path": str(getattr(record, "authority_path", "") or ""),
        "break_glass_override_id": getattr(record, "break_glass_override_id", None),
    }


def _order_response_snapshot(response: Any) -> dict[str, Any]:
    return {
        "broker_order_id": str(getattr(response, "broker_order_id", "") or ""),
        "status": str(getattr(response, "status", "") or ""),
        "message": str(getattr(response, "message", "") or ""),
        "filled_quantity": int(getattr(response, "filled_quantity", 0) or 0),
        "average_price": getattr(response, "average_price", None),
        "requested_quantity": getattr(response, "requested_quantity", None),
        "execution_mode": getattr(response, "execution_mode", None),
        "virtual": getattr(response, "virtual", None),
        "details": getattr(response, "details", None),
    }


def _break_glass_position_snapshot(plan: PositionExitPlan) -> dict[str, Any]:
    return {
        "symbol": plan.position_symbol,
        "position_quantity": plan.position_quantity,
        "exit_side": plan.exit_side.value if plan.exit_side is not None else None,
        "requested_lots": plan.lots,
        "broker_units": plan.broker_units,
        "lot_size": plan.lot_size,
        "exchange": plan.exchange,
        "symbol_token": plan.symbol_token,
        "product_type": (
            plan.product_type.value if plan.product_type is not None else None
        ),
        "contract": plan.contract_text,
    }


def _resolve_break_glass_exit_plan(
    *,
    positions: list[object],
    contract_key: ContractKey,
    override_id: str,
    broker_account_id: str,
) -> tuple[Optional[PositionExitPlan], Optional[dict[str, Any]]]:
    matches: list[PositionExitPlan] = []
    matched_errors: list[PositionExitPlan] = []
    target_storage_key = contract_key.as_storage_key()

    for position in positions or []:
        try:
            signed_units = int(
                _position_field(position, "quantity")
                if _position_field(position, "quantity") is not None
                else _position_field(position, "netqty", 0)
            )
        except Exception:
            signed_units = 0
        if signed_units == 0:
            continue

        plan = build_position_exit_plan(
            position,  # type: ignore[arg-type]
            tag=f"break_glass:{override_id}",
            idempotency_key=f"break_glass_{override_id}",
            position_ownership_bypass=True,
            exit_reason="BREAK_GLASS",
            default_product_type=None,
            require_exchange=True,
            require_symbol_token=True,
            require_contract_key=True,
            position_id=str(_position_field(position, "position_id", "") or "") or None,
            account_id=str(broker_account_id),
            strategy_id="break_glass_flatten",
        )
        if (
            plan.contract_key is None
            or plan.contract_key.as_storage_key() != target_storage_key
        ):
            continue
        if plan.ok:
            matches.append(plan)
        else:
            matched_errors.append(plan)

    if len(matches) > 1:
        return None, {
            "status_code": status.HTTP_409_CONFLICT,
            "detail": {
                "error": "break_glass_position_ambiguous",
                "message": (
                    "Multiple live positions matched the requested contract; "
                    "break-glass flatten requires exactly one authoritative match."
                ),
                "contract": contract_key.as_log_text(),
                "match_count": len(matches),
            },
        }
    if len(matches) == 1:
        return matches[0], None
    if matched_errors:
        plan = matched_errors[0]
        return None, {
            "status_code": status.HTTP_409_CONFLICT,
            "detail": {
                "error": "break_glass_position_missing_runtime_fields",
                "message": (
                    "Matching live position is missing authoritative runtime fields "
                    "required for a real break-glass flatten."
                ),
                "contract": contract_key.as_log_text(),
                "reason": plan.reason,
                "position": _break_glass_position_snapshot(plan),
            },
        }
    return None, {
        "status_code": status.HTTP_404_NOT_FOUND,
        "detail": {
            "error": "break_glass_position_not_found",
            "message": "No live position matched the requested contract.",
            "contract": contract_key.as_log_text(),
        },
    }


# List all tenants.
@router.get("/tenants")
async def list_tenants(ctx: AdminContext = Depends(get_admin_context)):
    tenants = get_all_tenants()
    return {"count": len(tenants), "tenants": tenants}


# List all broker accounts.
@router.get("/broker-accounts")
async def list_broker_accounts(ctx: AdminContext = Depends(get_admin_context)):
    accounts = get_all_broker_accounts()
    return {"count": len(accounts), "broker_accounts": accounts}


# List active hub runners and their status.
@router.get("/runners")
async def list_runners(ctx: AdminContext = Depends(get_admin_context)):
    runtime = get_hub_runtime()
    hub = runtime.hub

    runner_ids = hub.list_runner_ids()
    items = []
    for broker_account_id in runner_ids:
        runner = hub.get_runner(broker_account_id)
        if runner is None:
            continue
        items.append(
            {
                "broker_account_id": broker_account_id,
                "tenant_id": runner.tenant_id,
                "runtime_mode": runner.runtime_mode,
                "is_running": runner.is_running,
            }
        )

    return {"count": len(items), "runners": items}


@router.get("/audit", response_model=AuditEventListResponse)
def list_audit_log(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    actor: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    ctx: AdminContext = Depends(get_admin_context),
) -> AuditEventListResponse:
    ctx.require_role(AdminRole.READONLY)
    check_rate_limit(request)
    events = query_audit_events(
        limit=limit,
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return AuditEventListResponse(count=len(events), events=events)


# Create or update a tenant record.
@router.post("/tenants")
def create_or_update_tenant(
    request: Request,
    req: TenantUpsertRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> TenantModel:
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)
    now = datetime.now(timezone.utc)
    before_model = get_tenant(req.tenant_id)
    model = TenantModel(
        tenant_id=req.tenant_id,
        name=req.name,
        email=req.email,
        phone=req.phone,
        status=req.status,
        notes=req.notes,
        updated_at=now,
        created_at=now,
    )
    result = upsert_tenant(model)
    emit_audit_event(
        actor=ctx.caller,
        action="upsert_tenant",
        resource_type="tenant",
        resource_id=str(req.tenant_id),
        before=before_model,
        after=result,
        request_id=_request_id_from_request(request),
    )
    return result


# Create or update a broker account record.
@router.post("/broker-accounts")
def create_or_update_broker_account(
    request: Request,
    req: BrokerAccountUpsertRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> BrokerAccountModel:
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)
    now = datetime.now(timezone.utc)
    before_model = get_broker_account(req.broker_account_id)
    model = BrokerAccountModel(
        broker_account_id=req.broker_account_id,
        tenant_id=req.tenant_id,
        broker_type=req.broker_type,
        display_name=req.display_name,
        client_code=req.client_code,
        secret_ref=req.secret_ref,
        trading_mode=req.trading_mode,
        enabled=req.enabled,
        default_strategies=req.default_strategies,
        created_at=now,
        updated_at=now,
    )
    result = upsert_broker_account(model)
    emit_audit_event(
        actor=ctx.caller,
        action="upsert_broker_account",
        resource_type="broker_account",
        resource_id=str(req.broker_account_id),
        before=before_model,
        after=result,
        request_id=_request_id_from_request(request),
    )
    return result


# Create or update a subscription record.
@router.post("/subscriptions")
def create_or_update_subscription(
    request: Request,
    req: SubscriptionUpsertRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> SubscriptionModel:
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)
    now = datetime.now(timezone.utc)
    before_model = next(
        (
            sub
            for sub in get_subscriptions_for_account(req.broker_account_id)
            if str(sub.subscription_id) == req.subscription_id
        ),
        None,
    )
    model = SubscriptionModel(
        subscription_id=req.subscription_id,
        tenant_id=req.tenant_id,
        broker_account_id=req.broker_account_id,
        mode=req.mode,
        start_at=req.start_at,
        end_at=req.end_at,
        created_at=now,
        updated_at=now,
    )
    result = upsert_subscription(model)
    emit_audit_event(
        actor=ctx.caller,
        action="upsert_subscription",
        resource_type="subscription",
        resource_id=req.subscription_id,
        before=before_model,
        after=result,
        request_id=_request_id_from_request(request),
    )
    return result


@router.post("/test-order")
async def admin_test_order(
    request: Request,
    payload: AdminTestOrderRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)
    settings = get_settings()
    if not settings.enable_multi_hub:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multi-hub must be enabled for test-order.",
        )

    broker_account = get_broker_account(payload.broker_account_id)
    if broker_account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found.",
        )
    if str(broker_account.tenant_id) != str(payload.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant/account mismatch.",
        )

    subscriptions = get_subscriptions_for_account(payload.broker_account_id)
    active_subscription = pick_active_subscription(subscriptions)
    runtime_mode = compute_account_runtime_mode(broker_account, active_subscription)
    if runtime_mode != "PAPER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Test-order only allowed in PAPER mode (current mode={runtime_mode}).",
        )

    runtime = get_hub_runtime()
    order_router: OrderRouter = runtime.order_router
    order_req = OrderRequest(
        symbol=payload.symbol,
        quantity=payload.quantity,
        side=payload.side,
        order_type=payload.order_type,
        product_type=payload.product_type,
        time_in_force=payload.time_in_force,
        limit_price=payload.limit_price,
        stop_price=payload.stop_price,
        tag=payload.tag,
        exchange=payload.exchange,
        symbol_token=payload.symbol_token,
    )
    hub_order_id, response = await order_router.submit_order(
        tenant_id=payload.tenant_id,
        broker_account_id=payload.broker_account_id,
        strategy_id=payload.strategy_id,
        order_req=order_req,
    )
    emit_audit_event(
        actor=ctx.caller,
        action="admin_test_order",
        resource_type="order",
        resource_id=str(hub_order_id),
        after={
            "tenant_id": payload.tenant_id,
            "broker_account_id": payload.broker_account_id,
            "strategy_id": payload.strategy_id,
            "symbol": payload.symbol,
            "quantity": payload.quantity,
            "side": str(payload.side),
            "response": response,
        },
        request_id=_request_id_from_request(request),
    )
    return {"hub_order_id": hub_order_id, "response": response}


# Trigger a manual profit sweep for a given tenant/account.
@router.post("/manual-sweep")
def manual_sweep(
    request: Request,
    payload: ManualSweepRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)

    broker_account = get_broker_account(payload.broker_account_id)
    if broker_account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found.",
        )
    if str(broker_account.tenant_id) != str(payload.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant/account mismatch.",
        )

    runtime = get_hub_runtime()
    sweep_engine = getattr(runtime, "sweep_engine", None)
    if sweep_engine is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Sweep engine not available on this runtime.",
        )

    try:
        result = sweep_engine.trigger_sweep(
            tenant_id=payload.tenant_id,
            broker_account_id=payload.broker_account_id,
            reason=payload.reason,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sweep failed: {exc}",
        ) from exc

    emit_audit_event(
        actor=ctx.caller,
        action="manual_sweep",
        resource_type="broker_account",
        resource_id=str(payload.broker_account_id),
        after={
            "tenant_id": payload.tenant_id,
            "broker_account_id": payload.broker_account_id,
            "reason": payload.reason,
            "result": result,
        },
        request_id=_request_id_from_request(request),
    )
    return {"status": "ok", "result": result}


# Force-clear a stuck internal_position_records row to FLAT.
#
# Designed for the recovery scenario surfaced by the 2026-05-07 A1 incident:
# a flip-the-trade single fill parked the record in RECOVERY_PENDING and the
# entire account runner couldn't drain. Operators previously had to do a
# stop-backend / hand-edit-Postgres / start-backend dance documented in
# ops/cleanup_a1_stuck_state_20260507.sql; this endpoint replaces that with
# an audited, rate-limited HTTP action.
#
# Safety:
#  - Requires OPERATOR role.
#  - Refuses by default if Phoenix's view of broker positions still shows
#    a non-zero net qty for that contract (operator must square broker side
#    first). Pass force=True to override; force=True is recorded in audit.
#  - Mutates only the in-memory record + the matching position_ownership_ledger
#    row. The record's persisted state will reflect FLAT on the next periodic
#    save_position_records flush, OR immediately via a parallel UPDATE here.
@router.post("/state/clear-position-record")
def clear_position_record(
    request: Request,
    payload: ClearPositionRecordRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)

    runtime = get_hub_runtime()
    lifecycle = getattr(runtime, "order_lifecycle", None)
    if lifecycle is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Order lifecycle service not available on this runtime.",
        )

    # Look up the record so we can derive the broker_account_id for the
    # safety check and emit a complete audit "before" snapshot.
    prior = lifecycle.get_position_record(scope_key=payload.scope_key)
    if prior is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No internal position record for scope_key={payload.scope_key!r}.",
        )

    broker_account_id = str(prior.account_id or "")
    contract_key_text = str(prior.contract_key or "")

    # Safety check: broker must be flat for this contract unless force=True.
    broker_net_qty: float | None = None
    if not payload.force:
        try:
            positions = runtime.state_store.get_positions(broker_account_id) or []
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Could not read broker positions for safety check: {exc}",
            ) from exc
        for pos in positions:
            sym = str(_position_field(pos, "symbol") or "")
            ctx_text = repr(getattr(pos, "contract_key", None)) if hasattr(pos, "contract_key") else ""
            if contract_key_text and (contract_key_text in ctx_text or contract_key_text == ctx_text):
                qty = _position_field(pos, "quantity")
                try:
                    broker_net_qty = float(qty or 0)
                except (TypeError, ValueError):
                    broker_net_qty = None
                if broker_net_qty and abs(broker_net_qty) > 0.0001:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Broker still reports net_qty={broker_net_qty} for {sym} "
                            f"under scope {payload.scope_key!r}. Square the broker side "
                            "first or pass force=true to override."
                        ),
                    )

    cleared = lifecycle.force_clear_position_record(
        scope_key=payload.scope_key,
        reason=payload.reason or "force_cleared_by_admin",
    )
    if cleared is None:
        # Lost a race: record disappeared between get_position_record() and
        # force_clear_position_record() — treat as already-cleared.
        return {"status": "noop", "reason": "record_disappeared_between_lookup_and_clear"}

    # Best-effort: also delete the matching position_ownership_ledger row
    # so the next reconcile cycle starts from a clean slate. Failures here
    # are non-fatal — the operator can re-run cleanup if needed.
    ledger_deleted = 0
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn

        with connect_with_retry(get_control_plane_dsn()) as conn:
            # Persist the FLAT record immediately so a subsequent restart
            # cannot resurrect the prior state via load_position_records.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE internal_position_records "
                    "SET position_state = 'FLAT', "
                    "    state_reason = %s, "
                    "    last_reconciled_at = NOW(), "
                    "    updated_at = NOW() "
                    "WHERE scope_key = %s",
                    (payload.reason or "force_cleared_by_admin", payload.scope_key),
                )
                # Drop the matching ownership ledger row if we can derive it
                # from the position record's contract metadata. The contract
                # key is stored as a tuple repr like "('NG', '2026-05-22',
                # '255', 'CE', 'INTRADAY')" — we parse it loosely.
                contract_text = str(prior.contract_key or "").strip()
                if contract_text.startswith("(") and contract_text.endswith(")"):
                    try:
                        parts = [
                            p.strip().strip("'").strip('"')
                            for p in contract_text[1:-1].split(",")
                        ]
                        if len(parts) >= 5:
                            cur.execute(
                                "DELETE FROM position_ownership_ledger "
                                "WHERE broker_account_id = %s AND underlying = %s "
                                "AND expiry = %s AND strike = %s "
                                "AND option_right = %s",
                                (broker_account_id, parts[0], parts[1], parts[2], parts[3]),
                            )
                            ledger_deleted = cur.rowcount or 0
                    except Exception:  # noqa: BLE001
                        # Parse failure is non-fatal.
                        pass
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Persistence failure does not roll back the in-memory clear; the
        # next periodic save_position_records cycle will sync FLAT to DB.
        # We surface it in the audit event so operators can re-run if needed.
        emit_audit_event(
            actor=ctx.caller,
            action="clear_position_record_persist_failed",
            resource_type="internal_position_record",
            resource_id=payload.scope_key,
            after={"error": repr(exc)},
            request_id=_request_id_from_request(request),
        )

    emit_audit_event(
        actor=ctx.caller,
        action="clear_position_record",
        resource_type="internal_position_record",
        resource_id=payload.scope_key,
        before={
            "position_state": prior.position_state.value if hasattr(prior.position_state, "value") else str(prior.position_state),
            "state_reason": prior.state_reason,
            "side": prior.side,
            "net_qty": prior.net_qty,
            "filled_qty_open": prior.filled_qty_open,
            "filled_qty_close": prior.filled_qty_close,
            "broker_account_id": broker_account_id,
            "tenant_id": prior.tenant_id,
            "contract_key": prior.contract_key,
        },
        after={
            "position_state": "FLAT",
            "state_reason": payload.reason or "force_cleared_by_admin",
            "force": payload.force,
            "broker_net_qty_at_clear": broker_net_qty,
            "ledger_rows_deleted": ledger_deleted,
        },
        request_id=_request_id_from_request(request),
    )

    return {
        "status": "ok",
        "scope_key": payload.scope_key,
        "prior_state": prior.position_state.value if hasattr(prior.position_state, "value") else str(prior.position_state),
        "ledger_rows_deleted": ledger_deleted,
    }


# Trigger manual EOD exit for a given tenant/account (elevated admin only).
@router.post("/manual-eod-exit")
def manual_eod_exit(
    request: Request,
    payload: ManualEodExitRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)

    broker_account = get_broker_account(payload.broker_account_id)
    if broker_account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found.",
        )
    if str(broker_account.tenant_id) != str(payload.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant/account mismatch.",
        )

    runtime = get_hub_runtime()
    eod_engine = getattr(runtime, "eod_engine", None)
    if eod_engine is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="EOD engine not available on this runtime.",
        )

    try:
        result = eod_engine.trigger_eod_exit(
            tenant_id=payload.tenant_id,
            broker_account_id=payload.broker_account_id,
            reason=payload.reason,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"EOD exit failed: {exc}",
        ) from exc

    emit_audit_event(
        actor=ctx.caller,
        action="manual_eod_exit",
        resource_type="broker_account",
        resource_id=str(payload.broker_account_id),
        after={
            "tenant_id": payload.tenant_id,
            "broker_account_id": payload.broker_account_id,
            "reason": payload.reason,
            "result": result,
        },
        request_id=_request_id_from_request(request),
    )
    return {"status": "ok", "result": result}


# Break-glass manual flatten endpoint (Architecture S1 rules 3-4, C2).
@router.post("/break-glass/flatten")
def break_glass_flatten(
    request: Request,
    payload: BreakGlassFlattenRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    """Emergency break-glass flatten for a single contract.

    Requires ADMIN auth. Acquires scope lock at BREAK_GLASS priority,
    resolves the live contract from authoritative runtime state, submits a real
    EXIT order through OrderRouter, records break_glass_override_id on the
    ownership record, and emits full audit trail.
    """
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)

    if not payload.reason or not payload.reason.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reason is required for break-glass operations.",
        )

    # §113 / ARCHITECTURE §15.4: Require a step-up token in LIVE mode.
    # Break-glass flatten bypasses ownership policy (position_ownership_bypass=True),
    # so re-authentication via a short-lived step-up token is mandatory in LIVE.
    import os as _os
    _trade_mode = str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    if _trade_mode == "LIVE":
        if not payload.step_up_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is required for break-glass operations in LIVE mode. "
                    "The current repo contains the step-up token service but no HTTP "
                    "issuer route; use only an approved operator-issued BREAK_GLASS token."
                ),
            )
        from app.security.step_up import DangerousActionClass, consume_step_up_token
        token_valid = consume_step_up_token(
            token_id=payload.step_up_token,
            actor=ctx.caller,
            action_class=DangerousActionClass.BREAK_GLASS,
        )
        if not token_valid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is invalid, expired, already used, or was not issued "
                    "to the current actor. Obtain a new approved BREAK_GLASS step-up token "
                    "before retrying."
                ),
            )

    try:
        contract_key = normalize_contract_key(
            ContractKey(
                underlying=payload.underlying,
                expiry=payload.expiry,
                strike=payload.strike,
                option_right=payload.option_right,
                product_type=payload.product_type,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    runtime = get_hub_runtime()
    store = runtime.position_ownership_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Position ownership store not available on this runtime.",
        )

    state_store = getattr(runtime, "state_store", None)
    if state_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authoritative runtime state store not available on this runtime.",
        )

    order_router: OrderRouter = runtime.order_router
    if order_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Order router not available on this runtime.",
        )
    hub = runtime.hub
    runner = hub.get_runner(payload.broker_account_id) if hub else None
    if runner is None or not bool(getattr(runner, "is_running", False)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active runner for broker_account_id={payload.broker_account_id}.",
        )

    request_id = _request_id_from_request(request) or ""
    override_id = f"break_glass_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{ctx.caller}"

    from app.orders.scope_serializer import (
        MutationPriority,
        ScopedMutation,
        scope_serializer,
    )

    # Execute through scope serializer at BREAK_GLASS priority
    from app.orders.position_ownership import derive_ownership_key
    ownership_key_obj = derive_ownership_key(
        tenant_id=payload.tenant_id,
        account_id=payload.broker_account_id,
        broker_account_id=payload.broker_account_id,
        contract_key=contract_key,
    )
    ownership_key = str(ownership_key_obj.as_scope_key())

    def _execute_break_glass_flatten() -> dict[str, Any]:
        before_record = store.get_ownership_record(
            tenant_id=payload.tenant_id,
            broker_account_id=payload.broker_account_id,
            contract_key=contract_key,
        )
        before_state = {
            "tenant_id": payload.tenant_id,
            "broker_account_id": payload.broker_account_id,
            "contract": contract_key.as_log_text(),
            "ownership": _ownership_record_snapshot(before_record),
        }

        try:
            positions = list(state_store.get_positions(payload.broker_account_id) or [])
        except Exception as exc:
            return {
                "before": before_state,
                "after": {
                    "error": "break_glass_runtime_state_unavailable",
                    "message": f"Authoritative runtime positions unavailable: {exc}",
                    "contract": contract_key.as_log_text(),
                },
                "error_status_code": status.HTTP_503_SERVICE_UNAVAILABLE,
            }

        exit_plan, resolution_error = _resolve_break_glass_exit_plan(
            positions=positions,
            contract_key=contract_key,
            override_id=override_id,
            broker_account_id=payload.broker_account_id,
        )
        if resolution_error is not None or exit_plan is None or exit_plan.order_req is None:
            return {
                "before": before_state,
                "after": (
                    resolution_error["detail"]
                    if resolution_error is not None
                    else {
                        "error": "break_glass_position_resolution_failed",
                        "message": "Could not resolve a real routed exit plan.",
                        "contract": contract_key.as_log_text(),
                    }
                ),
                "error_status_code": (
                    resolution_error["status_code"]
                    if resolution_error is not None
                    else status.HTTP_409_CONFLICT
                ),
            }

        store.record_break_glass_override(
            tenant_id=payload.tenant_id,
            broker_account_id=payload.broker_account_id,
            contract_key=contract_key,
            actor="admin_breakglass",
            override_id=override_id,
            reason=payload.reason,
            request_id=request_id or override_id,
        )

        try:
            hub_order_id, response = asyncio.run(
                order_router.submit_order(
                    tenant_id=payload.tenant_id,
                    broker_account_id=payload.broker_account_id,
                    strategy_id=StrategyId("break_glass_flatten"),
                    order_req=exit_plan.order_req,
                )
            )
        except Exception as exc:
            ownership_record = (
                store.record_break_glass_override(
                    tenant_id=payload.tenant_id,
                    broker_account_id=payload.broker_account_id,
                    contract_key=contract_key,
                    actor="admin_breakglass",
                    override_id=override_id,
                    reason=payload.reason,
                    request_id=request_id or override_id,
                )
                or store.get_ownership_record(
                    tenant_id=payload.tenant_id,
                    broker_account_id=payload.broker_account_id,
                    contract_key=contract_key,
                )
            )
            return {
                "before": before_state,
                "after": {
                    "error": "break_glass_router_submission_failed",
                    "message": f"Break-glass routed exit failed: {exc}",
                    "override_id": override_id,
                    "ownership_key": ownership_key,
                    "tenant_id": payload.tenant_id,
                    "broker_account_id": payload.broker_account_id,
                    "contract": contract_key.as_log_text(),
                    "reason": payload.reason,
                    "exit_reason": "BREAK_GLASS",
                    "position": _break_glass_position_snapshot(exit_plan),
                    "ownership": _ownership_record_snapshot(ownership_record),
                },
                "error_status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
            }

        ownership_record = (
            store.record_break_glass_override(
                tenant_id=payload.tenant_id,
                broker_account_id=payload.broker_account_id,
                contract_key=contract_key,
                actor="admin_breakglass",
                override_id=override_id,
                reason=payload.reason,
                request_id=request_id or override_id,
            )
            or store.get_ownership_record(
                tenant_id=payload.tenant_id,
                broker_account_id=payload.broker_account_id,
                contract_key=contract_key,
            )
        )
        order_snapshot = _order_response_snapshot(response)
        order_status = str(order_snapshot.get("status", "") or "").upper()
        after_state = {
            "status": "break_glass_flatten_submitted",
            "submitted": order_status not in {"REJECTED", "FAILED", "ERROR"},
            "override_id": override_id,
            "ownership_key": ownership_key,
            "tenant_id": payload.tenant_id,
            "broker_account_id": payload.broker_account_id,
            "contract": contract_key.as_log_text(),
            "reason": payload.reason,
            "exit_reason": "BREAK_GLASS",
            "hub_order_id": hub_order_id,
            "order": order_snapshot,
            "position": _break_glass_position_snapshot(exit_plan),
            "ownership": _ownership_record_snapshot(ownership_record),
        }
        return {"before": before_state, "after": after_state}

    mutation = ScopedMutation(
        ownership_key=ownership_key,
        priority=MutationPriority.BREAK_GLASS,
        mutation_fn=_execute_break_glass_flatten,
        request_id=request_id or override_id,
        actor="admin_breakglass",
    )

    try:
        result = scope_serializer.execute(mutation)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Break-glass flatten failed: {exc}",
        ) from exc

    after_state = dict(result.get("after") or {})
    emit_audit_event(
        actor=ctx.caller,
        action="break_glass_flatten",
        resource_type="position",
        resource_id=ownership_key,
        before=result.get("before"),
        after=after_state,
        request_id=request_id,
        metadata={
            "override_id": override_id,
            "reason": payload.reason,
            "exit_reason": "BREAK_GLASS",
        },
    )

    error_status_code = result.get("error_status_code")
    if error_status_code is not None:
        raise HTTPException(
            status_code=int(error_status_code),
            detail=after_state,
        )
    if not bool(after_state.get("submitted", False)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=after_state,
        )

    return after_state


# Resolve an ORPHAN_REVIEW position via operator decision (Architecture S11.5).
@router.post("/resolve-orphan-review")
def resolve_orphan_review(
    request: Request,
    payload: ResolveOrphanReviewRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)

    contract_key = ContractKey(
        underlying=payload.underlying,
        expiry=payload.expiry,
        strike=payload.strike,
        option_right=payload.option_right,
        product_type=payload.product_type,
    )

    runtime = get_hub_runtime()
    store = runtime.position_ownership_store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Position ownership store not available on this runtime.",
        )

    try:
        result = store.resolve_orphan_review(
            tenant_id=payload.tenant_id,
            broker_account_id=payload.broker_account_id,
            contract_key=contract_key,
            decision=payload.decision,
            actor=ctx.caller,
            reason=payload.reason,
            request_id=_request_id_from_request(request) or "",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return result


class ResolveExpiredContractsRequest(BaseModel):
    symbol_pattern: str = Field(..., description="ILIKE pattern, e.g. '%MAR26%' or 'NIFTY%FEB26%'")
    min_age_days: int = Field(default=7, ge=1, description="Only records older than this many days")
    dry_run: bool = Field(default=True, description="Set false to actually update rows")


@router.post("/resolve-expired-contracts")
def resolve_expired_contracts(
    request: Request,
    payload: ResolveExpiredContractsRequest,
    ctx: AdminContext = Depends(get_admin_context),
):
    """Force non-terminal outbox records for expired contracts to TERMINAL_NON_FILL.

    Use this to clean up DEGRADED/deferred outbox records for option contracts that
    expired in a prior month and can never be filled or reconciled.
    """
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)

    if payload.dry_run:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        from app.orders.order_outbox import _ACTIVE_OUTBOX_STATUSES
        table = "order_submission_outbox"
        age_days = max(1, int(payload.min_age_days))
        sql = f"""
            SELECT COUNT(*) FROM {table}
            WHERE status = ANY(%(active_statuses)s)
              AND order_request_json->>'symbol' ILIKE %(pattern)s
              AND created_at < NOW() - INTERVAL '{age_days} days'
        """
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    "active_statuses": list(_ACTIVE_OUTBOX_STATUSES),
                    "pattern": payload.symbol_pattern,
                })
                row = cur.fetchone()
        count = int(row[0]) if row else 0
        return {"dry_run": True, "would_update": count, "symbol_pattern": payload.symbol_pattern}

    from app.orders.order_outbox import force_terminal_outbox_by_symbol_pattern
    updated = force_terminal_outbox_by_symbol_pattern(
        symbol_pattern=payload.symbol_pattern,
        min_age_days=payload.min_age_days,
    )
    emit_audit_event(
        actor=ctx.caller,
        action="resolve_expired_contracts",
        resource_type="outbox",
        resource_id=str(payload.symbol_pattern),
        metadata={
            "symbol_pattern": payload.symbol_pattern,
            "min_age_days": payload.min_age_days,
            "rows_updated": updated,
        },
    )
    return {"dry_run": False, "updated": updated, "symbol_pattern": payload.symbol_pattern}


def _get_kill_switch_manager():
    """Return the live KillSwitchManager from the hub runtime. Raises 503 if unavailable."""
    try:
        ksm = getattr(get_hub_runtime(), "kill_switch_manager", None)
        if ksm is None:
            raise HTTPException(status_code=503, detail="KillSwitchManager not available on hub runtime")
        return ksm
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"KillSwitchManager unavailable: {exc}") from exc


def _save_kill_switch_state(ksm) -> None:
    """Persist KillSwitchManager state to Postgres.

    Issue #238 acceptance: in LIVE mode the kill-switch state MUST be
    persisted durably. A silent in-memory-only save would leave the
    operator's UI showing TRIPPED while a process restart would
    rehydrate from Postgres and find INACTIVE — defeating the
    durability guarantee. Fail closed in LIVE so the API call surfaces
    a 500 to the dashboard instead of misleading the operator.

    In non-LIVE modes the existing non-fatal warning is preserved so
    local/dev runs without a control-plane Postgres do not crash.
    """
    import logging as _log
    import os as _os
    trade_mode = str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        with connect_with_retry(get_control_plane_dsn(), autocommit=True) as conn:
            ksm.save_state(conn)
    except Exception as exc:
        if trade_mode == "LIVE":
            _log.getLogger(__name__).error(
                "kill_switch.save_state failed in LIVE — failing closed "
                "so the operator does not see a phantom toggle: %s", exc,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "Kill-switch state could not be persisted to Postgres. "
                    "The toggle has NOT taken durable effect. Resolve the "
                    "control-plane outage and retry. "
                    f"Underlying error: {exc}"
                ),
            ) from exc
        _log.getLogger(__name__).warning("kill_switch.save_state failed (non-fatal): %s", exc)


class KillSwitchTripRequest(BaseModel):
    scope: str = Field(..., description="GLOBAL | TENANT | ACCOUNT | STRATEGY")
    scope_id: str = Field(..., description="Scope identifier (e.g. 'GLOBAL', tenant-id, account-id)")
    reason: str = Field(..., description="Human-readable reason for tripping the kill switch")
    # Issue #220: HARD trip vs SOFT trip selector.
    # SOFT trip (default, block_exits=False): block new entries; exit
    # orders that reduce exposure remain allowed. Used by the legacy
    # daily-loss / drawdown auto-trip path so trailing-lock and operator
    # manual flatten can still close exposure.
    # HARD trip (block_exits=True): block ALL orders including exits.
    # Operator-initiated panic stop. Operator must manually flatten
    # broker-side exposure (Phoenix will not auto-exit).
    block_exits: bool = Field(
        False,
        description=(
            "Issue #220. False (default, SOFT trip) blocks new entries only; "
            "exit orders remain allowed. True (HARD trip) blocks ALL orders "
            "including exits — use only for operator-initiated panic stops."
        ),
    )


class KillSwitchClearRequestPayload(BaseModel):
    scope: str
    scope_id: str
    reason_code: str = Field(..., description="Short reason code for the clear request")
    break_glass: bool = Field(False, description="True to bypass pre-clear validation")


class KillSwitchRearmRequest(BaseModel):
    scope: str
    scope_id: str
    step_up_token: Optional[str] = Field(
        None,
        description=(
            "Required in LIVE mode. Obtain via POST /admin/step-up/issue "
            "with action_class=kill_switch_rearm before calling this endpoint."
        ),
    )


class StepUpIssueRequest(BaseModel):
    action_class: str = Field(
        ...,
        description=(
            "Dangerous action class for which the token is issued. "
            "Allowed values: kill_switch_rearm, kill_switch_clear, "
            "break_glass, strategy_enable, strategy_disable, "
            "capital_limit_change, user_promote, config_change."
        ),
    )
    resource_id: str = Field(
        "",
        description="Optional resource scoping (e.g. scope_id for kill switch, contract for break-glass).",
    )


@router.post("/kill-switch/trip")
def kill_switch_trip(
    payload: KillSwitchTripRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Trip a kill switch for a given scope. Requires OPERATOR role."""
    ctx.require_role(AdminRole.OPERATOR)
    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    upgraded = False
    try:
        record = ksm.trip(
            scope,
            payload.scope_id,
            payload.reason,
            actor=ctx.caller,
            block_exits=bool(payload.block_exits),
        )
    except ValueError as exc:
        # Issue #220 (PR #233 review): if a record already exists in
        # TRIPPED / CLEAR_PENDING and the operator is requesting a
        # different ``block_exits`` value (e.g. SOFT auto-trip → HARD
        # panic stop), allow the in-place flag upgrade via
        # set_block_exits rather than rejecting with 409. Without this,
        # an operator could not panic-stop exit orders after a daily-
        # loss auto-trip without first clearing and rearming the kill
        # switch — defeating the purpose of the HARD trip.
        from app.risk.kill_switch import KillSwitchState
        existing = ksm.get_record(scope, payload.scope_id)
        can_upgrade = (
            existing is not None
            and existing.state in (
                KillSwitchState.TRIPPED, KillSwitchState.CLEAR_PENDING,
            )
            and bool(existing.block_exits) != bool(payload.block_exits)
        )
        if not can_upgrade:
            raise HTTPException(status_code=409, detail=str(exc))
        try:
            record = ksm.set_block_exits(
                scope,
                payload.scope_id,
                block_exits=bool(payload.block_exits),
                actor=ctx.caller,
                reason=payload.reason,
            )
        except (ValueError, KeyError) as upgrade_exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"trip rejected ({exc}) and block_exits upgrade also "
                    f"failed ({upgrade_exc})"
                ),
            )
        upgraded = True
    _save_kill_switch_state(ksm)
    emit_audit_event(
        actor=ctx.caller,
        action=(
            "kill_switch_block_exits_upgraded" if upgraded else "kill_switch_trip"
        ),
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={
            "scope": payload.scope,
            "scope_id": payload.scope_id,
            "reason": payload.reason,
            "block_exits": bool(payload.block_exits),
            "upgraded_in_place": upgraded,
        },
    )
    return {
        "status": "block_exits_upgraded" if upgraded else "tripped",
        "record_id": record.id,
        "state": record.state.value,
        "block_exits": bool(record.block_exits),
        "upgraded_in_place": upgraded,
    }


@router.post("/kill-switch/request-clear")
def kill_switch_request_clear(
    payload: KillSwitchClearRequestPayload,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Request a kill switch clear (TRIPPED → CLEAR_PENDING). Requires OPERATOR role."""
    ctx.require_role(AdminRole.OPERATOR)
    from app.risk.kill_switch import KillSwitchScope, KillSwitchClearRequest, KillSwitchClearValidation
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    import uuid as _uuid
    clear_req = KillSwitchClearRequest(
        scope=scope,
        scope_id=payload.scope_id,
        actor=ctx.caller,
        reason_code=payload.reason_code,
        request_id=_uuid.uuid4().hex,
        break_glass=payload.break_glass,
    )

    def _validation_fn(req: KillSwitchClearRequest) -> KillSwitchClearValidation:
        if req.break_glass:
            return KillSwitchClearValidation(passed=True, failures=[])
        # Minimal validation: no RECONCILING/ORPHAN_REVIEW positions for this scope
        failures = []
        try:
            hub = get_hub_runtime().hub
            runners = getattr(hub, "_runners", {})
            for runner in runners.values():
                ol = getattr(runner, "_order_lifecycle", None)
                if ol is None:
                    continue
                for rec in getattr(ol, "_position_records", {}).values():
                    state_val = str(getattr(rec, "position_state", "")).upper()
                    if state_val in {"RECONCILING", "MANUAL_REVIEW"}:
                        failures.append(f"position {rec.ownership_key!r} is in {state_val}")
        except Exception:
            pass
        return KillSwitchClearValidation(passed=len(failures) == 0, failures=failures)

    try:
        record, validation = ksm.request_clear(clear_req, _validation_fn)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not validation.passed:
        raise HTTPException(
            status_code=409,
            detail=f"Clear request denied: {validation.failures}",
        )
    _save_kill_switch_state(ksm)
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_request_clear",
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={"scope": payload.scope, "scope_id": payload.scope_id,
                  "reason_code": payload.reason_code, "break_glass": payload.break_glass},
    )
    return {"status": "clear_pending", "record_id": record.id, "state": record.state.value}


@router.post("/kill-switch/confirm-clear")
def kill_switch_confirm_clear(
    payload: KillSwitchRearmRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Confirm a kill switch clear (CLEAR_PENDING → CLEARED). Requires OPERATOR role."""
    ctx.require_role(AdminRole.OPERATOR)
    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    try:
        record = ksm.confirm_clear(scope, payload.scope_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _save_kill_switch_state(ksm)
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_confirm_clear",
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={"scope": payload.scope, "scope_id": payload.scope_id},
    )
    return {"status": "cleared", "record_id": record.id, "state": record.state.value}


@router.post("/kill-switch/rearm")
def kill_switch_rearm(
    payload: KillSwitchRearmRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Rearm a kill switch (CLEARED → INACTIVE). Requires OPERATOR role.

    In LIVE mode, a valid step_up_token with action_class=kill_switch_rearm
    is mandatory (Architecture §15.4). Obtain one first via
    POST /admin/step-up/issue with action_class=kill_switch_rearm.
    """
    ctx.require_role(AdminRole.OPERATOR)

    # §15.4: Require step-up token in LIVE mode before restoring entry eligibility.
    import os as _os
    if str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE":
        if not payload.step_up_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is required to rearm a kill switch in LIVE mode. "
                    "Issue one first: POST /admin/step-up/issue "
                    "{\"action_class\": \"kill_switch_rearm\", \"resource_id\": \"<scope_id>\"}."
                ),
            )
        from app.security.step_up import DangerousActionClass, consume_step_up_token
        if not consume_step_up_token(
            token_id=payload.step_up_token,
            actor=ctx.caller,
            action_class=DangerousActionClass.KILL_SWITCH_REARM,
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is invalid, expired, already used, or was not issued "
                    "to the current actor for kill_switch_rearm. "
                    "Issue a new token via POST /admin/step-up/issue and retry."
                ),
            )

    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    try:
        record = ksm.rearm(scope, payload.scope_id, actor=ctx.caller)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _save_kill_switch_state(ksm)
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_rearm",
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={"scope": payload.scope, "scope_id": payload.scope_id},
    )
    return {"status": "inactive", "record_id": record.id, "state": record.state.value}


@router.get("/kill-switch/state")
def get_kill_switch_state_endpoint(ctx: AdminContext = Depends(get_admin_context)) -> dict:
    """Return current kill switch state for all non-INACTIVE scopes. Requires OPERATOR role."""
    ctx.require_role(AdminRole.OPERATOR)
    from app.risk.kill_switch import get_kill_switch_state
    return get_kill_switch_state()


class KillSwitchCancelAllRequest(BaseModel):
    """Issue #238: idempotent broker-side cancel of all open orders."""

    reason: str = Field(
        ...,
        description=(
            "Operator-entered reason for cancelling all open broker orders. "
            "Stored in the audit trail."
        ),
    )
    broker_account_id: Optional[str] = Field(
        None,
        description=(
            "Optional: limit cancellation to a single broker account. "
            "Omit to cancel across every registered runner."
        ),
    )


@router.post("/kill-switch/cancel-all")
async def kill_switch_cancel_all(
    payload: KillSwitchCancelAllRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Cancel every open broker order across registered account runners.

    Issue #238: dashboard-driven safety control. Iterates each runner's
    last-known broker order list and calls the broker adapter's
    ``cancel_order`` per pending order. Cancellation is idempotent on the
    broker side — a missing or already-cancelled order returns a
    REJECTED ``OrderResponse`` which is recorded but does NOT fail the
    batch. Per-broker results are aggregated and returned so the
    dashboard can surface per-account success / failure.

    This endpoint does NOT trip the durable kill switch — call
    ``/admin/kill-switch/trip`` separately for that. The dashboard flow
    is: trip first (block new placements), then cancel-all (drain open
    orders), then optionally manual flatten via break-glass.

    Requires ADMIN role (stricter than ``trip``/``rearm`` which allow
    OPERATOR) because bulk cancellation affects every account at once.
    """
    ctx.require_role(AdminRole.ADMIN)
    if not str(payload.reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail="reason is required and must be non-empty",
        )

    try:
        runtime = get_hub_runtime()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"hub runtime unavailable: {exc}",
        ) from exc
    hub = getattr(runtime, "hub", None)
    if hub is None:
        raise HTTPException(
            status_code=503,
            detail="hub not available on runtime",
        )

    # Collect target runners. When broker_account_id is provided, scope
    # to that single account; otherwise iterate every registered runner.
    target_runner_ids: list = []
    if payload.broker_account_id:
        runner = hub.get_runner(payload.broker_account_id)
        if runner is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"broker_account_id {payload.broker_account_id!r} has "
                    "no registered runner"
                ),
            )
        target_runner_ids = [payload.broker_account_id]
    else:
        target_runner_ids = list(hub.list_runner_ids())

    per_account_results: list[dict] = []
    aggregate_attempted = 0
    aggregate_cancelled = 0
    aggregate_failed = 0
    aggregate_skipped = 0

    # ``_TERMINAL_BROKER_STATUSES`` lists statuses for which there is
    # nothing left to cancel — we count those as "skipped" rather than
    # attempting a no-op cancel that the broker would reject anyway.
    _TERMINAL_BROKER_STATUSES = {
        "FILLED", "FULL", "COMPLETE", "EXECUTED",
        "CANCELLED", "CANCELED", "CANCEL", "EXPIRED",
        "REJECTED", "REJECT", "FAILED", "FAILURE", "ERROR",
    }

    for acct_id in target_runner_ids:
        runner = hub.get_runner(acct_id)
        if runner is None:
            per_account_results.append({
                "broker_account_id": str(acct_id),
                "status": "no_runner",
                "attempted": 0,
                "cancelled": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [],
            })
            continue
        broker_client = getattr(runner, "_broker_client", None)
        last_orders = list(getattr(runner, "_last_orders", None) or [])
        attempted = 0
        cancelled = 0
        failed = 0
        skipped = 0
        errors: list[dict] = []
        cancel_fn = getattr(broker_client, "cancel_order", None)
        if not callable(cancel_fn):
            per_account_results.append({
                "broker_account_id": str(acct_id),
                "status": "broker_no_cancel_api",
                "attempted": 0,
                "cancelled": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [],
            })
            continue
        for order in last_orders:
            broker_order_id = str(getattr(order, "broker_order_id", "") or "")
            order_status = str(getattr(order, "status", "") or "").strip().upper()
            symbol = getattr(order, "symbol", None)
            if not broker_order_id:
                skipped += 1
                continue
            if order_status in _TERMINAL_BROKER_STATUSES:
                skipped += 1
                continue
            attempted += 1
            try:
                resp = await cancel_fn(broker_order_id, symbol=symbol)
            except Exception as exc:
                failed += 1
                errors.append({
                    "broker_order_id": broker_order_id,
                    "error": repr(exc),
                })
                continue
            resp_status = str(getattr(resp, "status", "") or "").strip().upper()
            if resp_status in {"CANCELLED", "CANCELED", "CANCEL"}:
                cancelled += 1
            elif resp_status in {"FILLED", "FULL", "COMPLETE", "EXECUTED"}:
                # Already terminal — broker raced us; idempotent OK.
                cancelled += 1
            elif resp_status in {"REJECTED", "REJECT", "FAILED", "FAILURE", "ERROR"}:
                # Idempotent: a cancel of an already-cancelled or
                # unknown order frequently yields REJECTED. Treat as
                # skipped so an operator-driven double-click doesn't
                # surface as a hard failure.
                resp_message = str(getattr(resp, "message", "") or "")
                skipped += 1
                if resp_message:
                    errors.append({
                        "broker_order_id": broker_order_id,
                        "broker_status": resp_status,
                        "broker_message": resp_message,
                    })
            else:
                # Unknown / pending status from the broker.
                failed += 1
                errors.append({
                    "broker_order_id": broker_order_id,
                    "broker_status": resp_status or "unknown",
                })
        per_account_results.append({
            "broker_account_id": str(acct_id),
            "status": "ok" if failed == 0 else "partial",
            "attempted": attempted,
            "cancelled": cancelled,
            "failed": failed,
            "skipped": skipped,
            "errors": errors[:20],  # cap for payload size
        })
        aggregate_attempted += attempted
        aggregate_cancelled += cancelled
        aggregate_failed += failed
        aggregate_skipped += skipped

    overall_status = "ok" if aggregate_failed == 0 else "partial"
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_cancel_all",
        resource_type="broker_orders",
        resource_id=payload.broker_account_id or "ALL_ACCOUNTS",
        metadata={
            "reason": payload.reason,
            "broker_account_id": payload.broker_account_id,
            "attempted": aggregate_attempted,
            "cancelled": aggregate_cancelled,
            "failed": aggregate_failed,
            "skipped": aggregate_skipped,
            "overall_status": overall_status,
            "per_account": [
                {
                    "broker_account_id": r["broker_account_id"],
                    "status": r["status"],
                    "attempted": r["attempted"],
                    "cancelled": r["cancelled"],
                    "failed": r["failed"],
                    "skipped": r["skipped"],
                }
                for r in per_account_results
            ],
        },
    )
    return {
        "status": overall_status,
        "attempted": aggregate_attempted,
        "cancelled": aggregate_cancelled,
        "failed": aggregate_failed,
        "skipped": aggregate_skipped,
        "per_account": per_account_results,
    }


@router.get("/release-evidence")
def get_release_evidence(ctx: AdminContext = Depends(get_admin_context)) -> dict:
    """Return the LIVE release-evidence bundle for operator sign-off.

    Captures trade mode, authority path, runtime readiness, position authority
    restore status, outbox recovery summary, schema guard result, runner counts,
    and key safety flags. Operators must review this bundle before approving a
    LIVE deployment.

    Requires OPERATOR role.
    """
    ctx.require_role(AdminRole.OPERATOR)
    from app.runtime.app_runtime import get_app_runtime
    runtime = get_app_runtime()
    evidence = runtime.release_evidence_snapshot()
    emit_audit_event(
        actor=ctx.caller,
        action="release_evidence_read",
        resource_type="release_evidence",
        resource_id="live",
        metadata={"trade_mode": evidence.get("trade_mode")},
    )
    return evidence


@router.post("/step-up/issue")
def step_up_issue(
    payload: StepUpIssueRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Issue a short-lived step-up token for a dangerous action class.

    Tokens are single-use, 5-minute TTL, actor-bound, and Postgres-persisted
    in LIVE mode (Architecture §15.4 / issue #110). Requires OPERATOR role.

    Use the returned token_id in the corresponding dangerous operation within
    the TTL window. Examples:
    - break-glass flatten  → POST /admin/break-glass/flatten {step_up_token: ...}
    - kill switch rearm    → POST /admin/kill-switch/rearm   {step_up_token: ...}
    """
    ctx.require_role(AdminRole.OPERATOR)

    from app.security.step_up import DangerousActionClass, issue_step_up_token

    try:
        action_class = DangerousActionClass(payload.action_class.strip().lower())
    except ValueError:
        allowed = ", ".join(v.value for v in DangerousActionClass)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown action_class {payload.action_class!r}. Allowed: {allowed}.",
        )

    try:
        tok = issue_step_up_token(
            actor=ctx.caller,
            action_class=action_class,
            resource_id=payload.resource_id or "",
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    return {
        "token_id": tok.token_id,
        "action_class": tok.action_class.value,
        "resource_id": tok.resource_id,
        "actor": tok.actor,
        "expires_at": tok.expires_at,
        "ttl_seconds": int(tok.expires_at - tok.issued_at),
    }


__all__ = [
    "AdminTestOrderRequest",
    "BreakGlassFlattenRequest",
    "BrokerAccountUpsertRequest",
    "StepUpIssueRequest",
    "SubscriptionUpsertRequest",
    "TenantUpsertRequest",
    "admin_test_order",
    "break_glass_flatten",
    "create_or_update_broker_account",
    "create_or_update_subscription",
    "create_or_update_tenant",
    "list_broker_accounts",
    "list_runners",
    "kill_switch_rearm",
    "list_tenants",
    "manual_eod_exit",
    "manual_sweep",
    "ManualEodExitRequest",
    "ManualSweepRequest",
    "ResolveOrphanReviewRequest",
    "resolve_orphan_review",
    "router",
    "step_up_issue",
]
