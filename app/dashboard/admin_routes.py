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
    """Payload for break-glass manual flatten (Architecture S1 rule 3-4)."""
    tenant_id: str
    broker_account_id: str
    underlying: str
    expiry: str
    strike: str
    option_right: str
    product_type: str
    reason: str


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
        event_type="ADMIN_RESOLVE_EXPIRED_CONTRACTS",
        actor=ctx.caller,
        details={
            "symbol_pattern": payload.symbol_pattern,
            "min_age_days": payload.min_age_days,
            "rows_updated": updated,
        },
    )
    return {"dry_run": False, "updated": updated, "symbol_pattern": payload.symbol_pattern}


__all__ = [
    "AdminTestOrderRequest",
    "BreakGlassFlattenRequest",
    "BrokerAccountUpsertRequest",
    "SubscriptionUpsertRequest",
    "TenantUpsertRequest",
    "admin_test_order",
    "break_glass_flatten",
    "create_or_update_broker_account",
    "create_or_update_subscription",
    "create_or_update_tenant",
    "list_broker_accounts",
    "list_runners",
    "list_tenants",
    "manual_eod_exit",
    "manual_sweep",
    "ManualEodExitRequest",
    "ManualSweepRequest",
    "ResolveOrphanReviewRequest",
    "resolve_orphan_review",
    "router",
]
