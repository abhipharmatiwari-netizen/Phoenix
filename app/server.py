"""
Trading hub API service with stream worker, admin, and dashboard endpoints.
Manages worker lifecycle, health probes, webhooks, and admin control routes.
"""



from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    FastAPI,
    Request,
    WebSocket,
    WebSocketDisconnect,
    Header,
    Query,
    status,
    Depends,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config.settings import get_settings
from app.brokers.base import OrderStatus
from app.core.audit_log import emit_audit_event
from app.core.dashboard_bus import dashboard_bus
from app.core.feature_flags import load_stability_feature_flags
from app.core.identifiers import BrokerAccountId, TenantId
from app.core.rate_limit_middleware import check_rate_limit
from app.core.lot_size import lot_size_for_symbol, qty_to_lots
from app.core.logging_utils import log_event, reset_log_context, set_log_context
from app.dashboard.admin_routes import router as admin_router
from app.dashboard.auth import (
    AdminContext,
    AdminRole,
    TenantContext,
    get_admin_context,
    get_tenant_context,
    issue_dashboard_ws_ticket,
    safe_compare_token,
    verify_dashboard_ws_ticket,
)
from app.dashboard import tenant_routes
from app.api.auth_routes import router as auth_router
from app.tenants.firestore_client import get_broker_account_by_client_code, get_broker_accounts_for_tenant
from app.hub.runtime import get_hub_runtime
from app.api.control_tower import router as control_tower_router
from app.api.bff_proxy import router as bff_proxy_router
from app.config.boot_config import RuntimeConfig, get_boot_config
from app.runtime.app_runtime import get_app_runtime
from app.strategies.naming import (
    canonicalize_strategy_name,
    canonicalize_strategy_names,
    strategy_alias_usage_snapshot,
)
from pydantic import BaseModel
from fastapi import HTTPException

logger = logging.getLogger(__name__)
_REAL_ASYNCIO_SLEEP = asyncio.sleep
_ACTIVE_DASHBOARD_SOCKETS: dict[int, WebSocket] = {}
_ACTIVE_DASHBOARD_SOCKETS_LOCK = threading.Lock()


def refresh_daily_levels_cache() -> bool:
    from app.runners.multi_instrument_stream import (
        refresh_daily_levels_cache as _refresh_daily_levels_cache,
    )

    return bool(_refresh_daily_levels_cache())


def _boot_config_created_at_for_health() -> str | None:
    try:
        boot_cfg = get_boot_config()
    except Exception as exc:
        logger.warning(
            "Health probe could not load boot config snapshot; returning degraded metadata only: %s",
            exc,
        )
        return None
    return getattr(boot_cfg, "created_at_utc", None)


def _dashboard_ws_prefers_delta(requested_mode: str | None) -> bool:
    mode = str(requested_mode or "").strip().lower()
    if mode == "delta":
        return True
    if mode == "full":
        return False
    try:
        app_env = str(get_boot_config().runtime.app_env or "").strip().lower()
    except Exception:
        app_env = "local"
    return app_env not in {"local", "dev", "test"}


def _dashboard_auth_required(runtime_cfg: RuntimeConfig | None = None) -> bool:
    cfg = runtime_cfg
    if cfg is None:
        try:
            cfg = get_boot_config().runtime
        except Exception:
            cfg = RuntimeConfig.from_env(dict(os.environ))
    app_env = str(getattr(cfg, "app_env", "local") or "local").strip().lower()
    if app_env in {"local", "dev", "test"}:
        return not bool(getattr(cfg, "dashboard_auth_disabled", False))
    return True


def _readiness_trade_mode() -> str:
    try:
        trade_mode = str(get_boot_config().env.get("TRADE_MODE", "PAPER") or "PAPER")
    except Exception:
        trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER")
    return trade_mode.strip().upper() or "PAPER"


def _stream_worker_expected_for_readiness(runtime: Any) -> bool:
    lease_owned = True
    lease_getter = getattr(runtime, "leader_lease_status", None)
    if callable(lease_getter):
        try:
            lease_owned = bool(dict(lease_getter()).get("owned", True))
        except Exception:
            lease_owned = True

    disable_stream_worker = False
    try:
        disable_stream_worker = bool(
            getattr(get_boot_config().runtime, "disable_stream_worker", False)
        )
    except Exception:
        try:
            disable_stream_worker = bool(
                RuntimeConfig.from_env(dict(os.environ)).disable_stream_worker
            )
        except Exception:
            disable_stream_worker = False

    return bool(lease_owned and not disable_stream_worker)


def _leader_lease_readiness_snapshot(runtime: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "enabled": False,
        "owned": True,
        "task_running": False,
    }
    lease_getter = getattr(runtime, "leader_lease_status", None)
    if callable(lease_getter):
        try:
            raw = dict(lease_getter())
        except Exception:
            raw = {}
        if "enabled" in raw:
            snapshot["enabled"] = bool(raw.get("enabled"))
        if "owned" in raw:
            snapshot["owned"] = bool(raw.get("owned"))
        if "task_running" in raw:
            snapshot["task_running"] = bool(raw.get("task_running"))
        for key in ("backend", "lease_id", "owner_id", "renew_failures", "last_failure_at"):
            if raw.get(key) is not None:
                snapshot[key] = raw.get(key)
    return snapshot


def _stream_worker_readiness_snapshot(runtime: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "expected": _stream_worker_expected_for_readiness(runtime),
        "running": bool(runtime.stream_worker_running()),
        "watchdog_running": bool(runtime.watchdog_running()),
        "fatal_error": None,
    }
    status_getter = getattr(runtime, "stream_worker_status", None)
    if callable(status_getter):
        try:
            raw = dict(status_getter())
        except Exception:
            raw = {}
        if "running" in raw:
            snapshot["running"] = bool(raw.get("running"))
        if "fatal_error" in raw and raw.get("fatal_error"):
            snapshot["fatal_error"] = str(raw.get("fatal_error"))
    if not snapshot["fatal_error"]:
        fatal_getter = getattr(runtime, "stream_worker_fatal_error", None)
        if callable(fatal_getter):
            try:
                fatal_error = fatal_getter()
            except Exception:
                fatal_error = None
            if fatal_error:
                snapshot["fatal_error"] = str(fatal_error)
    return snapshot


def _dashboard_ws_mode_token(requested_mode: str | None) -> str:
    return "delta" if _dashboard_ws_prefers_delta(requested_mode) else "full"


def _dashboard_ws_path(websocket: WebSocket) -> str:
    url = getattr(websocket, "url", None)
    path = str(getattr(url, "path", "") or "").strip()
    return path or "/ws/dashboard"


def _build_dashboard_payload(*, settings: Any, use_delta: bool, first_message: bool) -> dict[str, Any]:
    if use_delta and first_message:
        payload = dashboard_bus.delta_snapshot(full_interval=1)
    else:
        payload = dashboard_bus.delta_snapshot() if use_delta else dashboard_bus.snapshot()
    if not use_delta:
        payload["_mode"] = "full_snapshot"
    payload["multi_hub_enabled"] = settings.enable_multi_hub
    if settings.enable_multi_hub:
        try:
            hub_positions = _build_hub_positions_snapshot()
            payload["hub_positions"] = hub_positions
            payload["pnl"] = _build_hub_pnl_snapshot(hub_positions)
        except Exception as exc:  # pragma: no cover - dashboard best-effort
            logger.error("Dashboard hub positions snapshot failed: %s", exc)
            payload["hub_positions"] = []
    return payload


