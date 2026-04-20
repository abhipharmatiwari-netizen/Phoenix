"""
In-memory routing table mapping strategies to tenant/broker targets.
Refreshed from the control-plane backend (Postgres when CONTROL_PLANE_BACKEND=postgres,
Firestore otherwise).  In production (TRADE_MODE=LIVE) the backend must be postgres.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import json
import os
import time
from threading import RLock, Thread
from typing import Dict, List, Optional

from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.strategies.validators import validate_strategy_routing
from app.tenants.firestore_client import (
    get_active_broker_accounts,
    get_strategy_configs_for_account,
)

logger = logging.getLogger(__name__)


# Optional local routing fallback for dev/local use.
def _load_routes_from_env() -> Optional[Dict[StrategyId, List["HubRoute"]]]:
    raw = os.getenv("HUB_ROUTES_JSON")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("HUB_ROUTES_JSON invalid JSON: %s", exc)
        return None

    entries: List[dict] = []
    if isinstance(payload, dict) and "routes" in payload:
        routes = payload.get("routes")
        if isinstance(routes, list):
            entries = [r for r in routes if isinstance(r, dict)]
        else:
            logger.warning("HUB_ROUTES_JSON.routes must be a list")
            return {}
    elif isinstance(payload, dict):
        # Mapping of strategy_id -> list of routes
        for strategy_id, routes in payload.items():
            if not isinstance(routes, list):
                continue
            for route in routes:
                if isinstance(route, dict):
                    entry = dict(route)
                    entry.setdefault("strategy_id", strategy_id)
                    entries.append(entry)
    elif isinstance(payload, list):
        entries = [r for r in payload if isinstance(r, dict)]
    else:
        logger.warning("HUB_ROUTES_JSON must be dict or list")
        return {}

    raw_routes_by_strategy: Dict[str, List[Dict[str, str]]] = {}
    for entry in entries:
        strategy_id = entry.get("strategy_id") or entry.get("strategy")
        tenant_id = entry.get("tenant_id") or entry.get("tenant")
        broker_account_id = entry.get("broker_account_id") or entry.get("broker")
        if not strategy_id or not tenant_id or not broker_account_id:
            continue
        raw_routes_by_strategy.setdefault(str(strategy_id), []).append(
            {
                "tenant_id": str(tenant_id),
                "broker_account_id": str(broker_account_id),
            }
        )

    try:
        validated_routes = validate_strategy_routing(raw_routes_by_strategy)
    except ValueError as exc:
        logger.warning("HUB_ROUTES_JSON validation failed: %s", exc)
        return {}

    routes_by_strategy: Dict[StrategyId, List[HubRoute]] = {}
    for strategy_id, routes in validated_routes.items():
        routes_by_strategy[StrategyId(str(strategy_id))] = [
            HubRoute(
                tenant_id=TenantId(route["tenant_id"]),
                broker_account_id=BrokerAccountId(route["broker_account_id"]),
            )
            for route in routes
        ]

    if not routes_by_strategy:
        logger.warning("HUB_ROUTES_JSON provided but no valid routes parsed")
    return routes_by_strategy


def _routing_refresh_min_seconds() -> float:
    raw = os.getenv("HUB_ROUTING_REFRESH_MIN_SECONDS", "30")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


# Simple routing target for a tenant and broker account.
@dataclass(frozen=True)
class HubRoute:
    tenant_id: TenantId
    broker_account_id: BrokerAccountId


@dataclass
class HubRoutingTable:
    """
    Central routing table: strategy_id -> list of (tenant_id, broker_account_id)
    built from the control-plane backend (Postgres in LIVE, Firestore in legacy/dev).

    It is intentionally simple and in-memory; refresh() can be called at startup
    and periodically (if needed).
    """

    _routes_by_strategy: Dict[StrategyId, List[HubRoute]] = field(
        default_factory=dict
    )
    _lock: RLock = field(default_factory=RLock, repr=False)
    _last_refresh_mono: float = field(default=0.0, repr=False)
    _refresh_in_progress: bool = field(default=False, repr=False)

    # Rebuild routes from the control-plane backend (Postgres in LIVE).
    def refresh(self) -> None:
        """
        Rebuild the mapping from the control-plane backend:
          - Start from get_active_broker_accounts()
          - For each broker_account_id, fetch strategy configs
          - For every enabled config, map config.strategy_id to that account.
        Backend is determined by CONTROL_PLANE_BACKEND (postgres|firestore).
        TRADE_MODE=LIVE requires CONTROL_PLANE_BACKEND=postgres.
        """
        from app.tenants.models import StrategyConfigModel  # local import to avoid cycles

        trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
        control_plane_backend = str(
            os.getenv("CONTROL_PLANE_BACKEND", "firestore") or "firestore"
        ).strip().lower()

        if trade_mode == "LIVE" and control_plane_backend != "postgres":
            logger.error(
                "HubRoutingTable.refresh: TRADE_MODE=LIVE requires CONTROL_PLANE_BACKEND=postgres "
                "but got CONTROL_PLANE_BACKEND=%s; routing reads from undeclared Firestore backend",
                control_plane_backend,
            )

        logger.info(
            "HubRoutingTable.refresh: rebuilding routes from control-plane backend=%s",
            control_plane_backend,
        )
        env_routes = _load_routes_from_env()
        try:
            accounts = get_active_broker_accounts()
        except Exception:
            if trade_mode == "LIVE":
                logger.error(
                    "HubRoutingTable.refresh: control-plane (backend=%s) unavailable in LIVE mode; "
                    "HUB_ROUTES_JSON fallback is forbidden — raising startup failure",
                    control_plane_backend,
                )
                raise
            logger.exception(
                "HubRoutingTable.refresh: control-plane unavailable; using HUB_ROUTES_JSON fallback"
            )
            if env_routes is None:
                env_routes = {}
            with self._lock:
                self._routes_by_strategy = env_routes
                self._last_refresh_mono = time.monotonic()
            return

        routes: Dict[StrategyId, List[HubRoute]] = {}

        for account in accounts:
            broker_account_id: BrokerAccountId = account.broker_account_id
            tenant_id: TenantId = account.tenant_id

            try:
                configs = get_strategy_configs_for_account(broker_account_id)
            except Exception:
                logger.exception(
                    "HubRoutingTable.refresh: failed to load strategy configs for broker account %s",
                    broker_account_id,
                )
                continue

            for cfg in configs:
                if not isinstance(cfg, StrategyConfigModel):
                    logger.warning(
                        "HubRoutingTable.refresh: unexpected strategy config type %s for broker_account_id=%s",
                        type(cfg),
                        broker_account_id,
                    )
                    continue

                if not getattr(cfg, "enabled", False):
                    continue

                strategy_id: StrategyId = cfg.strategy_id
                routes.setdefault(strategy_id, []).append(
                    HubRoute(tenant_id=tenant_id, broker_account_id=broker_account_id)
                )

        with self._lock:
            if not routes and env_routes is not None:
                logger.warning(
                    "HubRoutingTable.refresh: no Firestore routes; using HUB_ROUTES_JSON fallback"
                )
                self._routes_by_strategy = env_routes
            else:
                self._routes_by_strategy = routes
            self._last_refresh_mono = time.monotonic()
            resolved_count = len(self._routes_by_strategy)

        logger.info(
            "HubRoutingTable.refresh: loaded %d strategies with routes",
            resolved_count,
        )

        if trade_mode == "LIVE" and resolved_count == 0:
            raise ValueError(
                "TRADE_MODE=LIVE requires at least one valid hub route "
                f"(control-plane backend={control_plane_backend}). "
                "Ensure strategy_configs rows are enabled in the control-plane DB, "
                "or set HUB_ROUTES_JSON as a fallback for dev/shadow runs."
            )

    def _refresh_worker(self) -> None:
        try:
            self.refresh()
        except Exception:
            logger.exception("HubRoutingTable.refresh_if_stale: background refresh failed")
            with self._lock:
                # Allow a near-term retry when refresh fails.
                self._last_refresh_mono = 0.0
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _request_background_refresh(self) -> None:
        with self._lock:
            if self._refresh_in_progress:
                return
            self._refresh_in_progress = True
            # Reserve refresh slot to prevent duplicate background refreshes.
            self._last_refresh_mono = time.monotonic()
        worker = Thread(
            target=self._refresh_worker,
            name="hub-routing-refresh",
            daemon=True,
        )
        worker.start()

    # Refresh only if stale based on minimum interval.
    def refresh_if_stale(self, min_interval_seconds: float) -> None:
        interval = max(0.0, float(min_interval_seconds))
        now = time.monotonic()
        with self._lock:
            is_fresh = self._last_refresh_mono > 0 and (now - self._last_refresh_mono) < interval
            needs_initial_load = self._last_refresh_mono <= 0.0 and not self._routes_by_strategy
        if is_fresh:
            return
        if needs_initial_load:
            # Preserve first-load behavior: callers should not see an empty table
            # when no prior refresh has ever run.
            self.refresh()
            return
        # Normal TTL path: trigger background refresh and serve cached routes.
        self._request_background_refresh()

    def routed_strategy_ids(self) -> frozenset[str]:
        """Return the set of strategy IDs that have at least one routing entry."""
        self.refresh_if_stale(_routing_refresh_min_seconds())
        with self._lock:
            return frozenset(str(sid) for sid, routes in self._routes_by_strategy.items() if routes)

    # Return routes for a given strategy id.
    def get_routes_for_strategy(self, strategy_id: StrategyId) -> List[HubRoute]:
        """
        Return a copy of the routes list for the given strategy_id.
        """
        self.refresh_if_stale(_routing_refresh_min_seconds())
        with self._lock:
            routes = self._routes_by_strategy.get(strategy_id, [])
            return list(routes)


# Global singleton routing table
_global_routing_table: Optional[HubRoutingTable] = None
_global_lock = RLock()


# Return the global routing table singleton.
def get_global_routing_table() -> HubRoutingTable:
    global _global_routing_table
    with _global_lock:
        if _global_routing_table is None:
            _global_routing_table = HubRoutingTable()
        return _global_routing_table


__all__ = ["HubRoute", "HubRoutingTable", "get_global_routing_table"]
