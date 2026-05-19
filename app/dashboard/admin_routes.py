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
from app.dashboard.strategy_candidates import (
    approve_strategy_candidate,
    get_strategy_candidate,
    list_strategy_candidates,
    reject_strategy_candidate,
)
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


class StrategyCandidateListResponse(BaseModel):
    count: int
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class StrategyCandidateReviewRequest(BaseModel):
    reason: Optional[str] = None


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


@router.get("/strategy-candidates", response_model=StrategyCandidateListResponse)
def list_strategy_candidate_reviews(
    request: Request,
    candidate_status: Optional[str] = Query(default="pending", alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: AdminContext = Depends(get_admin_context),
) -> StrategyCandidateListResponse:
    ctx.require_role(AdminRole.READONLY)
    check_rate_limit(request)
    payload = list_strategy_candidates(
        candidate_status=candidate_status,
        limit=limit,
    )
    return StrategyCandidateListResponse(**payload)


@router.get("/strategy-candidates/{candidate_id}")
def get_strategy_candidate_review(
    request: Request,
    candidate_id: str,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict[str, Any]:
    ctx.require_role(AdminRole.READONLY)
    check_rate_limit(request)
    return get_strategy_candidate(candidate_id)


@router.post("/strategy-candidates/{candidate_id}/approve")
def approve_strategy_candidate_review(
    request: Request,
    candidate_id: str,
    req: StrategyCandidateReviewRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict[str, Any]:
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)
    return approve_strategy_candidate(
        candidate_id=candidate_id,
        actor=ctx.caller,
        reason=req.reason,
        request_id=_request_id_from_request(request),
    )


@router.post("/strategy-candidates/{candidate_id}/reject")
def reject_strategy_candidate_review(
    request: Request,
    candidate_id: str,
    req: StrategyCandidateReviewRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict[str, Any]:
    ctx.require_role(AdminRole.ADMIN)
    check_rate_limit(request)
    return reject_strategy_candidate(
        candidate_id=candidate_id,
        actor=ctx.caller,
        reason=req.reason,
        request_id=_request_id_from_request(request),
    )


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
                    "    net_qty = 0, "
                    "    unrealized_pnl = 0, "
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
            "net_qty": 0,
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


def _require_scope_entitlement(ctx, scope_str: str, scope_id: str) -> None:
    """PR #240 round-6 review P1: enforce that the admin's tenant
    entitlement covers the kill-switch scope they are mutating.

    - GLOBAL scope requires ``ctx.all_tenants``.
    - TENANT scope requires either ``all_tenants`` or
      ``can_access_tenant(scope_id)``.
    - ACCOUNT scope requires either ``all_tenants`` OR an explicit
      ``broker_account_ids`` match OR (if the admin is tenant-scoped
      with no explicit account allow-list) a resolved tenant
      membership.
    - STRATEGY scope requires either ``all_tenants`` or an explicit
      ``broker_account_ids`` match (strategy IDs do not map cleanly
      to a tenant without additional metadata, so fail closed for
      tenant-only scoped admins).

    Without this check, a tenant-scoped admin bearer session could
    use the trip/clear/rearm endpoints (gated only by role=ADMIN)
    to mutate the GLOBAL kill switch and affect every tenant.

    PR #240 round-7 review P1: ``can_access_broker_account`` returns
    True when ``broker_account_ids`` is empty (auth.py:120-123). That
    let tenant-scoped admins with no explicit account allow-list pass
    the ACCOUNT/STRATEGY gate for arbitrary scope_ids and mutate
    another tenant's account-level kill switch. Replace the bare
    ``can_access_broker_account`` call with an explicit fail-closed
    check that requires either an exact account-id match OR a
    runtime/control-plane resolution that confirms the account
    belongs to a tenant in ``ctx.tenant_ids``.
    """
    scope_upper = str(scope_str or "").strip().upper()
    if scope_upper == "GLOBAL":
        if not ctx.all_tenants:
            raise HTTPException(
                status_code=403,
                detail=(
                    "GLOBAL-scope kill-switch mutations require an admin "
                    "with cross-tenant entitlement (all_tenants=true)."
                ),
            )
        return
    if scope_upper == "TENANT":
        if ctx.all_tenants or ctx.can_access_tenant(str(scope_id)):
            return
        raise HTTPException(
            status_code=403,
            detail=(
                f"tenant_id={scope_id!r} is outside this admin's tenant entitlement"
            ),
        )
    if scope_upper == "ACCOUNT":
        if ctx.all_tenants:
            return
        # Explicit broker-account allow-list — exact match.
        if ctx.broker_account_ids and str(scope_id) in ctx.broker_account_ids:
            return
        # Tenant-scoped admin with no explicit account allow-list —
        # resolve account → tenant_id and check tenant membership.
        if ctx.tenant_ids and not ctx.broker_account_ids:
            resolved_tenant = ""
            try:
                _rt = get_hub_runtime()
                _hub = getattr(_rt, "hub", None)
                _runner = (
                    _hub.get_runner(str(scope_id))
                    if _hub is not None and hasattr(_hub, "get_runner")
                    else None
                )
                if _runner is not None:
                    resolved_tenant = str(
                        getattr(_runner, "tenant_id", "") or ""
                    )
            except Exception:
                resolved_tenant = ""
            if not resolved_tenant:
                try:
                    from app.tenants.firestore_client import get_broker_account
                    _ba = get_broker_account(str(scope_id))
                    resolved_tenant = (
                        str(getattr(_ba, "tenant_id", "") or "")
                        if _ba is not None else ""
                    )
                except Exception:
                    resolved_tenant = ""
            if resolved_tenant and resolved_tenant in ctx.tenant_ids:
                return
        raise HTTPException(
            status_code=403,
            detail=(
                f"broker_account_id={scope_id!r} is outside this admin's "
                "broker-account / tenant entitlement"
            ),
        )
    if scope_upper == "STRATEGY":
        if ctx.all_tenants:
            return
        # STRATEGY scope_id does not have a uniform account/tenant
        # mapping — only allow exact broker_account_ids membership
        # (covers strategies keyed by account) or fail closed.
        if ctx.broker_account_ids and str(scope_id) in ctx.broker_account_ids:
            return
        raise HTTPException(
            status_code=403,
            detail=(
                f"strategy scope_id={scope_id!r} is outside this admin's "
                "explicit entitlement (STRATEGY scope requires all_tenants "
                "or an exact broker_account_ids match)"
            ),
        )
    # Unknown scope — let downstream KillSwitchScope validation 422.


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


def _save_kill_switch_state(ksm, *, rollback=None) -> None:
    """Persist KillSwitchManager state to Postgres.

    Issue #238 acceptance: in LIVE mode the kill-switch state MUST be
    persisted durably. A silent in-memory-only save would leave the
    operator's UI showing TRIPPED while a process restart would
    rehydrate from Postgres and find INACTIVE — defeating the
    durability guarantee. Fail closed in LIVE so the API call surfaces
    a 500 to the dashboard instead of misleading the operator.

    PR #240 round-1 review P1: when LIVE persistence fails the in-
    memory record has ALREADY been mutated (the endpoints call
    ``ksm.trip()`` / ``ksm.rearm()`` etc. before invoking this helper),
    so a running process would still consider the kill switch e.g.
    INACTIVE after a failed rearm. The optional ``rollback`` callable
    is invoked BEFORE raising the 500 to restore the prior in-memory
    state. Best-effort: any exception from the rollback is logged and
    swallowed so the original HTTP 500 always reaches the operator.

    In non-LIVE modes the existing non-fatal warning is preserved so
    local/dev runs without a control-plane Postgres do not crash.
    """
    import logging as _log
    import os as _os
    trade_mode = str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    try:
        # PR #240 round-3 review P1: use an EXPLICIT transaction for
        # LIVE persistence so a multi-record save is atomic. With
        # autocommit, a failure midway through ``save_state`` (which
        # upserts every kill-switch record) can leave Postgres with
        # some rows updated and others not — after a process restart
        # the durable state may resurrect a toggle the dashboard was
        # told failed. A transaction lets Postgres roll back the
        # partial write so the durable state matches the in-memory
        # rollback we perform below.
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        with connect_with_retry(get_control_plane_dsn(), autocommit=False) as conn:
            try:
                ksm.save_state(conn)
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
    except Exception as exc:
        if trade_mode == "LIVE":
            _log.getLogger(__name__).error(
                "kill_switch.save_state failed in LIVE — failing closed "
                "so the operator does not see a phantom toggle: %s", exc,
            )
            if rollback is not None:
                try:
                    rollback()
                except Exception as rb_exc:
                    _log.getLogger(__name__).warning(
                        "kill_switch state rollback after LIVE persist "
                        "failure also failed (best-effort): %s", rb_exc,
                    )
            # PR #240 round-3 review P2: emit a compensating audit
            # event so reviewers can see that the manager's earlier
            # ``kill_switch.*`` audit entry (which the manager emitted
            # before this helper attempted persistence) does NOT
            # represent durable state. Append-only audit logs cannot
            # delete the prior entry, but this compensating record
            # makes the rollback visible during incident review.
            try:
                emit_audit_event(
                    actor="system",
                    action="kill_switch_persist_failed_rollback",
                    resource_type="kill_switch",
                    resource_id="",
                    metadata={
                        "error": str(exc),
                        "trade_mode": trade_mode,
                        "note": (
                            "in-memory state restored; preceding "
                            "kill_switch.* audit entry from the "
                            "manager does NOT reflect durable state"
                        ),
                    },
                )
            except Exception:
                pass  # best-effort
            raise HTTPException(
                status_code=500,
                detail=(
                    "Kill-switch state could not be persisted to Postgres. "
                    "The toggle has NOT taken durable effect — in-memory "
                    "state has been rolled back. Resolve the control-plane "
                    f"outage and retry. Underlying error: {exc}"
                ),
            ) from exc
        _log.getLogger(__name__).warning("kill_switch.save_state failed (non-fatal): %s", exc)


def _kill_switch_rollback_factory(ksm, scope, scope_id):
    """Capture the current in-memory ``KillSwitchRecord`` for the given
    (scope, scope_id) and return a callable that restores it.

    PR #240 round-1 review P1: used by trip / rearm / clear endpoints
    so an LIVE persist failure can undo the in-memory mutation before
    surfacing the 500.
    """
    key = ksm._key(scope, scope_id)  # KSM uses tuple(scope, scope_id) keys
    import copy as _copy
    # PR #240 round-6 review P2: capture the pre-mutation snapshot
    # UNDER the manager's lock so a concurrent successful mutation
    # cannot land between our snapshot and the caller's mutation.
    # Without this, two overlapping requests could both observe the
    # same prior state and either could roll back to a snapshot that
    # no longer reflects what was actually there at mutation time.
    with ksm._lock:
        prior_snapshot = _copy.deepcopy(ksm._records.get(key))
    # PR #240 round-4/round-5 review P2: capture the post-mutation
    # record's identity at the time we wrap the rollback so a
    # CONCURRENT successful request that further mutates the same
    # scope is not clobbered.
    #
    # Round-4 used only ``record.id`` for the comparison, but
    # ``KillSwitchManager.request_clear`` / ``confirm_clear`` /
    # ``rearm`` mutate the existing record IN PLACE without changing
    # ``id``. An id-only guard therefore cannot detect a newer
    # successful transition on the same scope. Round-5 expands the
    # fingerprint to include ``state.value``, ``block_exits``, and
    # ``updated_at`` so any concurrent state-changing mutation is
    # observed and the rollback is suppressed.
    expected_post_fingerprint: list = []  # mutable cell

    def _record_fingerprint(record) -> Optional[tuple]:
        if record is None:
            return None
        state = getattr(record, "state", None)
        state_val = getattr(state, "value", state)
        return (
            getattr(record, "id", None),
            state_val,
            bool(getattr(record, "block_exits", False)),
            getattr(record, "updated_at", None),
        )

    def _rollback() -> None:
        with ksm._lock:
            current = ksm._records.get(key)
            current_fp = _record_fingerprint(current)
            expected_fp = (
                expected_post_fingerprint[0]
                if expected_post_fingerprint
                else None
            )
            if expected_fp is not None and current_fp != expected_fp:
                # A concurrent request has further mutated this scope
                # (different id, state, block_exits, or updated_at).
                # Suppress rollback to avoid clobbering newer state.
                _log.getLogger(__name__).warning(
                    "kill_switch rollback suppressed — concurrent mutation "
                    "on scope (%s, %s): expected=%s current=%s",
                    scope, scope_id, expected_fp, current_fp,
                )
                return
            if prior_snapshot is None:
                ksm._records.pop(key, None)
            else:
                ksm._records[key] = prior_snapshot

    def _set_expected(record) -> None:
        expected_post_fingerprint.append(_record_fingerprint(record))

    _rollback.set_expected_post_record = _set_expected  # type: ignore[attr-defined]
    return _rollback


import logging as _log  # noqa: E402 — used by ``_rollback``'s concurrent-mutation warning


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
    # PR #240 round-3 review P2: capture the operator-entered reason
    # so confirm-clear and rearm audit events carry the typed
    # justification the dashboard already prompts for. Optional for
    # backward compatibility with existing CLI scripts.
    reason: Optional[str] = Field(
        None,
        description=(
            "Operator-entered reason / acknowledgement, persisted in "
            "the audit event metadata. The dashboard requires a non-"
            "empty value before enabling the Confirm button; CLI "
            "scripts may omit it for backward compatibility."
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
    """Trip a kill switch for a given scope.

    PR #240 round-2 review P2 (issue #238 acceptance): kill-switch
    toggle endpoints are ADMIN-only. The dashboard UI gates the
    controls behind ``<Gate requiredRoles={[ADMIN]}>``; the backend
    MUST match so an operator bearer session cannot bypass the React
    gate via direct API/BFF calls.
    """
    ctx.require_role(AdminRole.ADMIN)
    # PR #240 round-6 review P1: ensure the admin's entitlement
    # covers the requested scope (GLOBAL requires cross-tenant).
    _require_scope_entitlement(ctx, payload.scope, payload.scope_id)
    # PR #240 round-7 review P2: reject whitespace-only reasons
    # server-side. The dashboard enforces non-empty in React but a
    # direct API/BFF call can still POST ``reason="   "`` and trip
    # the kill switch with an unusable audit justification. Mirror
    # the cancel-all endpoint's check so every operator-driven
    # kill-switch mutation carries a meaningful reason.
    if not str(payload.reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail="reason is required and must be non-empty",
        )
    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    # PR #240 round-1 review P1: capture prior record so a LIVE persist
    # failure can roll back the in-memory trip mutation.
    rollback = _kill_switch_rollback_factory(ksm, scope, payload.scope_id)
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
    # PR #240 round-4 review P2: register post-mutation identity so
    # the rollback only fires if the record is STILL the one we
    # mutated (i.e. no concurrent request has further changed state).
    rollback.set_expected_post_record(record)  # type: ignore[attr-defined]
    _save_kill_switch_state(ksm, rollback=rollback)
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
    """Request a kill switch clear (TRIPPED → CLEAR_PENDING).

    PR #240 round-2 review P2: ADMIN-only per #238 acceptance — matches
    the dashboard UI gate so operator sessions cannot bypass.
    """
    ctx.require_role(AdminRole.ADMIN)
    _require_scope_entitlement(ctx, payload.scope, payload.scope_id)
    # PR #240 round-7 review P2: reject whitespace-only reason_code
    # server-side so direct API/BFF callers cannot bypass the
    # dashboard's non-empty check.
    if not str(payload.reason_code or "").strip():
        raise HTTPException(
            status_code=422,
            detail="reason_code is required and must be non-empty",
        )
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

    rollback = _kill_switch_rollback_factory(ksm, scope, payload.scope_id)
    try:
        record, validation = ksm.request_clear(clear_req, _validation_fn)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not validation.passed:
        raise HTTPException(
            status_code=409,
            detail=f"Clear request denied: {validation.failures}",
        )
    rollback.set_expected_post_record(record)  # type: ignore[attr-defined]
    _save_kill_switch_state(ksm, rollback=rollback)
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
    """Confirm a kill switch clear (CLEAR_PENDING → CLEARED).

    PR #240 round-2 review P1: this is the transition that restores
    LIVE entry eligibility — ``is_tripped()`` no longer blocks orders
    once the record is CLEARED, even before the separate Rearm step.
    Therefore it requires the same step-up token ceremony in LIVE as
    rearm does (Architecture §15.4) — using the
    ``KILL_SWITCH_CLEAR`` action class so the token cannot be reused
    across action classes. Without this gate, a single authenticated
    admin session could clear the kill switch end-to-end without any
    secondary credential.

    PR #240 round-2 review P2: also ADMIN-only per #238 acceptance.
    PR #240 round-6 review P1: also requires the admin's tenant
    entitlement to cover the scope being mutated.
    """
    ctx.require_role(AdminRole.ADMIN)
    _require_scope_entitlement(ctx, payload.scope, payload.scope_id)

    # Parse scope FIRST so we can scope the step-up token check.
    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")

    # §15.4 step-up gate (LIVE only).
    # PR #240 round-3 review P1: pass ``resource_id=payload.scope_id``
    # so a token issued for one scope cannot be reused to clear a
    # different scope (the token issuer contract binds the resource).
    import os as _os
    if str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE":
        if not payload.step_up_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is required to confirm-clear a kill "
                    "switch in LIVE mode. Issue one first: "
                    "POST /admin/step-up/issue {\"action_class\": "
                    f"\"kill_switch_clear\", \"resource_id\": \"{payload.scope_id}\"}}."
                ),
            )
        from app.security.step_up import DangerousActionClass, consume_step_up_token
        if not consume_step_up_token(
            token_id=payload.step_up_token,
            actor=ctx.caller,
            action_class=DangerousActionClass.KILL_SWITCH_CLEAR,
            resource_id=str(payload.scope_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is invalid, expired, already used, or was not "
                    f"issued to the current actor for kill_switch_clear on "
                    f"scope_id={payload.scope_id!r}. Issue a new token via "
                    "POST /admin/step-up/issue and retry."
                ),
            )

    ksm = _get_kill_switch_manager()
    rollback = _kill_switch_rollback_factory(ksm, scope, payload.scope_id)
    try:
        record = ksm.confirm_clear(scope, payload.scope_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    rollback.set_expected_post_record(record)  # type: ignore[attr-defined]
    _save_kill_switch_state(ksm, rollback=rollback)
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_confirm_clear",
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={
            "scope": payload.scope,
            "scope_id": payload.scope_id,
            # PR #240 round-3 review P2: persist the operator
            # acknowledgement reason so the audit trail carries the
            # justification for the transition that restores LIVE
            # entry eligibility.
            "reason": payload.reason or "",
        },
    )
    return {"status": "cleared", "record_id": record.id, "state": record.state.value}


@router.post("/kill-switch/rearm")
def kill_switch_rearm(
    payload: KillSwitchRearmRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Rearm a kill switch (CLEARED → INACTIVE).

    In LIVE mode, a valid step_up_token with action_class=kill_switch_rearm
    is mandatory (Architecture §15.4). Obtain one first via
    POST /admin/step-up/issue with action_class=kill_switch_rearm.

    PR #240 round-2 review P2: ADMIN-only per #238 acceptance — matches
    the dashboard UI gate so operator sessions cannot bypass.
    PR #240 round-6 review P1: also requires the admin's tenant
    entitlement to cover the scope being mutated.
    """
    ctx.require_role(AdminRole.ADMIN)
    _require_scope_entitlement(ctx, payload.scope, payload.scope_id)

    # §15.4: Require step-up token in LIVE mode before restoring entry eligibility.
    import os as _os
    if str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE":
        if not payload.step_up_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is required to rearm a kill switch in LIVE mode. "
                    "Issue one first: POST /admin/step-up/issue "
                    f"{{\"action_class\": \"kill_switch_rearm\", \"resource_id\": \"{payload.scope_id}\"}}."
                ),
            )
        from app.security.step_up import DangerousActionClass, consume_step_up_token
        # PR #240 round-3 review P1: bind the token to ``scope_id`` so
        # one scope's token cannot rearm a different scope.
        if not consume_step_up_token(
            token_id=payload.step_up_token,
            actor=ctx.caller,
            action_class=DangerousActionClass.KILL_SWITCH_REARM,
            resource_id=str(payload.scope_id),
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "step_up_token is invalid, expired, already used, or was not issued "
                    f"to the current actor for kill_switch_rearm on scope_id="
                    f"{payload.scope_id!r}. Issue a new token via "
                    "POST /admin/step-up/issue and retry."
                ),
            )

    from app.risk.kill_switch import KillSwitchScope
    try:
        scope = KillSwitchScope(payload.scope.upper())
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid scope: {payload.scope!r}")
    ksm = _get_kill_switch_manager()
    rollback = _kill_switch_rollback_factory(ksm, scope, payload.scope_id)
    try:
        record = ksm.rearm(scope, payload.scope_id, actor=ctx.caller)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    rollback.set_expected_post_record(record)  # type: ignore[attr-defined]
    _save_kill_switch_state(ksm, rollback=rollback)
    emit_audit_event(
        actor=ctx.caller,
        action="kill_switch_rearm",
        resource_type="kill_switch",
        resource_id=str(payload.scope_id),
        metadata={
            "scope": payload.scope,
            "scope_id": payload.scope_id,
            # PR #240 round-3 review P2: persist operator reason.
            "reason": payload.reason or "",
        },
    )
    return {"status": "inactive", "record_id": record.id, "state": record.state.value}


@router.get("/kill-switch/state")
def get_kill_switch_state_endpoint(ctx: AdminContext = Depends(get_admin_context)) -> dict:
    """Return current kill switch state for all non-INACTIVE scopes.

    PR #240 round-3 review P2: include ``trade_mode`` so the dashboard
    can render the step-up token field conditionally (LIVE-only).
    Without this, the UI either always shows the token field
    (blocking non-LIVE confirm-clear/rearm) or never shows it
    (defeating the LIVE protection). Requires OPERATOR role.
    """
    ctx.require_role(AdminRole.OPERATOR)
    import os as _os
    from app.risk.kill_switch import get_kill_switch_state
    state = get_kill_switch_state()
    state["trade_mode"] = str(_os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    return state


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
    request: Request,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    """Cancel every open broker order across registered account runners.

    Issue #238 + PR #240 round-3: dashboard-driven safety control.
    Cancellation is idempotent on the broker side; per-broker results
    are aggregated and returned.

    Server-side preconditions enforced (PR #240 round-3 review P2):
      * ADMIN role
      * Non-empty reason
      * Rate-limited via the standard dashboard limiter
      * The durable GLOBAL kill switch must be actively blocking new
        placements (TRIPPED or CLEAR_PENDING). Otherwise Phoenix is
        free to place fresh orders during the bulk-cancel window,
        defeating the documented emergency-drain sequence.

    The endpoint does NOT trip the durable switch — operators must
    call ``/admin/kill-switch/trip`` first.
    """
    ctx.require_role(AdminRole.ADMIN)
    # PR #240 round-3 review P2: rate-limit destructive bulk cancel
    # the same way as every other state-changing admin endpoint.
    check_rate_limit(request)
    if not str(payload.reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail="reason is required and must be non-empty",
        )

    # PR #240 round-3 review P2: enforce trip-before-cancel on the
    # server even though the UI also disables the button. A direct
    # API/BFF call must be rejected when the durable kill switch is
    # INACTIVE / CLEARED, because new placements are still allowed in
    # those states.
    # PR #240 round-4/round-5 review P2: hierarchical trip-before-cancel
    # check that mirrors the order interceptor's GLOBAL → TENANT →
    # ACCOUNT precedence. A trip at ANY level above the target broker
    # account already blocks new placements for that account, so the
    # endpoint should accept it as a precondition without forcing the
    # operator to escalate to GLOBAL or add a duplicate ACCOUNT trip.
    try:
        _precheck_ksm = _get_kill_switch_manager()
        from app.risk.kill_switch import KillSwitchScope, KillSwitchState
        _ACTIVE_STATES = (
            KillSwitchState.TRIPPED, KillSwitchState.CLEAR_PENDING,
        )

        def _is_active(record) -> bool:
            return record is not None and record.state in _ACTIVE_STATES

        _global_record = _precheck_ksm.get_record(
            KillSwitchScope.GLOBAL, "GLOBAL",
        )
        _global_active = _is_active(_global_record)
        _scoped_active = False
        if not _global_active and payload.broker_account_id:
            # ACCOUNT-level trip at this broker account.
            _account_record = _precheck_ksm.get_record(
                KillSwitchScope.ACCOUNT, str(payload.broker_account_id),
            )
            _scoped_active = _is_active(_account_record)
            # PR #240 round-5/round-6 review P2: TENANT-level trip
            # that covers this broker account.
            #
            # Round-6: prefer the runtime ``runner.tenant_id`` over
            # the control-plane ``get_broker_account`` lookup. The
            # hub already knows which tenant owns each runner; using
            # that avoids losing TENANT-trip coverage during a
            # control-plane outage (precisely when the emergency
            # drain is most needed). Fall back to the control-plane
            # lookup only when the runner is not registered yet.
            if not _scoped_active:
                _tenant_id = ""
                _runtime = get_hub_runtime()
                _hub = getattr(_runtime, "hub", None)
                _runner = None
                if _hub is not None and hasattr(_hub, "get_runner"):
                    try:
                        _runner = _hub.get_runner(payload.broker_account_id)
                    except Exception:
                        _runner = None
                if _runner is not None:
                    _tenant_id = str(getattr(_runner, "tenant_id", "") or "")
                if not _tenant_id:
                    try:
                        from app.tenants.firestore_client import get_broker_account
                        _ba = get_broker_account(str(payload.broker_account_id))
                        _tenant_id = (
                            str(getattr(_ba, "tenant_id", "") or "")
                            if _ba is not None else ""
                        )
                    except Exception as _cp_exc:
                        # Control-plane outage AND no runtime runner —
                        # we cannot determine tenant. Fail with a
                        # clear 503 rather than silently treating an
                        # active TENANT trip as absent.
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                "Could not resolve tenant_id for "
                                f"broker_account_id={payload.broker_account_id!r}: "
                                f"{_cp_exc}. Retry once the control "
                                "plane is reachable, or use the GLOBAL trip."
                            ),
                        ) from _cp_exc
                if _tenant_id:
                    _tenant_record = _precheck_ksm.get_record(
                        KillSwitchScope.TENANT, _tenant_id,
                    )
                    _scoped_active = _is_active(_tenant_record)
        if not (_global_active or _scoped_active):
            current_label = (
                _global_record.state.value
                if _global_record is not None
                else "INACTIVE"
            )
            detail = (
                "cancel-all requires an active kill switch in the "
                "GLOBAL → TENANT → ACCOUNT hierarchy covering the "
                "target scope"
            )
            if payload.broker_account_id:
                detail += (
                    f" (broker_account_id={payload.broker_account_id!r})"
                )
            detail += f". Current GLOBAL state: {current_label}. "
            detail += "Trip the kill switch first via POST /admin/kill-switch/trip."
            raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        raise
    except Exception as _ksm_exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not verify durable kill-switch state before "
                f"cancel-all: {_ksm_exc}"
            ),
        ) from _ksm_exc

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
    # PR #240 round-1/round-6 review P1: enforce both broker-account
    # AND tenant scoping. A bearer-authenticated admin with restricted
    # ``tenant_ids`` (but no explicit ``broker_account_ids``) would
    # otherwise pass ``can_access_broker_account`` for every account
    # and could drain another tenant's broker orders. We resolve the
    # runner's tenant_id at runtime and require it to be in the admin's
    # ``tenant_ids`` unless ``all_tenants`` is True.

    def _admin_can_drain(_runner, acct_id: str) -> bool:
        """Combined broker-account + tenant scope check.

        Accepts the drain if ANY of:
          - admin has cross-tenant entitlement (``all_tenants``)
          - admin is broker-account-scoped and this account is in
            their entitlement (``broker_account_ids``)
          - admin is tenant-scoped and this account's runner belongs
            to a tenant in their entitlement (``tenant_ids``)

        Round-6 review P1: was previously requiring tenant match
        for ALL scoped admins, which broke pure account-scoped
        admins who have no tenant restriction.
        """
        if ctx.all_tenants:
            return True
        if ctx.broker_account_ids and str(acct_id) in ctx.broker_account_ids:
            return True
        runner_tenant = str(getattr(_runner, "tenant_id", "") or "")
        if (
            ctx.tenant_ids
            and runner_tenant
            and runner_tenant in ctx.tenant_ids
        ):
            return True
        return False

    target_runner_ids: list = []
    if payload.broker_account_id:
        runner = hub.get_runner(payload.broker_account_id)
        if runner is None:
            # Resolve scope first before exposing 404 vs 403; an
            # unauthorised admin should not be able to probe whether
            # an arbitrary account_id has a registered runner.
            if not ctx.can_access_broker_account(payload.broker_account_id):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"broker_account_id {payload.broker_account_id!r} is "
                        "outside this admin's scope"
                    ),
                )
            raise HTTPException(
                status_code=404,
                detail=(
                    f"broker_account_id {payload.broker_account_id!r} has "
                    "no registered runner"
                ),
            )
        if not _admin_can_drain(runner, payload.broker_account_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"broker_account_id {payload.broker_account_id!r} is "
                    "outside this admin's scope (tenant or broker_account "
                    "entitlement mismatch)"
                ),
            )
        target_runner_ids = [payload.broker_account_id]
        out_of_scope_runner_ids: list = []
    else:
        all_runner_ids = list(hub.list_runner_ids())
        target_runner_ids = []
        out_of_scope_runner_ids = []
        for acct_id in all_runner_ids:
            r = hub.get_runner(acct_id)
            if r is None:
                # Round-6 review P2: a runner that disappeared
                # between list_runner_ids and get_runner is NOT a
                # scope violation — let the main loop handle it as
                # ``no_runner`` race (failed=1).
                target_runner_ids.append(acct_id)
            elif _admin_can_drain(r, str(acct_id)):
                target_runner_ids.append(acct_id)
            else:
                out_of_scope_runner_ids.append(acct_id)

    per_account_results: list[dict] = []
    aggregate_attempted = 0
    aggregate_cancelled = 0
    aggregate_failed = 0
    aggregate_skipped = 0
    aggregate_raced_filled = 0
    aggregate_refresh_failures = 0
    aggregate_out_of_scope = 0
    for _oos_acct in out_of_scope_runner_ids:
        per_account_results.append({
            "broker_account_id": str(_oos_acct),
            "status": "out_of_scope",
            "attempted": 0,
            "cancelled": 0,
            "failed": 0,
            "skipped": 0,
            "raced_filled": 0,
            "errors": [{"out_of_scope": True}],
        })
        aggregate_out_of_scope += 1

    # Statuses for which there is nothing left to cancel — counted as
    # ``skipped`` rather than attempting a no-op cancel.
    # PR #240 round-7 review P2: include ``COMPLETED`` (matches
    # ``AccountRunner``'s own terminal set) — Zerodha/dhan style
    # brokers emit "COMPLETED" rather than the Angel "COMPLETE", so
    # without this entry a fully-executed order would be re-attempted
    # and turn a clean drain into a partial/failed result.
    _TERMINAL_BROKER_STATUSES = {
        "FILLED", "FULL", "COMPLETE", "COMPLETED", "EXECUTED",
        "CANCELLED", "CANCELED", "CANCEL", "EXPIRED",
        "REJECTED", "REJECT", "FAILED", "FAILURE", "ERROR",
    }
    _CANCELLED_RESPONSE_STATUSES = {"CANCELLED", "CANCELED", "CANCEL"}
    # PR #240 round-1 review P2: a broker that races the cancel with a
    # FILL is NOT a successful cancel — it changes exposure. Track
    # these separately so the operator can see they may need to flatten.
    _FILLED_RACE_RESPONSE_STATUSES = {"FILLED", "FULL", "COMPLETE", "EXECUTED"}
    # PR #240 round-1 review P1: only treat genuinely-idempotent
    # rejections (already-cancelled / unknown order) as ``skipped``.
    # The Angel adapter returns ``status="ERROR"`` for genuine cancel
    # failures (e.g. ``cancel_failed:...``); those MUST surface as
    # failed so the dashboard doesn't show ``failed=0`` during an
    # incident.
    _IDEMPOTENT_REJECTION_STATUSES = {
        "REJECTED", "REJECT", "EXPIRED",
    }
    _CANCEL_FAILURE_STATUSES = {"FAILED", "FAILURE", "ERROR"}

    for acct_id in target_runner_ids:
        runner = hub.get_runner(acct_id)
        if runner is None:
            # PR #240 round-3 review P2: a runner that disappeared
            # between ``list_runner_ids()`` and ``get_runner()`` has
            # NOT been drained — surface as failed so the aggregate
            # verdict is partial and operators see the gap.
            per_account_results.append({
                "broker_account_id": str(acct_id),
                "status": "no_runner",
                "attempted": 0,
                "cancelled": 0,
                "failed": 1,
                "skipped": 0,
                "raced_filled": 0,
                "errors": [{"no_runner_race": True}],
            })
            aggregate_failed += 1
            continue
        broker_client = getattr(runner, "_broker_client", None)
        cancel_fn = getattr(broker_client, "cancel_order", None)
        if not callable(cancel_fn):
            # PR #240 round-2 review P2: an account without a callable
            # cancel API has NOT been drained. For a bulk emergency
            # cancel-all this is a meaningful failure — count it as
            # ``failed=1`` so the aggregate status is partial and the
            # operator sees that one account could still have open
            # broker orders.
            per_account_results.append({
                "broker_account_id": str(acct_id),
                "status": "broker_no_cancel_api",
                "attempted": 0,
                "cancelled": 0,
                "failed": 1,
                "skipped": 0,
                "raced_filled": 0,
                "errors": [{"broker_no_cancel_api": True}],
            })
            aggregate_failed += 1
            continue

        # PR #240 round-1 review P1: refresh broker orders before
        # iterating so the cancel-all does not work from a 90s-stale
        # ``_last_orders`` cache. Fall back to the cached list when the
        # broker refresh fails so we still attempt cancellation of
        # whatever we last knew about.
        #
        # PR #240 round-3 review P2: when both the broker refresh AND
        # the in-memory cache are empty (e.g. immediately after a
        # process restart), also consult ``runner.state_store`` /
        # ``runner._state_store`` for persisted orders. This mirrors
        # ``AccountRunner.cancel_open_orders``'s own fallback so an
        # operator-driven cancel-all does not silently report
        # attempted=0 while persisted open orders are still live.
        get_orders_fn = getattr(broker_client, "get_orders", None)
        live_orders: Optional[list] = None
        refresh_error: Optional[str] = None
        refresh_succeeded = False
        if callable(get_orders_fn):
            try:
                live_orders = list(await get_orders_fn() or [])
                runner._last_orders = live_orders
                refresh_succeeded = True
            except Exception as exc:
                refresh_error = repr(exc)
                live_orders = None
        # PR #240 round-4 review P2: only consult the cache / state
        # store when the broker refresh ACTUALLY FAILED (or there is
        # no refresh API). A successful refresh that returns ``[]`` is
        # an authoritative "no open orders" — falling back to a stale
        # cache would resurrect already-terminal order ids and turn a
        # clean drain into partial/failed when the broker rejects
        # them.
        if live_orders is None and not refresh_succeeded:
            cached = list(getattr(runner, "_last_orders", None) or [])
            if cached:
                live_orders = cached
            else:
                state_store = (
                    getattr(runner, "state_store", None)
                    or getattr(runner, "_state_store", None)
                )
                state_get_orders = getattr(state_store, "get_orders", None)
                if callable(state_get_orders):
                    try:
                        live_orders = list(state_get_orders(acct_id) or [])
                    except Exception as ss_exc:
                        if refresh_error is None:
                            refresh_error = repr(ss_exc)
                        live_orders = []
                else:
                    live_orders = []
        if live_orders is None:
            live_orders = []

        attempted = 0
        cancelled = 0
        failed = 0
        skipped = 0
        raced_filled = 0
        errors: list[dict] = []
        if refresh_error is not None:
            errors.append({"broker_orders_refresh_error": refresh_error})

        for order in live_orders:
            # PR #240 round-1 review P1: ``OrderStatus.order_id`` is the
            # broker id; the field ``broker_order_id`` does not exist
            # on this dataclass. Fall back to several known aliases for
            # broker-shaped dicts.
            order_id = ""
            for alias in ("order_id", "broker_order_id", "orderid", "id"):
                candidate = getattr(order, alias, None)
                if candidate is None and isinstance(order, dict):
                    candidate = order.get(alias)
                if candidate:
                    order_id = str(candidate).strip()
                    if order_id:
                        break
            order_status = str(getattr(order, "status", None) or (
                order.get("status") if isinstance(order, dict) else ""
            ) or "").strip().upper()
            symbol = getattr(order, "symbol", None) or (
                order.get("symbol") if isinstance(order, dict) else None
            )
            # PR #240 round-1 review P2: pass the order's broker
            # ``variety`` (NORMAL / AMO / STOPLOSS / ...) so the
            # broker adapter does not default to NORMAL and fail to
            # cancel AMO/STOPLOSS orders.
            variety = getattr(order, "variety", None) or (
                order.get("variety") if isinstance(order, dict) else None
            )
            if not order_id:
                skipped += 1
                continue
            if order_status in _TERMINAL_BROKER_STATUSES:
                skipped += 1
                continue
            attempted += 1
            # PR #240 round-4 review P2: wrap the direct broker cancel
            # in a small retry loop with the same retryable-status
            # semantics as ``app/brokers/execution.py``. A single
            # transient broker blip during an incident would otherwise
            # surface as ``failed=1`` even though the order is still
            # cancellable. We keep the direct call (rather than
            # delegating to ``_execution_port.cancel``) because the
            # latter does not pass ``variety``, which we need for
            # AMO/STOPLOSS orders.
            _CANCEL_RETRY_ATTEMPTS = 2
            _RETRYABLE_RESP_STATUSES = {"ERROR", "TIMEOUT", "UNKNOWN"}
            resp = None
            cancel_exc: Optional[Exception] = None
            for _attempt_idx in range(_CANCEL_RETRY_ATTEMPTS + 1):
                try:
                    resp = await cancel_fn(order_id, symbol=symbol, variety=variety)
                    cancel_exc = None
                except Exception as exc:
                    cancel_exc = exc
                    resp = None
                if cancel_exc is None and resp is not None:
                    _intermediate_status = str(
                        getattr(resp, "status", "") or ""
                    ).strip().upper()
                    if _intermediate_status not in _RETRYABLE_RESP_STATUSES:
                        break
                if _attempt_idx < _CANCEL_RETRY_ATTEMPTS:
                    # Brief async sleep before retry — avoid tight loop.
                    await asyncio.sleep(0.2 * (_attempt_idx + 1))
            if cancel_exc is not None:
                failed += 1
                errors.append({
                    "order_id": order_id,
                    "error": repr(cancel_exc),
                })
                continue
            assert resp is not None  # mypy hint; the loop above sets one
            resp_status = str(getattr(resp, "status", "") or "").strip().upper()
            if resp_status in _CANCELLED_RESPONSE_STATUSES:
                cancelled += 1
            elif resp_status in _FILLED_RACE_RESPONSE_STATUSES:
                # PR #240 round-1 review P2: broker raced us with a
                # fill — NOT a successful cancel; the operator may
                # need to flatten this new exposure.
                raced_filled += 1
                errors.append({
                    "order_id": order_id,
                    "broker_status": resp_status,
                    "note": (
                        "filled before cancel could land — new exposure "
                        "may need manual flatten"
                    ),
                })
            elif resp_status in _IDEMPOTENT_REJECTION_STATUSES:
                # PR #240 round-2 review P1: a REJECTED status alone is
                # not enough to call this idempotent. Angel returns
                # ``REJECTED`` with message ``cancel_failed`` for
                # genuine broker-level cancel rejections too, which
                # MUST count as failed. Only treat the response as
                # idempotent when the message explicitly indicates the
                # order is already gone / already cancelled / in a
                # terminal state.
                #
                # PR #240 round-6 review P2: "already filled" /
                # "already executed" markers are RACED FILLS — the
                # broker accepted a fill before the cancel could land,
                # so exposure was created. Route those to
                # ``raced_filled`` rather than ``skipped`` so the
                # operator is prompted to flatten the new position.
                resp_message = str(getattr(resp, "message", "") or "")
                _msg_lower = resp_message.lower()
                _filled_race_markers = (
                    "already filled",
                    "already_filled",
                    "already executed",
                    "already_executed",
                )
                _idempotent_markers = (
                    "not found",
                    "not_found",
                    "does not exist",
                    "already cancelled",
                    "already canceled",
                    "already_cancelled",
                    "already_canceled",
                    "order completed",
                    "order_completed",
                    "terminal state",
                    "terminal_state",
                )
                is_raced_filled = any(
                    m in _msg_lower for m in _filled_race_markers
                )
                is_idempotent = (
                    not is_raced_filled
                    and any(m in _msg_lower for m in _idempotent_markers)
                )
                if is_raced_filled:
                    raced_filled += 1
                    errors.append({
                        "order_id": order_id,
                        "broker_status": resp_status,
                        "broker_message": resp_message,
                        "note": (
                            "broker says order already filled/executed — "
                            "new exposure may need manual flatten"
                        ),
                    })
                elif is_idempotent:
                    skipped += 1
                    if resp_message:
                        errors.append({
                            "order_id": order_id,
                            "broker_status": resp_status,
                            "broker_message": resp_message,
                        })
                else:
                    # Genuine broker-level cancel rejection (e.g.
                    # Angel ``REJECTED:cancel_failed``). Must surface
                    # as failed so the dashboard does not report a
                    # green cancel-all when orders remain live.
                    failed += 1
                    errors.append({
                        "order_id": order_id,
                        "broker_status": resp_status,
                        "broker_message": resp_message or "rejected_no_message",
                    })
            elif resp_status in _CANCEL_FAILURE_STATUSES:
                # PR #240 round-1 review P1: real broker cancel failure.
                # Must NOT be silently treated as idempotent.
                failed += 1
                errors.append({
                    "order_id": order_id,
                    "broker_status": resp_status,
                    "broker_message": str(getattr(resp, "message", "") or ""),
                })
            else:
                # Unknown / pending status — be conservative, count
                # as failed so the operator investigates.
                failed += 1
                errors.append({
                    "order_id": order_id,
                    "broker_status": resp_status or "unknown",
                })
        # PR #240 round-2 review P2: a broker_orders refresh failure
        # means we could NOT verify the current broker open-order
        # set — fresh orders not in ``_last_orders`` may still be
        # live. Surface as partial so the dashboard does not show a
        # green ok cancel-all under that condition.
        per_account_status = (
            "ok"
            if (failed == 0 and raced_filled == 0 and refresh_error is None)
            else "partial"
        )
        per_account_results.append({
            "broker_account_id": str(acct_id),
            "status": per_account_status,
            "attempted": attempted,
            "cancelled": cancelled,
            "failed": failed,
            "skipped": skipped,
            "raced_filled": raced_filled,
            "broker_orders_refresh_failed": refresh_error is not None,
            "errors": errors[:20],  # cap for payload size
        })
        aggregate_attempted += attempted
        aggregate_cancelled += cancelled
        aggregate_failed += failed
        aggregate_skipped += skipped
        aggregate_raced_filled += raced_filled
        if refresh_error is not None:
            aggregate_refresh_failures += 1

    overall_status = (
        "ok"
        if (
            aggregate_failed == 0
            and aggregate_raced_filled == 0
            and aggregate_refresh_failures == 0
            and aggregate_out_of_scope == 0
        )
        else "partial"
    )
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
            "raced_filled": aggregate_raced_filled,
            "refresh_failures": aggregate_refresh_failures,
            # PR #240 round-5 review P2: scoped-admin runners outside
            # entitlement that were skipped.
            "out_of_scope": aggregate_out_of_scope,
            "overall_status": overall_status,
            "per_account": [
                {
                    "broker_account_id": r["broker_account_id"],
                    "status": r["status"],
                    "attempted": r["attempted"],
                    "cancelled": r["cancelled"],
                    "failed": r["failed"],
                    "skipped": r["skipped"],
                    "raced_filled": r["raced_filled"],
                    "broker_orders_refresh_failed": r.get(
                        "broker_orders_refresh_failed", False
                    ),
                    # PR #240 round-4 review P2: include the per-account
                    # error details (capped) in the audit metadata so
                    # incident reviewers can see WHICH order ids failed
                    # and WHY after the response is gone or the Safety
                    # page is reloaded.
                    "errors": r.get("errors", [])[:10],
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
        "raced_filled": aggregate_raced_filled,
        "refresh_failures": aggregate_refresh_failures,
        "out_of_scope": aggregate_out_of_scope,
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
    except ValueError as exc:
        # PR #240 round-5 review P2: ``issue_step_up_token`` now
        # rejects empty ``resource_id`` for ``kill_switch_clear`` /
        # ``kill_switch_rearm``. Translate that to a 422 with the
        # actionable error message so older clients / LIVE operators
        # see a correctable bad request rather than an internal 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
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
