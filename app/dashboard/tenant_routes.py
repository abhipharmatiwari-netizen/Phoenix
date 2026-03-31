"""
Tenant-facing APIs.

All endpoints require admin authentication plus a TenantContext
(X-Tenant-Id header) and return data scoped to that tenant only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dashboard.auth import TenantContext, get_admin_context, get_tenant_context
from app.tenants.firestore_client import (
    get_broker_accounts_for_tenant,
    get_broker_account,
)
from app.hub.runtime import get_hub_runtime
from app.data.bq_persister import fetch_trades_for_tenant
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, StrategyId
from app.core.lot_size import lot_size_for_symbol, qty_to_lots
from app.pnl.pnl_engine import PnLEngine
from app.pnl.types import PnLSnapshot

router = APIRouter(
    prefix="/tenant",
    tags=["tenant"],
    dependencies=[Depends(get_admin_context)],
)


def _coerce_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except Exception:
        return 0


def _coerce_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _normalize_position_view(pos: Any) -> dict[str, Any]:
    if isinstance(pos, dict):
        row = dict(pos)
        symbol = str(
            row.get("symbol")
            or row.get("tradingsymbol")
            or row.get("trading_symbol")
            or ""
        ).strip()
        token = (
            row.get("symboltoken")
            or row.get("token")
            or row.get("instrument_token")
        )
        qty = _coerce_int(
            row.get("quantity", row.get("qty", row.get("net_qty", row.get("netqty"))))
        )
        avg_price = _coerce_float(
            row.get("avg_price")
            or row.get("avgPrice")
            or row.get("average_price")
            or row.get("avgprice")
            or row.get("entry_price")
        )
        entry_price = _coerce_float(
            row.get("entry_price")
            or row.get("entryPrice")
            or row.get("avg_price")
            or row.get("avgPrice")
            or row.get("average_price")
            or row.get("avgprice")
        )
        entry_ts = row.get("entry_ts") or row.get("entryTs") or row.get("entry_ts_utc")
        product_type = (
            row.get("product_type")
            or row.get("productType")
            or row.get("producttype")
            or ""
        )
    else:
        symbol = str(
            getattr(pos, "symbol", None)
            or getattr(pos, "tradingsymbol", None)
            or getattr(pos, "trading_symbol", None)
            or ""
        ).strip()
        token = (
            getattr(pos, "symboltoken", None)
            or getattr(pos, "token", None)
            or getattr(pos, "instrument_token", None)
            or getattr(pos, "symbol_token", None)
        )
        qty = _coerce_int(
            getattr(pos, "quantity", None)
            or getattr(pos, "qty", None)
            or getattr(pos, "net_qty", None)
        )
        avg_price = _coerce_float(
            getattr(pos, "avg_price", None)
            or getattr(pos, "avgPrice", None)
            or getattr(pos, "average_price", None)
            or getattr(pos, "avgprice", None)
            or getattr(pos, "entry_price", None)
        )
        entry_price = _coerce_float(
            getattr(pos, "entry_price", None)
            or getattr(pos, "entryPrice", None)
            or getattr(pos, "avg_price", None)
            or getattr(pos, "avgPrice", None)
            or getattr(pos, "average_price", None)
            or getattr(pos, "avgprice", None)
        )
        entry_ts = (
            getattr(pos, "entry_ts", None)
            or getattr(pos, "entryTs", None)
            or getattr(pos, "entry_ts_utc", None)
        )
        product_type = getattr(pos, "product_type", "")
        if hasattr(product_type, "value"):
            product_type = product_type.value
        product_type = str(product_type or "")

    token_text = str(token).strip() if token not in (None, "") else None
    ltp = dashboard_bus.get_last_price_for_instrument(
        symbol=symbol or None,
        token=token_text,
    )
    if ltp is None and symbol:
        ltp = dashboard_bus.get_last_price(symbol)

    side = "BUY" if qty > 0 else "SELL" if qty < 0 else ""
    lot_size = lot_size_for_symbol(symbol)
    qty_lots = qty_to_lots(qty, symbol)
    unrealized_pnl = round((ltp - avg_price) * qty, 2) if ltp is not None else None

    return {
        "symbol": symbol,
        "token": token_text,
        "quantity": qty,
        "net_qty": qty,
        "qty_lots": qty_lots,
        "lot_size": lot_size,
        "avg_price": avg_price,
        "entry_price": entry_price,
        "entry_ts": entry_ts,
        "product_type": product_type,
        "side": side,
        "ltp": ltp,
        "unrealized_pnl": unrealized_pnl,
    }


def _build_live_account_mark_snapshot(positions: list[Any]) -> dict[str, float]:
    unrealized_total = 0.0
    gross_exposure = 0.0
    for pos in positions:
        view = _normalize_position_view(pos)
        qty = int(view["quantity"] or 0)
        if qty == 0:
            continue
        mark_price = view["ltp"]
        if mark_price is not None:
            unrealized_total += float(view["unrealized_pnl"] or 0.0)
        gross_exposure += abs(qty) * float(
            mark_price if mark_price is not None else view["avg_price"]
        )
    return {
        "unrealized_pnl": round(unrealized_total, 2),
        "gross_exposure": round(gross_exposure, 2),
    }


# List broker accounts for the current tenant.
@router.get("/me/accounts")
async def list_my_accounts(
    ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Return broker accounts belonging to the current tenant.
    """
    accounts = get_broker_accounts_for_tenant(ctx.tenant_id)
    return {"tenant_id": ctx.tenant_id, "accounts": accounts}


