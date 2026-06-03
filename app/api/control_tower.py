"""
Control Tower API for managing strategy enablement across tenants and accounts.
Builds a strategy matrix and updates Firestore configs when toggles change.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.audit_log import emit_audit_event
from app.core.identifiers import StrategyId, TenantId
from app.core.logging_utils import log_event
from app.core.rate_limit_middleware import check_rate_limit
from app.dashboard.auth import AdminContext, AdminRole, get_admin_context
from app.hub.routing_table import get_global_routing_table
from app.strategies.naming import canonicalize_strategy_name
from app.strategies.registry import list_all_strategies
from app.tenants.firestore_client import (
    get_all_tenants,
    get_broker_accounts_for_tenant,
    get_strategy_configs_for_tenant,
    get_strategy_configs_for_tenant_strategy,
    get_subscriptions_for_account,
    upsert_strategy_config,
)
from app.tenants.models import BrokerAccountModel, StrategyConfigModel, TenantModel
from app.tenants.subscription_service import is_subscription_active

router = APIRouter(prefix="/api/control_tower", tags=["control_tower"])
logger = logging.getLogger(__name__)


class StrategyColumn(BaseModel):
    strategy_id: str
    display_name: str


class TenantRow(BaseModel):
    tenant_id: str
    name: str | None = None


class ControlTowerCapability(BaseModel):
    read_only: bool
    mutation_enabled: bool
    routes_disabled: bool
    trade_mode: str
    reason_required: bool = True
    management_disabled_reason: str | None = None
    blocking_reasons: List[str] = Field(default_factory=list)


class ControlTowerAccountStatus(BaseModel):
    tenant_id: str
    broker_account_id: str
    display_name: str | None = None
    trading_mode: str | None = None


class ControlTowerStrategyConfigStatus(BaseModel):
    tenant_id: str
    broker_account_id: str
    strategy_id: str
    strategy_config_id: str
    enabled: bool


class ControlTowerMatrixResponse(BaseModel):
    tenants: List[TenantRow]
    strategies: List[StrategyColumn]
    matrix: Dict[str, Dict[str, bool]] = Field(
        default_factory=dict,
        description="matrix[tenant_id][strategy_id] -> enabled flag",
    )
    capability: ControlTowerCapability | None = None
    active_accounts: List[ControlTowerAccountStatus] = Field(default_factory=list)
    enabled_strategy_configs: List[ControlTowerStrategyConfigStatus] = Field(default_factory=list)
    routed_strategy_ids: List[str] = Field(default_factory=list)


class ControlTowerToggleRequest(BaseModel):
    tenant_id: str
    strategy_id: str
    enabled: bool
    reason: str | None = None


class ControlTowerToggleResponse(BaseModel):
    tenant_id: str
    strategy_id: str
    enabled: bool
    reason: str | None = None


class ControlTowerStatusResponse(ControlTowerMatrixResponse):
    capability: ControlTowerCapability


def _request_id_from_request(request: Request) -> str | None:
    return request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _trade_mode() -> str:
    return str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() or "PAPER"


def _control_tower_routes_disabled() -> bool:
    return _env_truthy("DISABLE_CONTROL_TOWER_ROUTES", default=False)


def _control_tower_mutation_config_enabled() -> bool:
    explicit_enabled = _env_truthy("CONTROL_TOWER_MUTATIONS_ENABLED", default=False)
    routes_disabled = _control_tower_routes_disabled()
    if _trade_mode() == "LIVE":
        return explicit_enabled and not routes_disabled
    return explicit_enabled or not routes_disabled


def _control_tower_kill_switch_blockers() -> list[str]:
    blockers: list[str] = []
    if _trade_mode() != "LIVE":
        return blockers
    try:
        from app.risk.kill_switch import get_kill_switch_state

        state = get_kill_switch_state()
    except Exception as exc:
        if _trade_mode() == "LIVE":
            blockers.append(f"kill-switch state unavailable: {exc}")
        return blockers

    try:
        active_count = int(state.get("active_count", 0) or 0)
    except Exception:
        active_count = 0
    if active_count > 0:
        blockers.append(f"durable kill switch active: {active_count} non-INACTIVE scope(s)")

    legacy = state.get("legacy_kill_switch") or {}
    divergence = state.get("divergence") or {}
    if bool(legacy.get("active")) or bool(state.get("kill_switch_activated")):
        blockers.append("legacy risk-manager kill switch is active")
    if bool(divergence.get("divergent")):
        blockers.append("legacy and durable kill-switch state are divergent")
    if _trade_mode() == "LIVE" and state.get("source") == "unavailable":
        blockers.append("kill-switch state unavailable")
    return blockers


def _sync_timestamp_stale(value: Any, *, interval_seconds: float) -> bool:
    if not value:
        return True
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > (2.0 * interval_seconds)
    except Exception:
        return True


def _control_tower_readiness_blockers() -> list[str]:
    if _trade_mode() != "LIVE":
        return []
    blockers: list[str] = []
    try:
        from app.runtime.app_runtime import get_app_runtime

        runtime = get_app_runtime()
    except Exception as exc:
        return [f"runtime readiness unavailable: {exc}"]

    if not bool(getattr(runtime, "ready", False)):
        blockers.append("runtime is not ready")

    schema_getter = getattr(runtime, "schema_status", None)
    if callable(schema_getter):
        try:
            schema = schema_getter()
            status_text = str(schema.get("status") if isinstance(schema, dict) else "").lower()
            if status_text in {"error", "degraded"}:
                blockers.append("schema guard is degraded")
            if isinstance(schema, dict) and (
                schema.get("missing_tables") or schema.get("missing_indexes")
            ):
                blockers.append("schema guard is missing required objects")
        except Exception as exc:
            blockers.append(f"schema guard unavailable: {exc}")

    try:
        from app.hub.runtime import get_hub_runtime

        hub_runtime = get_hub_runtime()
        hub = getattr(hub_runtime, "hub", None)
        state_store = getattr(hub_runtime, "state_store", None)
        if hub is None or state_store is None:
            blockers.append("broker/order/position sync state unavailable")
            return blockers
        if callable(getattr(hub, "list_runner_ids", None)):
            runner_ids = [str(v) for v in hub.list_runner_ids()]
        else:
            runner_ids = [str(v) for v in getattr(hub, "_runners", {}).keys()]
        if not runner_ids:
            blockers.append("no broker runners registered")
            return blockers
        try:
            pos_interval = float(os.getenv("POSITION_SYNC_INTERVAL_SECONDS", "30"))
        except Exception:
            pos_interval = 30.0
        try:
            ord_interval = float(os.getenv("ORDERS_SYNC_INTERVAL_SECONDS", "90"))
        except Exception:
            ord_interval = 90.0
        for account_id in runner_ids:
            positions_status = (
                state_store.get_positions_status(account_id)
                if callable(getattr(state_store, "get_positions_status", None))
                else {}
            )
            orders_status = (
                state_store.get_orders_status(account_id)
                if callable(getattr(state_store, "get_orders_status", None))
                else {}
            )
            if isinstance(positions_status, dict) and (
                bool(positions_status.get("stale"))
                or _sync_timestamp_stale(
                    positions_status.get("last_ok_ts"),
                    interval_seconds=pos_interval,
                )
            ):
                blockers.append(f"position sync stale for {account_id}")
            if isinstance(orders_status, dict) and (
                bool(orders_status.get("stale"))
                or _sync_timestamp_stale(
                    orders_status.get("orders_last_ok_ts"),
                    interval_seconds=ord_interval,
                )
            ):
                blockers.append(f"orders sync stale for {account_id}")
    except Exception as exc:
        blockers.append(f"broker/order/position sync unavailable: {exc}")
    return blockers


def _control_tower_capability() -> ControlTowerCapability:
    blockers: list[str] = []
    routes_disabled = _control_tower_routes_disabled()
    if routes_disabled:
        blockers.append("Control Tower management routes are disabled for safety")
    if not _control_tower_mutation_config_enabled():
        blockers.append(
            "Control Tower mutations require CONTROL_TOWER_MUTATIONS_ENABLED=true"
            if _trade_mode() == "LIVE"
            else "Control Tower mutations are disabled by configuration"
        )
    blockers.extend(_control_tower_kill_switch_blockers())
    blockers.extend(_control_tower_readiness_blockers())
    deduped_blockers = list(dict.fromkeys(reason for reason in blockers if reason))
    mutation_enabled = not deduped_blockers and _control_tower_mutation_config_enabled()
    return ControlTowerCapability(
        read_only=not mutation_enabled,
        mutation_enabled=mutation_enabled,
        routes_disabled=routes_disabled,
        trade_mode=_trade_mode(),
        management_disabled_reason=deduped_blockers[0] if deduped_blockers else None,
        blocking_reasons=deduped_blockers,
    )


def _require_control_tower_mutation_allowed() -> ControlTowerCapability:
    capability = _control_tower_capability()
    if not capability.mutation_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "Control Tower mutation blocked by safety gates.",
                "blocking_reasons": capability.blocking_reasons,
                "management_disabled_reason": capability.management_disabled_reason,
            },
        )
    return capability


def _context_scoped(ctx: AdminContext) -> bool:
    if ctx.all_tenants:
        return False
    auth_source = str(getattr(ctx, "auth_source", "") or "").strip()
    return bool(
        ctx.tenant_ids
        or ctx.broker_account_ids
        or auth_source in {"bearer", "ws_ticket"}
    )


def _tenant_allowed_for_context(tenant_id: object, ctx: AdminContext) -> bool:
    return bool(
        not _context_scoped(ctx)
        or ctx.can_access_tenant(str(tenant_id or "").strip())
    )


def _account_allowed_for_context(account: BrokerAccountModel, ctx: AdminContext) -> bool:
    if not _context_scoped(ctx):
        return True
    return bool(
        ctx.can_access_tenant(str(getattr(account, "tenant_id", "") or "").strip())
        and ctx.can_access_broker_account(
            str(getattr(account, "broker_account_id", "") or "").strip()
        )
    )


def _config_allowed_for_context(config: StrategyConfigModel, ctx: AdminContext) -> bool:
    if not _context_scoped(ctx):
        return True
    return bool(
        ctx.can_access_tenant(str(getattr(config, "tenant_id", "") or "").strip())
        and ctx.can_access_broker_account(
            str(getattr(config, "broker_account_id", "") or "").strip()
        )
    )


# Build the list of known strategies for the control tower matrix.
def _discover_strategy_columns() -> List[StrategyColumn]:
    """Build the list of strategies from the authoritative registry."""
    cols: List[StrategyColumn] = []
    for strategy_id, metadata in list_all_strategies().items():
        cols.append(
            StrategyColumn(
                strategy_id=strategy_id,
                display_name=str(metadata.display_name),
            )
        )
    cols.sort(key=lambda c: c.display_name.lower())
    return cols


# Combine multiple strategy configs into a single tenant-level on/off map.
def _aggregate_strategy_state(
    configs: List[StrategyConfigModel],
) -> Dict[str, bool]:
    """
    Collapse multiple strategy_config rows into a single tenant-level state.
    """
    state: Dict[str, bool] = {}
    for cfg in configs:
        sid = str(cfg.strategy_id)
        current = state.get(sid, False)
        state[sid] = current or bool(getattr(cfg, "enabled", False))
    return state


def _status_for_configs(
    configs: list[StrategyConfigModel],
) -> list[ControlTowerStrategyConfigStatus]:
    rows: list[ControlTowerStrategyConfigStatus] = []
    for cfg in configs:
        rows.append(
            ControlTowerStrategyConfigStatus(
                tenant_id=str(cfg.tenant_id),
                broker_account_id=str(cfg.broker_account_id),
                strategy_id=str(cfg.strategy_id),
                strategy_config_id=str(cfg.strategy_config_id),
                enabled=bool(getattr(cfg, "enabled", False)),
            )
        )
    return rows


def _routed_strategy_ids_from_runtime(configs: list[StrategyConfigModel]) -> list[str]:
    try:
        return sorted(get_global_routing_table().routed_strategy_ids())
    except Exception:
        return sorted({str(cfg.strategy_id) for cfg in configs if bool(getattr(cfg, "enabled", False))})


# Return tenant broker accounts that are enabled and have active subscriptions.
def _eligible_accounts_for_tenant(
    tenant_id: TenantId,
    ctx: AdminContext | None = None,
) -> List[BrokerAccountModel]:
    """
    Active broker accounts for a tenant that have at least one active subscription.
    """
    accounts = get_broker_accounts_for_tenant(tenant_id)
    eligible: List[BrokerAccountModel] = []
    for acct in accounts:
        if ctx is not None and not _account_allowed_for_context(acct, ctx):
            continue
        if not getattr(acct, "enabled", False):
            continue
        subs = get_subscriptions_for_account(acct.broker_account_id)
        if any(is_subscription_active(sub) for sub in subs):
            eligible.append(acct)
    return eligible


# Return the tenant-by-strategy enablement matrix.
@router.get("/matrix", response_model=ControlTowerMatrixResponse)
def get_control_tower_matrix(ctx: AdminContext = Depends(get_admin_context)) -> ControlTowerMatrixResponse:
    return get_control_tower_status(ctx)


@router.get("/status", response_model=ControlTowerStatusResponse)
def get_control_tower_status(ctx: AdminContext = Depends(get_admin_context)) -> ControlTowerStatusResponse:
    ctx.require_role(AdminRole.OPERATOR)
    tenants: List[TenantModel] = [
        tenant
        for tenant in get_all_tenants()
        if _tenant_allowed_for_context(getattr(tenant, "tenant_id", ""), ctx)
    ]
    strategies = _discover_strategy_columns()

    matrix: Dict[str, Dict[str, bool]] = {}
    active_accounts: list[ControlTowerAccountStatus] = []
    visible_configs: list[StrategyConfigModel] = []
    for tenant in tenants:
        tenant_id = str(tenant.tenant_id)
        configs = [
            cfg
            for cfg in get_strategy_configs_for_tenant(tenant.tenant_id)
            if _config_allowed_for_context(cfg, ctx)
        ]
        visible_configs.extend(configs)
        try:
            eligible_accounts = _eligible_accounts_for_tenant(tenant.tenant_id, ctx)
        except Exception:
            eligible_accounts = []
        for acct in eligible_accounts:
            active_accounts.append(
                ControlTowerAccountStatus(
                    tenant_id=str(getattr(acct, "tenant_id", tenant.tenant_id)),
                    broker_account_id=str(acct.broker_account_id),
                    display_name=getattr(acct, "display_name", None),
                    trading_mode=getattr(acct, "trading_mode", None),
                )
            )
        state = _aggregate_strategy_state(configs)
        row = {
            strat.strategy_id: bool(state.get(strat.strategy_id, False))
            for strat in strategies
        }
        matrix[tenant_id] = row

    tenant_rows = [
        TenantRow(tenant_id=str(t.tenant_id), name=getattr(t, "name", None))
        for t in tenants
    ]

    return ControlTowerStatusResponse(
        tenants=tenant_rows,
        strategies=strategies,
        matrix=matrix,
        capability=_control_tower_capability(),
        active_accounts=active_accounts,
        enabled_strategy_configs=[
            row for row in _status_for_configs(visible_configs) if row.enabled
        ],
        routed_strategy_ids=_routed_strategy_ids_from_runtime(visible_configs),
    )


# Toggle a strategy across eligible broker accounts for a tenant.
@router.post("/toggle", response_model=ControlTowerToggleResponse)
def toggle_control_tower(
    request: Request,
    req: ControlTowerToggleRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> ControlTowerToggleResponse:
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)
    capability = _require_control_tower_mutation_allowed()
    if not req.tenant_id or not req.strategy_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id and strategy_id are required",
        )
    reason = str(req.reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reason is required for Control Tower mutations",
        )
    if not _tenant_allowed_for_context(req.tenant_id, ctx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tenant is outside the caller's entitlement scope",
        )

    tenant_id = TenantId(req.tenant_id)
    strategy_id_raw = str(req.strategy_id or "").strip()
    canonical_strategy_id = canonicalize_strategy_name(
        strategy_id_raw,
        source="/api/control_tower/toggle",
        warn_alias=False,
    )
    if not canonical_strategy_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown strategy '{strategy_id_raw}'",
        )
    strategy_id = StrategyId(canonical_strategy_id)
    now = datetime.now(timezone.utc)

    existing_configs = [
        cfg
        for cfg in get_strategy_configs_for_tenant_strategy(tenant_id, strategy_id)
        if _config_allowed_for_context(cfg, ctx)
    ]
    previous_enabled = any(
        bool(getattr(cfg, "enabled", False))
        for cfg in existing_configs
        if cfg is not None
    )
    before_by_account = {
        str(cfg.broker_account_id): bool(getattr(cfg, "enabled", False))
        for cfg in existing_configs
        if cfg is not None
    }
    created_any = False

    if req.enabled:
        eligible_accounts = _eligible_accounts_for_tenant(tenant_id, ctx)
        existing_accounts = {
            cfg.broker_account_id for cfg in existing_configs if cfg is not None
        }
        for acct in eligible_accounts:
            if acct.broker_account_id in existing_accounts:
                continue
            cfg_id = f"{tenant_id}_{acct.broker_account_id}_{strategy_id}"
            model = StrategyConfigModel(
                strategy_config_id=cfg_id,
                tenant_id=tenant_id,
                broker_account_id=acct.broker_account_id,
                strategy_id=strategy_id,
                enabled=True,
                params={},
                created_at=now,
                updated_at=now,
            )
            upsert_strategy_config(model)
            created_any = True

    if existing_configs or created_any:
        target_configs = existing_configs
        if req.enabled and created_any:
            # Refresh configs if any were just created so updates happen uniformly
            target_configs = [
                cfg
                for cfg in get_strategy_configs_for_tenant_strategy(tenant_id, strategy_id)
                if _config_allowed_for_context(cfg, ctx)
            ]
        for cfg in target_configs:
            model = StrategyConfigModel(
                strategy_config_id=cfg.strategy_config_id,
                tenant_id=cfg.tenant_id,
                broker_account_id=cfg.broker_account_id,
                strategy_id=cfg.strategy_id,
                enabled=req.enabled,
                params=getattr(cfg, "params", {}) or {},
                created_at=getattr(cfg, "created_at", None),
                updated_at=now,
            )
            upsert_strategy_config(model)
    else:
        # No existing configs and enabling requested: create across eligible accounts.
        if req.enabled:
            eligible_accounts = _eligible_accounts_for_tenant(tenant_id, ctx)
            for acct in eligible_accounts:
                cfg_id = f"{tenant_id}_{acct.broker_account_id}_{strategy_id}"
                model = StrategyConfigModel(
                    strategy_config_id=cfg_id,
                    tenant_id=tenant_id,
                    broker_account_id=acct.broker_account_id,
                    strategy_id=strategy_id,
                    enabled=True,
                    params={},
                    created_at=now,
                    updated_at=now,
                )
                upsert_strategy_config(model)

    # Refresh routing table so ON/OFF takes effect without restart
    try:
        get_global_routing_table().refresh()
    except Exception:
        # Do not fail the API if refresh fails; callers can retry or refresh manually.
        pass

    emit_audit_event(
        actor=ctx.caller,
        action="toggle_control_tower",
        resource_type="strategy_config",
        resource_id=f"{tenant_id}:{strategy_id}",
        before={
            "tenant_id": str(tenant_id),
            "strategy_id": str(strategy_id),
            "enabled": previous_enabled,
            "broker_accounts": before_by_account,
        },
        after={
            "tenant_id": str(tenant_id),
            "strategy_id": str(strategy_id),
            "enabled": req.enabled,
            "broker_account_ids": sorted(
                {
                    str(cfg.broker_account_id)
                    for cfg in get_strategy_configs_for_tenant_strategy(tenant_id, strategy_id)
                    if _config_allowed_for_context(cfg, ctx)
                }
            ),
        },
        request_id=_request_id_from_request(request),
        metadata={
            "actor": ctx.caller,
            "tenant_id": str(tenant_id),
            "strategy_id": str(strategy_id),
            "old_value": previous_enabled,
            "new_value": bool(req.enabled),
            "reason": reason,
            "request_id": _request_id_from_request(request),
            "timestamp": now.isoformat(),
            "capability": capability.model_dump() if hasattr(capability, "model_dump") else capability.dict(),
        },
    )
    log_event(
        logger,
        event_type="CONTROL_TOWER_TOGGLE",
        message="Control Tower toggle applied.",
        tenant_id=tenant_id,
        strategy_id=strategy_id,
        request_id=_request_id_from_request(request),
        actor=ctx.caller,
        reason=reason,
        previous_enabled=previous_enabled,
        enabled=req.enabled,
        created_any=created_any,
        existing_config_count=len(existing_configs),
    )

    return ControlTowerToggleResponse(
        tenant_id=str(tenant_id),
        strategy_id=str(strategy_id),
        enabled=req.enabled,
        reason=reason,
    )


__all__ = [
    "router",
    "get_control_tower_matrix",
    "get_control_tower_status",
    "toggle_control_tower",
]
