"""Strategy bridge to route strategy orders via multi-tenant OrderRouter."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.identifiers import TenantId, BrokerAccountId, StrategyId
from app.core.feature_flags import load_stability_feature_flags
from app.core.signal_metrics import (
    get_signal_summary_metrics,
    is_submitted_order_response,
)
from app.brokers.base import OrderRequest, OrderResponse
from app.hub.routing_table import HubRoute, get_global_routing_table
from app.hub.runtime import get_hub_runtime
from app.orders.bridge_loop import (
    StrategyBridgeTimeoutError,
    get_shared_bridge_loop,
)
from app.orders.replay_context import get_replay_flag, get_replay_sink

logger = logging.getLogger(__name__)


# Route a strategy order through the hub order router.
def place_order_via_bridge(
    *,
    strategy_id: StrategyId,
    order_req: OrderRequest,
    tenant_id: Optional[TenantId] = None,
    broker_account_id: Optional[BrokerAccountId] = None,
) -> OrderResponse:
    """
    Synchronous strategy-facing bridge.

    Behaviour:
    - Route to hub OrderRouter for all strategy orders.

    Tenant/broker semantics:
    - Explicit tenant_id and broker_account_id are treated as a manual override
      and used directly (single account behaviour).
    - If either is omitted, routing table decides the target accounts.

    This keeps strategies synchronous while centralizing multi-tenant routing.
    """
    signal_metrics = get_signal_summary_metrics()
    signal_metrics.mark_signal_fired()

    # Replay isolation: short-circuit before any hub/broker infrastructure
    if get_replay_flag():
        sink = get_replay_sink()
        if sink is None:
            raise RuntimeError(
                "place_order_via_bridge: no replay-local order sink is active "
                "(isolated replay flag is set but no sink is registered)"
            )
        response = sink.place_order(strategy_id=strategy_id, order_req=order_req)
        if is_submitted_order_response(
            status=getattr(response, "status", None),
            broker_order_id=getattr(response, "broker_order_id", None),
        ):
            signal_metrics.mark_order_submitted()
        return response

    # Readiness gate: block order submission until the runtime is fully initialized.
    # The replay path above is intentionally exempt — replay bypasses live infrastructure.
    # Lazy import to avoid the circular dependency:
    #   strategy_bridge → app_runtime → multi_instrument_stream → strategy_bridge
    from app.runtime.app_runtime import get_app_runtime  # noqa: PLC0415
    _app_rt = get_app_runtime()
    if not _app_rt.ready:
        raise RuntimeError(
            f"place_order_via_bridge: runtime not ready — order blocked during startup recovery "
            f"(strategy_id={strategy_id})"
        )

    # Multi-tenant hub path: run async OrderRouter.submit_order in a fresh loop
    runtime = get_hub_runtime()
    router = runtime.order_router

    explicit_tenant = tenant_id is not None
    explicit_broker = broker_account_id is not None

    if explicit_tenant and explicit_broker:
        routes = [
            HubRoute(
                tenant_id=tenant_id,  # type: ignore[arg-type]
                broker_account_id=broker_account_id,  # type: ignore[arg-type]
            )
        ]
    else:
        routing_table = get_global_routing_table()
        routes = routing_table.get_routes_for_strategy(strategy_id)

        if not routes:
            raise ValueError(
                f"place_order_via_bridge: no active routes for strategy_id={strategy_id} "
                "(likely disabled in control tower)"
            )

    logger.info(
        "place_order_via_bridge: routing strategy_id=%s to %d route(s): %s "
        "(order_path=strategy_bridge->order_router)",
        strategy_id,
        len(routes),
        [(str(r.tenant_id), str(r.broker_account_id)) for r in routes],
    )

    # Submit an order for a single hub route.
    async def _submit(route: HubRoute) -> tuple[str, OrderResponse]:
        return await router.submit_order(
            tenant_id=route.tenant_id,
            broker_account_id=route.broker_account_id,
            strategy_id=strategy_id,
            order_req=order_req,
        )

    last_response: Optional[OrderResponse] = None
    last_successful_response: Optional[OrderResponse] = None
    flags = load_stability_feature_flags()
    if flags.strategy_bridge_mode == "shared_loop":
        bridge_loop = get_shared_bridge_loop(
            max_inflight=flags.strategy_bridge_max_inflight
        )
        for route in routes:
            try:
                _, response = bridge_loop.submit(
                    _submit(route),
                    timeout_s=flags.strategy_bridge_timeout_s,
                )
            except StrategyBridgeTimeoutError:
                logger.warning(
                    "place_order_via_bridge shared_loop timeout strategy_id=%s tenant_id=%s broker_account_id=%s timeout_s=%.3f",
                    strategy_id,
                    route.tenant_id,
                    route.broker_account_id,
                    flags.strategy_bridge_timeout_s,
                )
                response = OrderResponse(
                    broker_order_id="",
                    status="FAILED",
                    message="strategy_bridge_timeout",
                    filled_quantity=0,
                    average_price=None,
                )
            except Exception as exc:
                logger.exception(
                    "place_order_via_bridge shared_loop submission error strategy_id=%s tenant_id=%s broker_account_id=%s err=%s",
                    strategy_id,
                    route.tenant_id,
                    route.broker_account_id,
                    exc,
                )
                response = OrderResponse(
                    broker_order_id="",
                    status="FAILED",
                    message=f"strategy_bridge_error:{exc}",
                    filled_quantity=0,
                    average_price=None,
                )
            last_response = response
            status_upper = str(getattr(response, "status", "")).upper()
            if status_upper not in {"REJECTED", "FAILED"}:
                last_successful_response = response
            if is_submitted_order_response(
                status=getattr(response, "status", None),
                broker_order_id=getattr(response, "broker_order_id", None),
            ):
                signal_metrics.mark_order_submitted()
    else:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            for route in routes:
                _, response = loop.run_until_complete(_submit(route))
                last_response = response
                status_upper = str(getattr(response, "status", "")).upper()
                if status_upper not in {"REJECTED", "FAILED"}:
                    last_successful_response = response
                if is_submitted_order_response(
                    status=getattr(response, "status", None),
                    broker_order_id=getattr(response, "broker_order_id", None),
                ):
                    signal_metrics.mark_order_submitted()
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    final_response = last_successful_response or last_response
    if final_response is None:
        raise RuntimeError("place_order_via_bridge: no response received from hub routes")

    # For now, ignore hub_order_id; strategies only care about broker response.
    return final_response


__all__ = ["place_order_via_bridge"]