# Return balance for a tenant's broker account.
@router.get("/me/accounts/{broker_account_id}/balance")
async def get_account_balance(
    broker_account_id: BrokerAccountId,
    ctx: TenantContext = Depends(get_tenant_context),
):
    account = get_broker_account(broker_account_id)
    if account is None or account.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found for this tenant.",
        )

    runtime = get_hub_runtime()
    balance = runtime.state_store.get_balance(broker_account_id)
    return {
        "tenant_id": ctx.tenant_id,
        "broker_account_id": broker_account_id,
        "balance": balance,
    }


# Return normalized open positions for a tenant's broker account.
@router.get("/me/accounts/{broker_account_id}/positions")
async def get_account_positions(
    broker_account_id: BrokerAccountId,
    ctx: TenantContext = Depends(get_tenant_context),
):
    account = get_broker_account(broker_account_id)
    if account is None or account.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found for this tenant.",
        )

    runtime = get_hub_runtime()
    positions = runtime.state_store.get_positions(broker_account_id)
    normalized = []
    for pos in positions:
        row = dict(pos) if isinstance(pos, dict) else {}
        row.update(_normalize_position_view(pos))
        normalized.append(row)
    return {
        "tenant_id": ctx.tenant_id,
        "broker_account_id": broker_account_id,
        "positions": normalized,
    }


# Return order history for a tenant's broker account.
@router.get("/me/accounts/{broker_account_id}/orders")
async def get_account_orders(
    broker_account_id: BrokerAccountId,
    ctx: TenantContext = Depends(get_tenant_context),
):
    account = get_broker_account(broker_account_id)
    if account is None or account.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found for this tenant.",
        )

    runtime = get_hub_runtime()
    orders = runtime.state_store.get_order_snapshot(broker_account_id)
    if not orders:
        orders = runtime.state_store.get_orders(broker_account_id)
    return {
        "tenant_id": ctx.tenant_id,
        "broker_account_id": broker_account_id,
        "orders": orders,
    }


# Return PnL snapshot for a tenant's broker account and optional strategy.
@router.get("/me/accounts/{broker_account_id}/pnl")
async def get_account_pnl(
    broker_account_id: BrokerAccountId,
    strategy_id: Optional[StrategyId] = Query(default=None, alias="strategy_id"),
    ctx: TenantContext = Depends(get_tenant_context),
):
    account = get_broker_account(broker_account_id)
    if account is None or account.tenant_id != ctx.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Broker account not found for this tenant.",
        )

    runtime = get_hub_runtime()
    pnl_engine: PnLEngine = runtime.pnl_engine
    positions = runtime.state_store.get_positions(broker_account_id)
    live_mark = _build_live_account_mark_snapshot(positions)
    now = datetime.now(timezone.utc)

    # Strategy-specific snapshot when requested.
    if strategy_id:
        snapshot: Optional[PnLSnapshot] = pnl_engine.get_snapshot(
            tenant_id=ctx.tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
        )
        if snapshot is not None:
            mark_source = str(snapshot.freshness_source or "").strip().lower()
            unrealized_pnl = (
                float(snapshot.unrealized_pnl or 0.0)
                if mark_source == "mark_update"
                else float(live_mark["unrealized_pnl"])
            )
            gross_exposure = (
                float(snapshot.gross_exposure or 0.0)
                if mark_source == "mark_update"
                else float(live_mark["gross_exposure"])
            )
            return {
                "tenant_id": ctx.tenant_id,
                "broker_account_id": broker_account_id,
                "strategy_id": strategy_id,
                "pnl": {
                    "realized_pnl": float(snapshot.realized_pnl or 0.0),
                    "unrealized_pnl": round(unrealized_pnl, 2),
                    "gross_exposure": round(gross_exposure, 2),
                    "as_of": (snapshot.as_of or now).isoformat(),
                    "session_date": (
                        snapshot.session_date.isoformat()
                        if snapshot.session_date is not None
                        else None
                    ),
                },
            }

    # Account-level aggregate: combine realized PnL snapshots with live position marks.
    realized = pnl_engine.get_current_realized_pnl(
        tenant_id=ctx.tenant_id,
        broker_account_id=broker_account_id,
    )
    realized_f = float(realized or 0.0)

    return {
        "tenant_id": ctx.tenant_id,
        "broker_account_id": broker_account_id,
        "strategy_id": strategy_id,
        "pnl": {
            "realized_pnl": realized_f,
            "unrealized_pnl": float(live_mark["unrealized_pnl"]),
            "gross_exposure": float(live_mark["gross_exposure"]),
            "as_of": now.isoformat(),
        },
    }


# Return recent trades for the tenant, optionally filtered.
@router.get("/me/trades")
async def get_my_trades(
    ctx: TenantContext = Depends(get_tenant_context),
    broker_account_id: Optional[BrokerAccountId] = Query(default=None),
    from_time: Optional[datetime] = Query(default=None),
    to_time: Optional[datetime] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
):
    """
    Return recent trades for the current tenant, optionally filtered by
    broker_account_id and time window.
    """
    trades = fetch_trades_for_tenant(
        tenant_id=ctx.tenant_id,
        broker_account_id=broker_account_id,
        start_time=from_time,
        end_time=to_time,
        limit=limit,
    )
    return {
        "tenant_id": ctx.tenant_id,
        "count": len(trades),
        "trades": trades,
    }


__all__ = ["router"]
