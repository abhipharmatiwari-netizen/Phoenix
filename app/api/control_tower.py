"""
Control Tower API for managing strategy enablement across tenants and accounts.
Builds a strategy matrix and updates Firestore configs when toggles change.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Dict, List

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


class ControlTowerMatrixResponse(BaseModel):
    tenants: List[TenantRow]
    strategies: List[StrategyColumn]
    matrix: Dict[str, Dict[str, bool]] = Field(
        default_factory=dict,
        description="matrix[tenant_id][strategy_id] -> enabled flag",
    )


class ControlTowerToggleRequest(BaseModel):
    tenant_id: str
    strategy_id: str
    enabled: bool


class ControlTowerToggleResponse(BaseModel):
    tenant_id: str
    strategy_id: str
    enabled: bool


def _request_id_from_request(request: Request) -> str | None:
    return request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")


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


# Return tenant broker accounts that are enabled and have active subscriptions.
def _eligible_accounts_for_tenant(tenant_id: TenantId) -> List[BrokerAccountModel]:
    """
    Active broker accounts for a tenant that have at least one active subscription.
    """
    accounts = get_broker_accounts_for_tenant(tenant_id)
    eligible: List[BrokerAccountModel] = []
    for acct in accounts:
        if not getattr(acct, "enabled", False):
            continue
        subs = get_subscriptions_for_account(acct.broker_account_id)
        if any(is_subscription_active(sub) for sub in subs):
            eligible.append(acct)
    return eligible


# Return the tenant-by-strategy enablement matrix.
@router.get("/matrix", response_model=ControlTowerMatrixResponse)
def get_control_tower_matrix() -> ControlTowerMatrixResponse:
    tenants: List[TenantModel] = get_all_tenants()
    strategies = _discover_strategy_columns()

    matrix: Dict[str, Dict[str, bool]] = {}
    for tenant in tenants:
        tenant_id = str(tenant.tenant_id)
        configs = get_strategy_configs_for_tenant(tenant.tenant_id)
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

    return ControlTowerMatrixResponse(
        tenants=tenant_rows,
        strategies=strategies,
        matrix=matrix,
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
    if not req.tenant_id or not req.strategy_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id and strategy_id are required",
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

    existing_configs = get_strategy_configs_for_tenant_strategy(
        tenant_id, strategy_id
    )
    previous_enabled = any(
        bool(getattr(cfg, "enabled", False))
        for cfg in existing_configs
        if cfg is not None
    )
    created_any = False

    if req.enabled:
        eligible_accounts = _eligible_accounts_for_tenant(tenant_id)
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
            target_configs = get_strategy_configs_for_tenant_strategy(
                tenant_id, strategy_id
            )
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
            eligible_accounts = _eligible_accounts_for_tenant(tenant_id)
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
        },
        after={
            "tenant_id": str(tenant_id),
            "strategy_id": str(strategy_id),
            "enabled": req.enabled,
        },
        request_id=_request_id_from_request(request),
    )
    log_event(
        logger,
        event_type="CONTROL_TOWER_TOGGLE",
        message="Control Tower toggle applied.",
        tenant_id=tenant_id,
        strategy_id=strategy_id,
        request_id=_request_id_from_request(request),
        actor=ctx.caller,
        previous_enabled=previous_enabled,
        enabled=req.enabled,
        created_any=created_any,
        existing_config_count=len(existing_configs),
    )

    return ControlTowerToggleResponse(
        tenant_id=str(tenant_id),
        strategy_id=str(strategy_id),
        enabled=req.enabled,
    )


__all__ = [
    "router",
    "get_control_tower_matrix",
    "toggle_control_tower",
]