async def _dashboard_ws_sender_loop(
    websocket: WebSocket,
    *,
    settings: Any,
    use_delta: bool,
) -> None:
    first_message = True
    while True:
        payload = _build_dashboard_payload(
            settings=settings,
            use_delta=use_delta,
            first_message=first_message,
        )
        first_message = False
        await websocket.send_text(json.dumps(payload))
        await asyncio.sleep(1.0)
        await _REAL_ASYNCIO_SLEEP(0)


async def _dashboard_ws_disconnect_watcher(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=int(message.get("code", 1000) or 1000))


def _register_dashboard_socket(websocket: WebSocket) -> None:
    with _ACTIVE_DASHBOARD_SOCKETS_LOCK:
        _ACTIVE_DASHBOARD_SOCKETS[id(websocket)] = websocket


def _unregister_dashboard_socket(websocket: WebSocket) -> None:
    with _ACTIVE_DASHBOARD_SOCKETS_LOCK:
        _ACTIVE_DASHBOARD_SOCKETS.pop(id(websocket), None)


async def close_active_dashboard_sockets() -> None:
    with _ACTIVE_DASHBOARD_SOCKETS_LOCK:
        sockets = list(_ACTIVE_DASHBOARD_SOCKETS.values())
        _ACTIVE_DASHBOARD_SOCKETS.clear()
    if not sockets:
        return
    logger.info("Closing active dashboard websockets count=%d", len(sockets))
    for websocket in sockets:
        try:
            await websocket.close(code=1001)
        except Exception:
            continue

# Manage app startup/shutdown via a single runtime orchestrator.
@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.session_store import _load_revoked_from_postgres
    _load_revoked_from_postgres()
    runtime = get_app_runtime()
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()
        await close_active_dashboard_sockets()


app = FastAPI(title="Multi-instrument stream", lifespan=lifespan)

_cors_env = os.getenv("CORS_ORIGINS", "http://localhost:3000")
origins = [o.strip() for o in _cors_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_context_middleware(request: Request, call_next):
    correlation_id = (
        request.headers.get("X-Correlation-Id")
        or request.headers.get("X-Request-Id")
        or uuid4().hex
    )
    request_id = request.headers.get("X-Request-Id") or correlation_id
    request.state.correlation_id = correlation_id
    request.state.request_id = request_id
    tokens = set_log_context(
        correlation_id=correlation_id,
        request_id=request_id,
    )
    try:
        response = await call_next(request)
    finally:
        reset_log_context(tokens)
    response.headers.setdefault("X-Correlation-Id", correlation_id)
    response.headers.setdefault("X-Request-Id", request_id)
    return response


_app_runtime = get_app_runtime()
_runtime_cfg = RuntimeConfig.from_env(dict(os.environ))
strategy_switchboard = _app_runtime.strategy_switchboard
instrument_controller = _app_runtime.instrument_controller
app.include_router(admin_router)
# Auth router is always mounted — it provides user login/register/me endpoints.
# The legacy demo_auth gate is no longer used; auth_routes has its own guards.
app.include_router(auth_router)
app.include_router(tenant_routes.router)
if not _runtime_cfg.disable_control_tower_routes:
    app.include_router(
        control_tower_router, dependencies=[Depends(get_admin_context)]
    )
app.include_router(bff_proxy_router)


# Legacy compatibility endpoints (for backwards-compat with older clients)
# Serve the legacy /positions endpoint by delegating to tenant-scoped state.
@app.get("/positions")
async def get_positions_legacy(
    broker_account_id: str = Query(None, alias="broker_account_id"),
    tenant_ctx: TenantContext = Depends(get_tenant_context),
):
    """
    Legacy endpoint for backward compatibility.
    Delegates to tenant-scoped authorization and server-side tenant entitlements.

    Requires either:
    - Authorization or X-Admin-Key plus X-Tenant-Id when multiple tenants are available
    - Or Authorization or X-Admin-Key only when a single tenant/account can be resolved
    """
    resolved_tenant_id = str(tenant_ctx.tenant_id)

    if not broker_account_id:
        accounts = list(get_broker_accounts_for_tenant(resolved_tenant_id))
        if tenant_ctx.broker_account_ids:
            accounts = [
                account for account in accounts
                if str(getattr(account, "broker_account_id", "") or "") in tenant_ctx.broker_account_ids
            ]
        if not accounts:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No broker accounts found for tenant",
            )
        first_account = accounts[0]
        broker_account_id = str(
            getattr(first_account, "broker_account_id", None)
            or (first_account.get("broker_account_id") if isinstance(first_account, dict) else "")
            or ""
        )

    runtime = get_hub_runtime()
    try:
        positions = runtime.state_store.get_positions(broker_account_id)
        return {
            "tenant_id": resolved_tenant_id,
            "broker_account_id": broker_account_id,
            "positions": positions,
        }
    except Exception as e:
        logger.warning(f"Failed to get positions for {broker_account_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failed to retrieve positions",
        )


class ToggleRequest(BaseModel):
    name: str
    enabled: bool


class InstrumentUpdateRequest(BaseModel):
    underlying: str
    enabled: bool | None = None
    allowed_strategies: list[str] | None = None


class DailyLevelsRefreshResponse(BaseModel):
    status: str


class DashboardWebsocketTicketResponse(BaseModel):
    ticket: str
    expires_at: str
    ttl_seconds: int
    mode: str
    path: str


DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"
dashboard_bus.set_mode(_runtime_cfg.runner_mode)


# Compile a dashboard snapshot of positions across all running accounts.
def _build_hub_positions_snapshot() -> list[dict[str, Any]]:
    runtime = get_hub_runtime()
    hub = runtime.hub
    rows: list[dict[str, Any]] = []
    for broker_account_id in hub.list_runner_ids():
        positions = runtime.state_store.get_positions(broker_account_id)
        if not positions:
            continue
        runner = hub.get_runner(broker_account_id)
        tenant_id = runner.tenant_id if runner else ""
        for pos in positions:
            if isinstance(pos, dict):
                raw_qty = pos.get("quantity", pos.get("qty", pos.get("net_qty")))
                symbol = str(
                    pos.get("symbol")
                    or pos.get("tradingsymbol")
                    or pos.get("trading_symbol")
                    or ""
                ).strip()
                avg_price = (
                    pos.get("avg_price")
                    or pos.get("avgPrice")
                    or pos.get("average_price")
                    or pos.get("avgprice")
                    or 0.0
                )
                symbol_token = (
                    pos.get("symboltoken")
                    or pos.get("token")
                    or pos.get("instrument_token")
                )
                entry_ts = pos.get("entry_ts") or pos.get("entryTs")
                product_type = (
                    pos.get("product_type")
                    or pos.get("productType")
                    or pos.get("producttype")
                    or ""
                )
            else:
                raw_qty = getattr(pos, "quantity", None)
                symbol = str(getattr(pos, "symbol", "") or "").strip()
                avg_price = getattr(pos, "avg_price", 0.0) or 0.0
                symbol_token = getattr(pos, "symbol_token", None) or getattr(
                    pos, "token", None
                )
                entry_ts = getattr(pos, "entry_ts", None)
                product_type = getattr(pos, "product_type", "")
                if hasattr(product_type, "value"):
                    product_type = product_type.value
                product_type = str(product_type)
            try:
                qty = int(raw_qty or 0)
            except Exception:
                qty = 0
            if qty == 0 or not symbol:
                continue
            side = "BUY" if qty > 0 else "SELL"
            lot_size = lot_size_for_symbol(symbol)
            qty_lots = qty_to_lots(qty, symbol)
            last_price = dashboard_bus.get_last_price_for_instrument(
                symbol=symbol,
                token=str(symbol_token) if symbol_token not in (None, "") else None,
            )
            rows.append(
                {
                    "tenant_id": str(tenant_id),
                    "broker_account_id": str(broker_account_id),
                    "symbol": symbol,
                    "side": side,
                    "qty": abs(qty),
                    "qty_lots": abs(qty_lots),
                    "lot_size": lot_size,
                    "net_qty": qty,
                    "avg_price": float(avg_price or 0.0),
                    "entry_price": float(avg_price or 0.0),
                    "entry_ts": entry_ts,
                    "product_type": product_type,
                    "ltp": last_price,
                }
            )
    return rows


def _build_hub_pnl_snapshot(
    hub_positions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runtime = get_hub_runtime()
    pnl_engine = getattr(runtime, "pnl_engine", None)

    realized_total = 0.0
    if pnl_engine is not None:
        seen_accounts: set[tuple[str, str]] = set()
        for broker_account_id in runtime.hub.list_runner_ids():
            runner = runtime.hub.get_runner(broker_account_id)
            tenant_id = str(getattr(runner, "tenant_id", "") or "").strip()
            account_id = str(broker_account_id or "").strip()
            if not tenant_id or not account_id:
                continue
            account_key = (tenant_id, account_id)
            if account_key in seen_accounts:
                continue
            seen_accounts.add(account_key)
            try:
                realized = pnl_engine.get_current_realized_pnl(
                    tenant_id=TenantId(tenant_id),
                    broker_account_id=BrokerAccountId(account_id),
                )
            except Exception as exc:  # pragma: no cover - dashboard best-effort
                logger.warning(
                    "Dashboard hub realized PnL snapshot failed for %s/%s: %s",
                    tenant_id,
                    account_id,
                    exc,
                )
                continue
            if realized is not None:
                realized_total += float(realized)

    open_total = 0.0
    by_instrument: dict[str, float] = {}
    for row in hub_positions or []:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        net_qty = _coerce_float(row.get("net_qty"))
        if net_qty in (None, 0.0):
            continue
        entry_price = _coerce_float(row.get("entry_price"))
        if entry_price is None:
            entry_price = _coerce_float(row.get("avg_price")) or 0.0
        mark_price = _coerce_float(row.get("ltp"))
        if mark_price is None:
            mark_price = entry_price
        pnl = (mark_price - entry_price) * net_qty
        open_total += pnl
        by_instrument[symbol] = round(by_instrument.get(symbol, 0.0) + pnl, 2)

    realized_total = round(realized_total, 2)
    open_total = round(open_total, 2)
    return {
        "by_instrument": by_instrument,
        "total": round(realized_total + open_total, 2),
        "realized": realized_total,
        "open": open_total,
    }


# Safely convert a value to int, returning 0 on failure.
def _coerce_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# Safely convert a value to float, returning None on failure.
def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# Parse Angel broker postback payload into an OrderStatus if fields are valid.
def _parse_angel_postback(payload: dict[str, Any]) -> OrderStatus | None:
    order_id = str(
        payload.get("orderid")
        or payload.get("order_id")
        or payload.get("uniqueorderid")
        or ""
    ).strip()
    symbol = str(payload.get("tradingsymbol") or payload.get("symbol") or "").strip()
    if not order_id or not symbol:
        return None
    status_raw = str(payload.get("orderstatus") or payload.get("status") or "").strip()
    order_type = (
        str(payload.get("ordertype") or payload.get("order_type") or "")
        .strip()
        .upper()
    )
    product_type = (
        str(payload.get("producttype") or payload.get("product_type") or "")
        .strip()
        .upper()
    )
    side = (
        str(payload.get("transactiontype") or payload.get("side") or "")
        .strip()
        .upper()
    )
    qty = _coerce_int(payload.get("quantity") or payload.get("qty"))
    filled = _coerce_int(
        payload.get("filledshares")
        or payload.get("filled_qty")
        or payload.get("filled_quantity")
    )
    avg_price = _coerce_float(
        payload.get("averageprice")
        or payload.get("average_price")
        or payload.get("avgprice")
    )
    price = _coerce_float(payload.get("price"))
    exchange = str(payload.get("exchange") or "").strip() or None
    updated_at = str(
        payload.get("updatetime")
        or payload.get("exchorderupdatetime")
        or payload.get("exchtime")
        or ""
    ).strip() or None
    return OrderStatus(
        order_id=order_id,
        symbol=symbol,
        side=side,
        status=status_raw or "",
        order_type=order_type,
        product_type=product_type,
        quantity=qty,
        filled_quantity=filled,
        average_price=avg_price,
        price=price,
        exchange=exchange,
        updated_at=updated_at,
    )


def _parse_iso_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _latest_iso(values: list[Any]) -> str | None:
    latest: datetime | None = None
    for raw in values:
        ts = _parse_iso_timestamp(raw)
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    if latest is None:
        return None
    return latest.isoformat().replace("+00:00", "Z")


def _sync_freshness_check(
    *,
    last_ok_ts_iso: str | None,
    interval_seconds: float,
    label: str,
) -> dict[str, Any]:
    """Return a staleness report dict. `stale=True` when sync has never run or is overdue."""
    multiplier = 2.0
    threshold = interval_seconds * multiplier
    if last_ok_ts_iso is None:
        return {
            f"{label}_last_ok_ts": None,
            f"{label}_age_seconds": None,
            f"{label}_stale": True,
            f"{label}_threshold_seconds": threshold,
        }
    last_ts = _parse_iso_timestamp(last_ok_ts_iso)
    if last_ts is None:
        return {
            f"{label}_last_ok_ts": last_ok_ts_iso,
            f"{label}_age_seconds": None,
            f"{label}_stale": True,
            f"{label}_threshold_seconds": threshold,
        }
    age = (datetime.now(timezone.utc) - last_ts).total_seconds()
    return {
        f"{label}_last_ok_ts": last_ok_ts_iso,
        f"{label}_age_seconds": round(age, 1),
        f"{label}_stale": age > threshold,
        f"{label}_threshold_seconds": threshold,
    }


def _validate_angel_postback_content_type(request: Request) -> None:
    content_type = str(request.headers.get("content-type") or "").strip().lower()
    if "application/json" in content_type:
        return
    raise HTTPException(status_code=415, detail="Expected application/json payload")


def _validate_angel_postback_payload_shape(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    required = {
        "clientcode": str(payload.get("clientcode") or payload.get("client_code") or "").strip(),
        "orderid": str(payload.get("orderid") or payload.get("order_id") or "").strip(),
        "tradingsymbol": str(payload.get("tradingsymbol") or payload.get("symbol") or "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(sorted(missing))}",
        )


async def _authorize_angel_postback(request: Request, payload: Any) -> str:
    mode = load_stability_feature_flags().angel_postback_auth_mode.upper()
    settings = get_settings()
    if mode == "LEGACY":
        await get_admin_context(
            request,
            x_admin_key=request.headers.get("X-Admin-Key"),
            x_admin_timestamp=request.headers.get("X-Admin-Timestamp"),
            x_admin_signature=request.headers.get("X-Admin-Signature"),
        )
        return "legacy_admin"

    _validate_angel_postback_content_type(request)
    _validate_angel_postback_payload_shape(payload)

    if mode == "DIRECT_BROKER":
        expected = getattr(settings, "angel_postback_token", None)
        provided = request.headers.get("X-Angel-Postback-Token")
        if not safe_compare_token(expected, provided):
            logger.warning(
                "Angel postback auth failed mode=%s reason=invalid_direct_broker_token clientcode=%s",
                mode,
                str(payload.get("clientcode") or payload.get("client_code") or "").strip() or "-",
            )
            raise HTTPException(status_code=401, detail="Invalid broker postback token")
        return "direct_broker"

    if mode == "RELAY":
        expected = getattr(settings, "angel_postback_relay_token", None)
        provided = request.headers.get("X-Relay-Token")
        if not safe_compare_token(expected, provided):
            logger.warning(
                "Angel postback auth failed mode=%s reason=invalid_relay_token clientcode=%s",
                mode,
                str(payload.get("clientcode") or payload.get("client_code") or "").strip() or "-",
            )
            raise HTTPException(status_code=401, detail="Invalid relay token")
        return "relay"

    logger.warning("Angel postback auth failed mode=%s reason=unsupported_mode", mode)
    raise HTTPException(status_code=500, detail="Unsupported postback auth mode")


def _build_docker_health_summary() -> dict[str, Any]:
    runtime = get_app_runtime()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    schema_snapshot: dict[str, Any] = {
        "status": "unknown",
        "checked_at": None,
        "missing_tables": [],
        "missing_indexes": [],
    }
    schema_getter = getattr(runtime, "schema_status", None)
    if callable(schema_getter):
        try:
            raw = schema_getter()
            if isinstance(raw, dict):
                schema_snapshot.update(raw)
        except Exception as exc:
            schema_snapshot = {
                "status": "error",
                "checked_at": now,
                "missing_tables": [],
                "missing_indexes": [],
                "error": str(exc),
            }

    watchdog_snapshot: dict[str, Any] = {}
    watchdog_getter = getattr(runtime, "watchdog_status", None)
    if callable(watchdog_getter):
        try:
            raw = watchdog_getter()
            if isinstance(raw, dict):
                watchdog_snapshot = dict(raw)
        except Exception:
            watchdog_snapshot = {}

    leader_lease_snapshot: dict[str, Any] = {}
    leader_lease_getter = getattr(runtime, "leader_lease_status", None)
    if callable(leader_lease_getter):
        try:
            raw = leader_lease_getter()
            if isinstance(raw, dict):
                leader_lease_snapshot = dict(raw)
        except Exception:
            leader_lease_snapshot = {}

    tracked_accounts: list[str] = []
    last_ok_values: list[Any] = []
    last_blocked_values: list[Any] = []
    try:
        hub_runtime = get_hub_runtime()
        hub = getattr(hub_runtime, "hub", None)
        state_store = getattr(hub_runtime, "state_store", None)
        if hub is not None and state_store is not None:
            ids = getattr(hub, "list_runner_ids", None)
            if callable(ids):
                tracked_accounts = [str(v) for v in ids()]
            for broker_account_id in tracked_accounts:
                status = state_store.get_positions_status(broker_account_id)
                if not isinstance(status, dict):
                    continue
                last_ok_values.append(status.get("last_ok_ts"))
                reason = str(status.get("error_reason") or "").strip().lower()
                blocked_ts = status.get("blocked_ts")
                if blocked_ts and reason in {"blocked", "rate_limited"}:
                    last_blocked_values.append(blocked_ts)
    except Exception as exc:
        logger.warning("Health summary failed to gather hub state: %s", exc)

    last_position_sync_ok_ts = _latest_iso(last_ok_values)
    last_broker_blocked_ts = _latest_iso(last_blocked_values)
    stream_worker_running = bool(runtime.stream_worker_running())
    watchdog_running = bool(runtime.watchdog_running())

    # Resolve operating mode so health checks are mode-aware (Architecture S1)
    op_mode_snapshot: dict[str, Any] = {}
    op_mode_getter = getattr(runtime, "operating_mode_status", None)
    if callable(op_mode_getter):
        try:
            op_mode_snapshot = dict(op_mode_getter())
        except Exception:
            op_mode_snapshot = {}
    try:
        boot_runtime_cfg = get_boot_config().runtime
    except Exception:
        boot_runtime_cfg = _runtime_cfg
    lease_owned = bool(leader_lease_snapshot.get("owned", True))
    stream_worker_expected = lease_owned and not bool(
        getattr(boot_runtime_cfg, "disable_stream_worker", False)
    )

    degraded_reasons: list[str] = []
    if not stream_worker_running and stream_worker_expected:
        degraded_reasons.append("stream_worker_stopped")
    schema_status = str(schema_snapshot.get("status") or "unknown").strip().lower()
    if schema_status == "error":
        degraded_reasons.append("schema_error")
    if tracked_accounts and not last_position_sync_ok_ts:
        degraded_reasons.append("position_sync_not_ready")

    status = "ok" if not degraded_reasons else "degraded"

    # OPS-6.6: Per-account staleness + policy state
    per_account_staleness: list[dict[str, Any]] = []
    try:
        hub_runtime = get_hub_runtime()
        state_store = getattr(hub_runtime, "state_store", None)
        for acct_id in tracked_accounts:
            acct_info: dict[str, Any] = {"broker_account_id": acct_id}
            if state_store is not None:
                pos_status = state_store.get_positions_status(acct_id)
                if isinstance(pos_status, dict):
                    acct_info["last_sync_ts"] = pos_status.get("last_ok_ts")
                    acct_info["error_reason"] = pos_status.get("error_reason")
                    acct_info["stale"] = pos_status.get("stale", False)
            per_account_staleness.append(acct_info)
    except Exception:
        pass

    # Policy / alert summary
    alert_summary: dict[str, Any] = {}
    try:
        from app.observability.alert_rules import get_alert_evaluator
        evaluator = get_alert_evaluator()
        firing = evaluator.get_firing_alerts()
        alert_summary = {
            "firing_count": len(firing),
            "firing_rules": [a.rule_name for a in firing],
        }
    except Exception:
        alert_summary = {"firing_count": 0, "firing_rules": []}
    runtime_alert_status: dict[str, Any] = {}
    alert_status_getter = getattr(runtime, "alert_evaluator_status", None)
    if callable(alert_status_getter):
        try:
            runtime_alert_status = dict(alert_status_getter())
        except Exception:
            runtime_alert_status = {}

    mitigation_summary: dict[str, Any] = {}
    try:
        from app.observability.auto_mitigation import get_fault_tracker
        tracker = get_fault_tracker()
        mitigation_summary = {
            "enabled": tracker.enabled,
            "total_events": len(tracker._events),
        }
    except Exception:
        mitigation_summary = {"enabled": False, "total_events": 0}

    return {
        "status": status,
        "service": "phoenix",
        "timestamp": now,
        "schema_status": schema_status,
        "operating_mode": op_mode_snapshot.get("mode"),
        "stream_worker_running": stream_worker_running,
        "stream_worker_expected": stream_worker_expected,
        "watchdog_running": watchdog_running,
        "last_position_sync_ok_ts": last_position_sync_ok_ts,
        "last_broker_blocked_or_rate_limited_ts": last_broker_blocked_ts,
        "tracked_account_count": len(tracked_accounts),
        "degraded_reasons": degraded_reasons,
        "schema": schema_snapshot,
        "watchdog": watchdog_snapshot,
        "leader_lease": leader_lease_snapshot,
        "alert_evaluator": runtime_alert_status,
        "per_account_staleness": per_account_staleness,
        "alerts": alert_summary,
        "auto_mitigation": mitigation_summary,
    }


# Return lightweight health status for container probes.
@app.get("/health")
async def health() -> dict:
    """
    Lightweight health endpoint for container probes.

    Must stay fast and in-memory: no broker calls, Firestore, or slow checks.
    This is a liveness probe; readiness is reported separately via /ready.
    """
    runtime = get_app_runtime()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    ready = bool(getattr(runtime, "ready", False))
    return {
        "status": "ok",
        "ready": ready,
        "service": "phoenix",
        "timestamp": now,
        "runtime_path": "app_runtime_lifespan",
        "order_path": "strategy_bridge_order_router",
        "boot_config_created_at": _boot_config_created_at_for_health(),
        "stream_worker_running": runtime.stream_worker_running(),
        "watchdog_running": runtime.watchdog_running(),
        "leader_lease_status": (
            runtime.leader_lease_status()
            if callable(getattr(runtime, "leader_lease_status", None))
            else {}
        ),
    }


@app.get("/health/summary")
async def health_summary() -> dict:
    """Unified Docker/K8s health summary for runtime and broker-sync signals."""
    return _build_docker_health_summary()


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """
    Machine-readable readiness probe for container orchestrators.

    Returns HTTP 200 only when the runtime readiness latch is set AND at least
    all expected hub AccountRunners are actively running (if multi-hub is enabled).
    Returns HTTP 503 otherwise.

    Use this as the Docker/compose healthcheck target instead of /health.
    /health remains a liveness probe that always returns HTTP 200.
    """
    runtime = get_app_runtime()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict = {"timestamp": now}

    if _readiness_trade_mode() == "LIVE":
        leader_lease_snapshot = _leader_lease_readiness_snapshot(runtime)
        payload["leader_lease_status"] = leader_lease_snapshot
        if leader_lease_snapshot["enabled"] and not leader_lease_snapshot["owned"]:
            payload["ready"] = False
            payload["reason"] = "leader_lease_not_owned"
            return JSONResponse(status_code=503, content=payload)
        if (
            leader_lease_snapshot["enabled"]
            and leader_lease_snapshot["owned"]
            and not leader_lease_snapshot["task_running"]
        ):
            payload["ready"] = False
            payload["reason"] = "leader_lease_task_not_running"
            return JSONResponse(status_code=503, content=payload)

    if not runtime.ready:
        payload["ready"] = False
        payload["reason"] = "startup_recovery_in_progress"
        return JSONResponse(status_code=503, content=payload)

    startup_recovery_getter = getattr(runtime, "startup_recovery_status", None)
    if callable(startup_recovery_getter):
        startup_recovery = startup_recovery_getter()
        if isinstance(startup_recovery, dict):
            recovery_status = str(startup_recovery.get("status") or "").strip().lower()
            recovery_reason = str(startup_recovery.get("reason") or "").strip() or None
            recovery_summary = startup_recovery.get("summary")
            if recovery_status:
                payload["startup_recovery_status"] = recovery_status
            if recovery_reason:
                payload["startup_recovery_reason"] = recovery_reason
            if isinstance(recovery_summary, dict) and recovery_summary:
                payload["startup_recovery_summary"] = dict(recovery_summary)
            if recovery_status == "degraded":
                payload["ready"] = False
                payload["reason"] = recovery_reason or "startup_recovery_degraded"
                return JSONResponse(status_code=503, content=payload)

    settings = get_settings()
    if settings.enable_multi_hub:
        try:
            hub = get_hub_runtime().hub
            running = hub.running_runner_count
            registered = int(
                getattr(
                    hub,
                    "registered_runner_count",
                    getattr(hub, "runner_count", 0),
                )
                or 0
            )
            failed = int(getattr(hub, "failed_runner_count", 0) or 0)
            payload["runner_count"] = registered
            payload["registered_runner_count"] = registered
            payload["running_runner_count"] = running
            payload["failed_runner_count"] = failed
            if _readiness_trade_mode() == "LIVE" and registered == 0:
                payload["ready"] = False
                payload["reason"] = "no_runners_registered"
                return JSONResponse(status_code=503, content=payload)
            if registered > 0 and running == 0:
                payload["ready"] = False
                payload["reason"] = "no_runners_running"
                return JSONResponse(status_code=503, content=payload)
            if registered > running:
                payload["ready"] = False
                payload["reason"] = "runner_startup_incomplete"
                return JSONResponse(status_code=503, content=payload)
            if failed > 0:
                payload["ready"] = False
                payload["reason"] = "runner_failures_present"
                return JSONResponse(status_code=503, content=payload)
        except Exception as exc:
            payload["ready"] = False
            payload["reason"] = f"hub_unavailable:{exc}"
            return JSONResponse(status_code=503, content=payload)

    if _readiness_trade_mode() == "LIVE":
        stream_status = _stream_worker_readiness_snapshot(runtime)
        payload["stream_worker_expected"] = bool(stream_status["expected"])
        payload["stream_worker_running"] = bool(stream_status["running"])
        payload["watchdog_running"] = bool(stream_status["watchdog_running"])
        if stream_status["fatal_error"]:
            payload["stream_worker_failure_state"] = "fatal_error"
        if payload["stream_worker_expected"]:
            if stream_status["fatal_error"]:
                payload["ready"] = False
                payload["reason"] = "stream_worker_fatal_error"
                return JSONResponse(status_code=503, content=payload)
            if not payload["stream_worker_running"]:
                payload["ready"] = False
                payload["reason"] = "stream_worker_not_running"
                return JSONResponse(status_code=503, content=payload)
            if not payload["watchdog_running"]:
                payload["ready"] = False
                payload["reason"] = "stream_watchdog_not_running"
                return JSONResponse(status_code=503, content=payload)

    # Sync freshness check — gates readiness on position and orders sync currency
    try:
        hub_runtime = get_hub_runtime()
        hub = getattr(hub_runtime, "hub", None)
        state_store = getattr(hub_runtime, "state_store", None)
        runner_ids: list[str] = []
        if hub is not None and callable(getattr(hub, "list_runner_ids", None)):
            runner_ids = [str(v) for v in hub.list_runner_ids()]
        if runner_ids and state_store is not None:
            pos_interval = float(os.getenv("POSITION_SYNC_INTERVAL_SECONDS", "30"))
            ord_interval = float(os.getenv("ORDERS_SYNC_INTERVAL_SECONDS", "90"))
            pos_ts_values: list[str | None] = []
            ord_ts_values: list[str | None] = []
            for acct_id in runner_ids:
                pos_st = state_store.get_positions_status(acct_id)
                pos_ts_values.append(pos_st.get("last_ok_ts") if isinstance(pos_st, dict) else None)
                ord_st = state_store.get_orders_status(acct_id)
                ord_ts_values.append(ord_st.get("orders_last_ok_ts") if isinstance(ord_st, dict) else None)
            oldest_pos_ts = min(
                (t for t in pos_ts_values if t is not None),
                default=None,
            )
            oldest_ord_ts = min(
                (t for t in ord_ts_values if t is not None),
                default=None,
            )
            pos_report = _sync_freshness_check(
                last_ok_ts_iso=oldest_pos_ts,
                interval_seconds=pos_interval,
                label="position_sync",
            )
            ord_report = _sync_freshness_check(
                last_ok_ts_iso=oldest_ord_ts,
                interval_seconds=ord_interval,
                label="orders_sync",
            )
            # Include only stale/ready status in public readyz — not raw timestamps.
            payload["position_sync_stale"] = pos_report["position_sync_stale"]
            payload["orders_sync_stale"] = ord_report["orders_sync_stale"]
            if pos_report["position_sync_stale"]:
                payload["ready"] = False
                payload["reason"] = "position_sync_stale"
                return JSONResponse(status_code=503, content=payload)
            if ord_report["orders_sync_stale"]:
                payload["ready"] = False
                payload["reason"] = "orders_sync_stale"
                return JSONResponse(status_code=503, content=payload)
    except (ImportError, AttributeError, RuntimeError) as exc:
        # Hub not configured or not available — sync freshness check is not applicable.
        logger.debug("readyz: sync freshness check skipped (hub unavailable): %s", exc)
    except Exception as exc:
        logger.error("readyz: sync freshness check error", exc_info=True)
        payload["ready"] = False
        payload["reason"] = "sync_freshness_check_error"
        payload["sync_freshness_error"] = str(exc)
        return JSONResponse(status_code=503, content=payload)

    if _readiness_trade_mode() == "LIVE":
        # §58/§93 — Surface kill-switch state in readyz; block readiness when
        # the durable kill-switch state is active or cannot be verified.
        try:
            from app.risk.kill_switch import get_kill_switch_state
            ks_state = get_kill_switch_state()
            _ks_active = ks_state.get("active_count", 0)
            payload["kill_switch_active_count"] = _ks_active
            payload["kill_switch_source"] = ks_state.get("source", "unavailable")
            if _ks_active > 0:
                payload["ready"] = False
                payload["reason"] = f"kill_switch_active: {_ks_active} non-INACTIVE kill switch(es)"
                return JSONResponse(status_code=503, content=payload)
        except Exception as _ks_exc:
            # §93: Cannot verify durable kill-switch state — fail closed.
            payload["kill_switch_active_count"] = -1
            payload["kill_switch_source"] = "unavailable"
            payload["ready"] = False
            payload["reason"] = f"kill_switch_unavailable: {_ks_exc}"
            return JSONResponse(status_code=503, content=payload)

    if _readiness_trade_mode() == "LIVE":
        # §57 / #108 — Gate readiness on restored authoritative position state.
        # Block when ANY of the following is true:
        #   (a) position_records_loaded > 0 but _position_authority_restored is False
        #       (load failed or returned 0 after records existed in DB)
        #   (b) _startup_ownership_gap_detected is True — positions loaded but no
        #       corresponding ownership records exist (#107)
        # The previous condition "unresolved_active > 0 and not pos_authority" was
        # too narrow: it silently passed when the outbox was clean (0 unresolved)
        # even if non-terminal position records had no ownership coverage.
        try:
            pos_authority = getattr(runtime, "_position_authority_restored", None)
            ownership_gap = bool(getattr(runtime, "_startup_ownership_gap_detected", False))
            recovery_summary: dict = {}
            if callable(getattr(runtime, "startup_recovery_status", None)):
                rs = runtime.startup_recovery_status()
                recovery_summary = rs.get("summary") or {}
            position_records_loaded = int((recovery_summary or {}).get("position_records_loaded", 0))
            payload["position_records_loaded"] = position_records_loaded
            # When no position records were loaded there is nothing to restore;
            # report authority as satisfied rather than misleadingly false.
            effective_authority = True if position_records_loaded == 0 else bool(pos_authority)
            payload["position_authority_restored"] = effective_authority
            payload["startup_ownership_gap_detected"] = ownership_gap

            if position_records_loaded > 0 and not pos_authority:
                payload["ready"] = False
                payload["reason"] = "position_authority_not_restored"
                payload["position_authority_detail"] = (
                    f"{position_records_loaded} non-terminal position record(s) were loaded "
                    f"from Postgres but _position_authority_restored is False. "
                    f"Check startup.position_records_loaded log and migration 009."
                )
                return JSONResponse(status_code=503, content=payload)

            if ownership_gap:
                payload["ready"] = False
                payload["reason"] = "startup_ownership_gap_detected"
                payload["position_authority_detail"] = (
                    f"{position_records_loaded} non-terminal position record(s) exist in Postgres "
                    f"but 0 ownership records were marked RECOVERY_PENDING. "
                    f"These positions are untracked by the ownership layer. "
                    f"Investigate internal_position_records and resolve before accepting orders."
                )
                return JSONResponse(status_code=503, content=payload)

        except Exception as _pos_exc:
            logger.warning("readyz: position authority check failed: %s", _pos_exc)

    if _readiness_trade_mode() == "LIVE":
        try:
            from app.runners.multi_instrument_stream import get_active_instrument_count
            active_count = get_active_instrument_count()
            payload["active_instrument_count"] = active_count
            min_instruments = int(os.getenv("MIN_TRADEABLE_INSTRUMENTS", "1"))
            if active_count < min_instruments:
                payload["ready"] = False
                payload["reason"] = "instrument_universe_degraded"
                return JSONResponse(status_code=503, content=payload)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("readyz: instrument count check failed: %s", exc)

        try:
            from app.brokers.angel_client import get_balance_schema_violation_count
            schema_violations = get_balance_schema_violation_count()
            payload["balance_schema_violation_count"] = schema_violations
            max_allowed = int(os.getenv("READYZ_MAX_BALANCE_SCHEMA_VIOLATIONS", "3"))
            if schema_violations > max_allowed:
                payload["ready"] = False
                payload["reason"] = "balance_schema_violations_exceeded"
                return JSONResponse(status_code=503, content=payload)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("readyz: balance schema check failed: %s", exc)

        # §106: Gate readiness on at least one successful balance fetch per runner.
        # Capital engine starts with empty state when RMS is down; fail-closed on
        # missing state means all entries are blocked. Surface this explicitly rather
        # than reporting false-green readiness.
        try:
            hub = getattr(runtime, "hub", None)
            if hub is not None:
                _all_runners = list(getattr(hub, "_account_runners", {}).values())
                # §125 / Issue #4: default must be False (fail-closed) so any runner
                # that does not expose has_ever_synced_balance blocks readiness.
                # The previous default=True silently passed the gate when the attribute
                # was absent, producing false-green readyz during AB1004 RMS outages.
                runners_never_synced = [
                    r.broker_account_id
                    for r in _all_runners
                    if not getattr(r, "has_ever_synced_balance", False)
                ]
                payload["balance_sync_ready"] = len(runners_never_synced) == 0
                # Account IDs of unsynced runners are kept in logs/admin endpoint only.
                if runners_never_synced:
                    payload["ready"] = False
                    payload["reason"] = "balance_sync_not_ready"
                    payload["balance_sync_pending_count"] = len(runners_never_synced)
                    return JSONResponse(status_code=503, content=payload)
        except Exception as exc:
            logger.warning("readyz: balance sync ready check failed: %s", exc)

    # §85/§92: Surface degraded scope count and block readiness in LIVE when
    # active DEGRADED/RECONCILING/ORPHAN scopes require operator attention.
    _degraded_count = 0
    try:
        from app.core.degraded_scope_manager import degraded_scope_manager
        _degraded_count = len(degraded_scope_manager.active_scopes())
        payload["degraded_scope_count"] = _degraded_count
    except Exception:
        payload["degraded_scope_count"] = -1

    # §92: Also surface DEGRADED and RECONCILING position counts from
    # OrderLifecycleService in-memory records.
    _reconciling_count = 0
    _degraded_positions = 0
    try:
        _hub_rt = get_hub_runtime()
        _olc = getattr(_hub_rt, "order_lifecycle", None)
        if _olc is not None and hasattr(_olc, "count_positions_by_state"):
            _pos_by_state = _olc.count_positions_by_state()
            _degraded_positions = _pos_by_state.get("DEGRADED", 0)
            _reconciling_count = _pos_by_state.get("RECONCILING", 0)
            payload["position_state_counts"] = _pos_by_state
    except Exception:
        pass

    # §92: In LIVE mode block readiness when any position is in a state that
    # requires operator attention.  A DEGRADED or RECONCILING position means
    # evidence is ambiguous — new orders on the same scope could be unsafe.
    if _readiness_trade_mode() == "LIVE":
        _blocking_states = _degraded_count + _degraded_positions + _reconciling_count
        if _blocking_states > 0:
            payload["ready"] = False
            payload["reason"] = (
                f"position_authority_degraded: degraded_scopes={_degraded_count} "
                f"degraded_positions={_degraded_positions} "
                f"reconciling_positions={_reconciling_count}"
            )
            return JSONResponse(status_code=503, content=payload)

    # Postgres transport security state is surfaced in the authenticated release-evidence
    # endpoint (/admin/release-evidence) only — not in the public /readyz response.

    payload["ready"] = True
    return JSONResponse(status_code=200, content=payload)


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus-compatible metrics endpoint (OBS-6.1)."""
    from app.observability.prometheus_metrics import render_metrics, sync_from_labeled_registry

    try:
        sync_from_labeled_registry()
    except Exception:
        pass  # Best-effort bridge; core metrics still render
    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/health/alerts")
async def health_alerts() -> dict:
    """Evaluate alert rules and return current alert statuses (OBS-6.3)."""
    from app.observability.alert_rules import get_alert_evaluator

    evaluator = get_alert_evaluator()
    return evaluator.summary()


@app.get("/health/mitigations")
async def health_mitigations() -> dict:
    """Return auto-mitigation tracker state (OPS-6.5)."""
    from app.observability.auto_mitigation import get_fault_tracker

    tracker = get_fault_tracker()
    return tracker.summary()


# Return readiness status and optional deep checks.
@app.get("/ready")
async def ready(deep: bool = Query(False)) -> dict:
    """
    Readiness endpoint used by Cloud Run / external monitors.

    Includes:
    - whether multi-hub is enabled,
    - number of AccountRunners active,
    - number of tenants defined,
    - timestamp of last subscription reconcile.
    """
    app_runtime = get_app_runtime()
    if not app_runtime.ready:
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "startup_recovery_in_progress"},
        )

    settings = get_settings()

    if not settings.enable_multi_hub:
        return {
            "status": "ready",
            "multi_hub_enabled": False,
        }

    runtime = get_hub_runtime()
    hub = runtime.hub

    response = {
        "status": "ready",
        "multi_hub_enabled": True,
        "runner_count": hub.runner_count,
        "last_subscription_refresh": (
            hub.last_subscription_refresh.isoformat()
            if hub.last_subscription_refresh
            else None
        ),
    }

    if deep:
        from app.tenants.firestore_client import get_all_tenants

        tenants = get_all_tenants()
        response["tenant_count"] = len(tenants)

    return response


# Return a simple response for scheduled start checks.
@app.get("/start")
async def start() -> dict:
    """
    Cloud Scheduler trigger endpoint.
    
    Called by Cloud Scheduler to verify the service is running.
    Can be extended to perform periodic tasks if needed.
    """
    settings = get_settings()
    
    if not settings.enable_multi_hub:
        return {
            "status": "started",
            "multi_hub_enabled": False,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    runtime = get_hub_runtime()
    hub = runtime.hub
    
    return {
        "status": "started",
        "multi_hub_enabled": True,
        "runner_count": hub.runner_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Return the current strategy enablement snapshot.
@app.get("/admin/strategies")
async def list_strategies(
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    return {"strategies": strategy_switchboard.snapshot()}


@app.get("/admin/strategy-selection")
async def list_strategy_selection(
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    return {"strategy_selection": dashboard_bus.strategy_selection_snapshot()}


@app.get("/admin/strategies/aliases")
async def list_strategy_alias_usage(
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    return {"strategy_alias_usage": strategy_alias_usage_snapshot()}


# Enable or disable a strategy and return its new state.
@app.post("/admin/strategies/toggle")
async def toggle_strategy(
    request: Request,
    req: ToggleRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)
    name = str(req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing strategy name")
    canonical_name = canonicalize_strategy_name(
        name,
        source="/admin/strategies/toggle",
        preserve_unknown=True,
    )
    previous = strategy_switchboard.is_enabled(canonical_name)
    strategy_switchboard.set_enabled(canonical_name, req.enabled)
    emit_audit_event(
        actor=ctx.caller,
        action="toggle_strategy",
        resource_type="strategy",
        resource_id=canonical_name,
        before={"enabled": previous},
        after={"enabled": req.enabled},
    )
    log_event(
        logger,
        event_type="ADMIN_STRATEGY_TOGGLE",
        message="Admin strategy toggle applied.",
        strategy_id=canonical_name,
        request_id=getattr(request.state, "request_id", None),
        actor=ctx.caller,
        previous_enabled=previous,
        enabled=req.enabled,
    )
    return {
        "name": canonical_name,
        "enabled": strategy_switchboard.is_enabled(canonical_name),
    }


# Return the current instrument enablement snapshot.
@app.get("/admin/instruments")
async def list_instruments(
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    return {"instruments": instrument_controller.snapshot()}


# Update instrument settings like enablement and allowed strategies.
@app.post("/admin/instruments/update")
async def update_instrument(
    request: Request,
    req: InstrumentUpdateRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict:
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)
    if not req.underlying:
        raise HTTPException(status_code=400, detail="Missing underlying")
    key = req.underlying
    before_state = instrument_controller.snapshot().get(key.upper(), {})
    if req.enabled is not None:
        instrument_controller.set_enabled(key, req.enabled)
    if req.allowed_strategies is not None:
        allowed = canonicalize_strategy_names(
            [str(name) for name in req.allowed_strategies],
            source="/admin/instruments/update.allowed_strategies",
            preserve_unknown=True,
        )
        instrument_controller.update_allowed_strategies(key, allowed)
    after_state = instrument_controller.snapshot().get(key.upper(), {})
    emit_audit_event(
        actor=ctx.caller,
        action="update_instrument",
        resource_type="instrument",
        resource_id=key,
        before=before_state,
        after=after_state,
    )
    log_event(
        logger,
        event_type="ADMIN_INSTRUMENT_UPDATE",
        message="Admin instrument control updated.",
        instrument=key.upper(),
        request_id=getattr(request.state, "request_id", None),
        actor=ctx.caller,
        enabled=after_state.get("enabled"),
        allowed_strategies=",".join(after_state.get("allowed_strategies", [])),
    )
    return {
        "underlying": key,
        "state": after_state,
    }


# Report ML configuration and model status.
@app.get("/ml/health")
async def ml_health() -> dict:
    ml_cfg = getattr(get_boot_config(), "ml_config", None)
    enabled = bool(getattr(ml_cfg, "enabled", False))
    mode = str(getattr(ml_cfg, "mode", "OFF"))
    models_dir = str(getattr(ml_cfg, "models_dir", "")) if ml_cfg is not None else ""
    return {
        "ml_enabled": enabled,
        "global_mode": mode,
        "models_dir": models_dir,
        # ML model loading is intentionally disabled in runtime health checks.
        "underlyings": [],
    }


# Refresh cached daily levels for strategies and dashboards.
@app.post("/admin/daily-levels/refresh", response_model=DailyLevelsRefreshResponse)
async def refresh_daily_levels(
    request: Request,
    ctx: AdminContext = Depends(get_admin_context),
) -> DailyLevelsRefreshResponse:
    ctx.require_role(AdminRole.OPERATOR)
    check_rate_limit(request)
    ok = refresh_daily_levels_cache()
    emit_audit_event(
        actor=ctx.caller,
        action="refresh_daily_levels",
        resource_type="daily_levels",
        resource_id="cache",
        after={"status": "ok" if ok else "skipped"},
    )
    return DailyLevelsRefreshResponse(status="ok" if ok else "skipped")


class VolatilityUpdateRequest(BaseModel):
    vix: float


# Feed India VIX level into the circuit breaker for volatility-based protection.
@app.post("/admin/volatility/update")
async def update_volatility(
    request: Request,
    payload: VolatilityUpdateRequest,
    ctx: AdminContext = Depends(get_admin_context),
) -> dict[str, Any]:
    ctx.require_role(AdminRole.OPERATOR)
    runtime = get_hub_runtime()
    runtime.update_volatility(payload.vix)
    cb_status = runtime.circuit_breaker.get_status()
    return {
        "status": "ok",
        "vix": payload.vix,
        "circuit_breaker_state": cb_status.state.value,
        "entries_blocked": cb_status.entries_blocked,
        "reason": cb_status.reason,
    }


# Handle Angel broker order postbacks and update the order store.
@app.post("/webhook/angel/postback")
async def angel_postback(
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    auth_caller = await _authorize_angel_postback(request, payload)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")
    client_code = str(payload.get("clientcode") or payload.get("client_code") or "").strip()
    if not client_code:
        raise HTTPException(status_code=400, detail="Missing clientcode")

    account = get_broker_account_by_client_code(client_code)
    broker_account_id = None
    tenant_id = None
    if account is not None:
        broker_account_id = account.broker_account_id
        tenant_id = account.tenant_id
    else:
        settings = get_settings()
        if settings.hub_default_broker_account_id:
            broker_account_id = settings.hub_default_broker_account_id
            tenant_id = settings.hub_default_tenant_id
        else:
            logger.warning(
                "Angel postback ignored: unknown clientcode=%s order_id=%s",
                client_code,
                str(payload.get("orderid") or payload.get("order_id") or "").strip()
                or "-",
            )
            return {"status": "ignored", "reason": "unknown_clientcode"}

    order = _parse_angel_postback(payload)
    if order is None:
        logger.warning(
            "Angel postback ignored: missing order fields clientcode=%s order_id=%s symbol=%s",
            client_code,
            str(payload.get("orderid") or payload.get("order_id") or "").strip()
            or "-",
            str(payload.get("tradingsymbol") or payload.get("symbol") or "").strip()
            or "-",
        )
        return {"status": "ignored", "reason": "invalid_order"}

    runtime = get_hub_runtime()
    runtime.state_store.upsert_order(broker_account_id, order)
    if hasattr(runtime.state_store, "upsert_order_snapshot"):
        runtime.state_store.upsert_order_snapshot(broker_account_id, order)
    lifecycle = getattr(runtime, "order_lifecycle", None)
    if lifecycle is not None and hasattr(lifecycle, "process_observed_order_status"):
        lifecycle.process_observed_order_status(
            broker_account_id,
            order,
            transition_source="webhook",
        )
    return {
        "status": "ok",
        "auth_mode": auth_caller,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "broker_account_id": str(broker_account_id),
        "order_id": order.order_id,
    }


# Serve the static dashboard HTML page.
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    if DASHBOARD_HTML_PATH.exists():
        html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
        html = html.replace(
            "__DASHBOARD_FIX_RECENT_TRADES__",
            "true" if load_stability_feature_flags().dashboard_fix_recent_trades else "false",
        )
        return HTMLResponse(
            html, media_type="text/html"
        )
    return HTMLResponse(
        "<h3>Dashboard template missing</h3>", status_code=500, media_type="text/html"
    )


@app.post("/admin/dashboard/ws-ticket", response_model=DashboardWebsocketTicketResponse)
async def create_dashboard_ws_ticket(
    mode: str | None = Query(default=None),
    ctx: AdminContext = Depends(get_admin_context),
) -> DashboardWebsocketTicketResponse:
    resolved_mode = _dashboard_ws_mode_token(mode)
    ticket = issue_dashboard_ws_ticket(
        admin_ctx=ctx,
        path="/ws/dashboard",
        mode=resolved_mode,
        settings=get_settings(),
    )
    return DashboardWebsocketTicketResponse(
        ticket=ticket.token,
        expires_at=datetime.fromtimestamp(
            ticket.expires_at,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        ttl_seconds=max(0, ticket.expires_at - ticket.issued_at),
        mode=ticket.mode,
        path=ticket.path,
    )


# Stream live dashboard data to authorized websocket clients.
@app.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket) -> None:
    settings = get_settings()
    try:
        runtime_cfg = get_boot_config().runtime
    except Exception:
        runtime_cfg = _runtime_cfg
    auth_required = _dashboard_auth_required(runtime_cfg)
    requested_mode = websocket.query_params.get("mode")
    resolved_mode = _dashboard_ws_mode_token(requested_mode)
    use_delta = resolved_mode == "delta"
    ticket = websocket.query_params.get("ticket")
    if auth_required or ticket:
        try:
            verify_dashboard_ws_ticket(
                ticket,
                path=_dashboard_ws_path(websocket),
                mode=resolved_mode,
                settings=settings,
            )
        except ValueError:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    _register_dashboard_socket(websocket)
    sender_task = asyncio.create_task(
        _dashboard_ws_sender_loop(
            websocket,
            settings=settings,
            use_delta=use_delta,
        )
    )
    disconnect_task = asyncio.create_task(_dashboard_ws_disconnect_watcher(websocket))
    try:
        done, pending = await asyncio.wait(
            {sender_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except WebSocketDisconnect:
                logger.debug("Dashboard websocket disconnected")
        for task in done:
            try:
                await task
            except WebSocketDisconnect:
                logger.debug("Dashboard websocket disconnected")
            except asyncio.CancelledError:
                pass
    except Exception as exc:  # pragma: no cover - safety net
        logger.error("Dashboard websocket error: %s", exc)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        sender_task.cancel()
        disconnect_task.cancel()
        for task in (sender_task, disconnect_task):
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
        _unregister_dashboard_socket(websocket)
