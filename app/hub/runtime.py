"""
HubRuntime: container wiring together the multi-tenant hub runtime components.

It owns:
- Hub
- CapitalEngine
- RiskEngine
- PnLEngine
- StateStore (via hub.state_store)
- OrderRouter

This module provides get_hub_runtime() as a lazily-initialized singleton
for use by FastAPI routes and background tasks.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any, Callable, Iterable, Optional

from app.config.settings import get_settings
from app.core.clock import IClock, SystemClock
from app.core.identifiers import BrokerAccountId, TenantId
from app.hub.hub import Hub
from app.hub.routing_table import get_global_routing_table
from app.data.state_store import StateStore
from app.risk.capital_engine import CapitalEngine
from app.risk.risk_engine import RiskEngine
from app.risk.profit_engine import ProfitEngine
from app.pnl.pnl_engine import PnLEngine
from app.pnl.state_store import build_pnl_state_store
from app.pnl.profit_engine import ProfitEngine as SweepProfitEngine
from app.pnl.profit_lock import ProfitLockManager
from app.orders.order_lifecycle import OrderLifecycleService
from app.orders.order_outbox import build_order_submission_outbox
from app.orders.position_ownership import build_position_ownership_store
from app.orders.router import OrderRouter
from app.risk.circuit_breaker import CircuitBreakerConfig, BreakerTrip, BreakerType, TradingCircuitBreaker
from app.observability.auto_mitigation import (
    MitigationAction,
    MitigationEvent,
    get_fault_tracker,
)
from app.hub.exit_engines import (
    ProfitSweepEngine as HubProfitSweepEngine,
    EODExitEngine,
)
from app.hub.sweep_state import SweepStateStore, SweepStateManager, PostgresSweepStateStore
from app.hub.eod_state import EODStateStore, EODStateManager, PostgresEODStateStore
from app.data.postgres import get_sweep_state_dsn

logger = logging.getLogger(__name__)
_LOCAL_APP_ENVS = {"local", "dev", "test"}


def _is_missing_control_plane_settings_error(exc: Exception) -> bool:
    return "Missing control plane Postgres settings" in str(exc)


def _truthy_env_value(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _runtime_app_env() -> str:
    return str(os.getenv("APP_ENV", os.getenv("ENV", "local")) or "local").strip().lower() or "local"


def _runtime_trade_mode() -> str:
    return str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() or "PAPER"


def _requires_durable_sweep_state() -> bool:
    if _runtime_trade_mode() == "LIVE":
        return True
    if bool(os.getenv("K_SERVICE")):
        return True
    return _runtime_app_env() not in _LOCAL_APP_ENVS


def _allow_mock_sweep_state_for_error(exc: Exception) -> bool:
    if _requires_durable_sweep_state():
        return False
    override = os.getenv("ALLOW_MOCK_SWEEP_STATE")
    if override is not None:
        return _truthy_env_value(override)
    return _is_missing_control_plane_settings_error(exc)


def _build_durable_state_initialization_error(exc: Exception) -> RuntimeError:
    trade_mode = _runtime_trade_mode()
    app_env = _runtime_app_env()
    if trade_mode == "LIVE":
        reason = "TRADE_MODE=LIVE requires durable sweep/EOD state managers"
    elif bool(os.getenv("K_SERVICE")):
        reason = "Cloud Run requires durable sweep/EOD state managers"
    else:
        reason = f"APP_ENV={app_env} requires durable sweep/EOD state managers"
    override_note = ""
    if os.getenv("ALLOW_MOCK_SWEEP_STATE") is not None:
        override_note = (
            " ALLOW_MOCK_SWEEP_STATE is only permitted for explicit local/dev/test "
            "workflows and is ignored here."
        )
    return RuntimeError(
        "Sweep/EOD durable state initialization failed: "
        f"{exc}. {reason}; mock fallback is disabled."
        f"{override_note}"
    )


def _create_durable_sweep_and_eod_state_managers(
    settings: Any,
) -> tuple[SweepStateManager, EODStateManager]:
    if settings.sweep_state_backend.lower() == "postgres":
        dsn = get_sweep_state_dsn(settings)
        sweep_state_store = PostgresSweepStateStore(
            dsn, settings.default_time_zone
        )
        eod_state_store = PostgresEODStateStore(
            dsn, settings.default_time_zone
        )
    else:
        from google.cloud import firestore

        firestore_client = firestore.Client()
        sweep_state_store = SweepStateStore(
            firestore_client, settings.default_time_zone
        )
        eod_state_store = EODStateStore(
            firestore_client, settings.default_time_zone
        )
    return SweepStateManager(sweep_state_store), EODStateManager(eod_state_store)


def _initialize_sweep_and_eod_state_managers(
    settings: Any,
) -> tuple[SweepStateManager, EODStateManager]:
    try:
        return _create_durable_sweep_and_eod_state_managers(settings)
    except Exception as exc:
        if _allow_mock_sweep_state_for_error(exc):
            logger.warning(
                "State managers initialization failed: %s. Using local mock sweep/EOD state.",
                exc,
            )
            return (
                _create_mock_sweep_state_manager(),
                _create_mock_eod_state_manager(),
            )
        if _requires_durable_sweep_state():
            raise _build_durable_state_initialization_error(exc) from exc
        raise


class PnLBootstrapResult:
    """Captures PnL bootstrap outcome: complete, partial, or failed."""

    __slots__ = ("status", "seeded", "account_pairs_count", "error")

    def __init__(
        self,
        status: str,
        seeded: int,
        account_pairs_count: int,
        error: Optional[str] = None,
    ) -> None:
        self.status = status  # "complete" | "partial" | "failed"
        self.seeded = seeded
        self.account_pairs_count = account_pairs_count
        self.error = error

    @property
    def ok(self) -> bool:
        return self.status == "complete"


def _seed_runtime_pnl_snapshots(
    *,
    pnl_engine: PnLEngine,
    settings: Any,
    account_loader: Optional[Callable[[], Iterable[Any]]] = None,
) -> PnLBootstrapResult:
    trade_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()

    if account_loader is None:
        from app.tenants.firestore_client import get_active_broker_accounts

        account_loader = get_active_broker_accounts

    seeded = 0
    account_pairs: set[tuple[str, str]] = set()
    accounts_loaded = True
    try:
        accounts = list(account_loader())
    except Exception as exc:
        accounts_loaded = False
        accounts = []
        if trade_mode == "LIVE":
            logger.error(
                "PnL snapshot bootstrap FAILED: could not load active accounts in LIVE mode: %s",
                exc,
            )
            raise RuntimeError(
                f"TRADE_MODE=LIVE PnL bootstrap failed: active accounts unavailable: {exc}"
            ) from exc
        logger.warning("PnL snapshot bootstrap failed: could not load active accounts: %s", exc)

    for account in accounts:
        tenant_raw = str(getattr(account, "tenant_id", "") or "").strip()
        broker_raw = str(getattr(account, "broker_account_id", "") or "").strip()
        if not tenant_raw or not broker_raw:
            continue
        key = (tenant_raw, broker_raw)
        if key in account_pairs:
            continue
        account_pairs.add(key)
        if pnl_engine.ensure_account_snapshot(
            tenant_id=TenantId(tenant_raw),
            broker_account_id=BrokerAccountId(broker_raw),
        ):
            seeded += 1

    fallback_tenant = str(getattr(settings, "hub_default_tenant_id", "") or "").strip()
    fallback_broker = str(getattr(settings, "hub_default_broker_account_id", "") or "").strip()
    if fallback_tenant and fallback_broker:
        fallback_key = (fallback_tenant, fallback_broker)
        if fallback_key not in account_pairs:
            if pnl_engine.ensure_account_snapshot(
                tenant_id=TenantId(fallback_tenant),
                broker_account_id=BrokerAccountId(fallback_broker),
            ):
                seeded += 1
                account_pairs.add(fallback_key)

    # Determine bootstrap outcome status
    if not accounts_loaded:
        status = "failed"
        error_detail = "active accounts unavailable"
    elif len(account_pairs) == 0 and seeded == 0:
        status = "partial"
        error_detail = "no accounts discovered"
    else:
        status = "complete"
        error_detail = None

    result = PnLBootstrapResult(
        status=status,
        seeded=seeded,
        account_pairs_count=len(account_pairs),
        error=error_detail,
    )
    logger.info(
        "PnL snapshot bootstrap %s: seeded=%d account_pairs=%d%s",
        result.status,
        result.seeded,
        result.account_pairs_count,
        f" error={result.error}" if result.error else "",
    )
    return result


class HubRuntime:
    # Wire together core hub engines, routers, and state.
    def __init__(self) -> None:
        self.settings = get_settings()
        self.clock: IClock = SystemClock()

        # Core engines
        self.capital_engine = CapitalEngine()
        self.pnl_state_store = build_pnl_state_store(self.settings)
        self.pnl_engine = PnLEngine(
            state_store=self.pnl_state_store,
            clock=self.clock,
        )
        self._pnl_bootstrap = _seed_runtime_pnl_snapshots(
            pnl_engine=self.pnl_engine,
            settings=self.settings,
        )
        self.risk_engine = RiskEngine(pnl_engine=self.pnl_engine)
        self.profit_sweep_engine = SweepProfitEngine(self.pnl_engine)

        # Hub + state store
        self.position_ownership_store = build_position_ownership_store()
        self.order_submission_outbox = build_order_submission_outbox()
        self.hub = Hub(
            pnl_engine=self.pnl_engine,
            position_ownership_store=self.position_ownership_store,
        )
        self.state_store: StateStore = self.hub.state_store
        self.profit_lock_manager = ProfitLockManager(
            default_time_zone=self.settings.default_time_zone
        )
        self.profit_engine = ProfitEngine(
            self.pnl_engine,
            state_store=self.state_store,
            profit_lock_manager=self.profit_lock_manager,
        )
        self.order_lifecycle = OrderLifecycleService(
            state_store=self.state_store,
            pnl_engine=self.pnl_engine,
            position_ownership_store=self.position_ownership_store,
            submission_outbox=self.order_submission_outbox,
            clock=self.clock,
        )

        # Trading circuit breaker (RISK-5.4)
        self.circuit_breaker = TradingCircuitBreaker(
            config=CircuitBreakerConfig.from_env(),
        )
        logger.info(
            "Circuit breaker initialized: loss_streak=%s reject_rate=%s volatility=%s (threshold=%.1f)",
            self.circuit_breaker._config.loss_streak_enabled,
            self.circuit_breaker._config.reject_rate_enabled,
            self.circuit_breaker._config.volatility_enabled,
            self.circuit_breaker._config.volatility_threshold,
        )

        # Wire fault tracker's TRIP_CIRCUIT_BREAKER action to the real breaker.
        cb_ref = self.circuit_breaker

        def _trip_circuit_breaker_handler(event: MitigationEvent) -> None:
            trip = BreakerTrip(
                breaker_type=BreakerType.REJECT_RATE,
                tripped_at=event.timestamp,
                cooldown_seconds=600.0,
                reason=f"auto_mitigation:{event.rule_name} faults={event.fault_count}",
            )
            with cb_ref._lock:
                cb_ref._active_trips[BreakerType.REJECT_RATE] = trip
            logger.warning(
                "Circuit breaker tripped by auto-mitigation: rule=%s scope=%s:%s",
                event.rule_name,
                event.scope_key,
                event.scope_value,
            )

        try:
            fault_tracker = get_fault_tracker()
            fault_tracker.set_action_handler(
                MitigationAction.TRIP_CIRCUIT_BREAKER,
                _trip_circuit_breaker_handler,
            )
        except Exception:
            logger.exception("Failed to wire fault tracker to circuit breaker")

        # Order router
        self.order_router = OrderRouter(
            hub=self.hub,
            capital_engine=self.capital_engine,
            risk_engine=self.risk_engine,
            profit_engine=self.profit_engine,
            state_store=self.state_store,
            pnl_engine=self.pnl_engine,
            order_lifecycle=self.order_lifecycle,
            position_ownership_store=self.position_ownership_store,
            submission_outbox=self.order_submission_outbox,
            clock=self.clock,
            circuit_breaker=self.circuit_breaker,
        )

        # Routing table: build initial strategy -> (tenant, broker) map from Firestore
        self.routing_table = get_global_routing_table()
        try:
            self.routing_table.refresh()
        except Exception:
            logger.exception("HubRuntime: routing table refresh failed during init")

        # Sweep and EOD state manager for profit sweep orchestration
        (
            self.sweep_state_manager,
            self.eod_state_manager,
        ) = _initialize_sweep_and_eod_state_managers(self.settings)

        # Durable kill-switch state machine (Architecture §12.1)
        # Loaded from Postgres at startup by AppRuntime; starts as empty in-memory.
        from app.risk.kill_switch import KillSwitchManager
        self.kill_switch_manager = KillSwitchManager()

        # Hub-level exit orchestrators
        self.hub_profit_sweep_engine = HubProfitSweepEngine(
            settings=self.settings,
            pnl_engine=self.pnl_engine,
            sweep_engine=self.profit_sweep_engine,
            state_store=self.state_store,
            order_router=self.order_router,
            sweep_state_manager=self.sweep_state_manager,
            profit_lock_manager=self.profit_lock_manager,
            clock=self.clock,
        )
        self.eod_exit_engine = EODExitEngine(
            settings=self.settings,
            state_store=self.state_store,
            order_router=self.order_router,
            eod_state_manager=self.eod_state_manager,
            clock=self.clock,
        )

    def update_volatility(self, volatility_proxy: float) -> None:
        """Feed current volatility (e.g. India VIX) into the circuit breaker.

        Call this from any tick handler, scheduled job, or API endpoint that
        obtains the current India VIX level. The circuit breaker will block
        new entry orders when volatility exceeds its configured threshold.
        """
        if self.circuit_breaker is not None:
            self.circuit_breaker.update_volatility(volatility_proxy)


# Return a cached singleton HubRuntime instance.
@lru_cache(maxsize=1)
def get_hub_runtime() -> HubRuntime:
    return HubRuntime()


# Build a mock SweepStateManager for local development.
def _create_mock_sweep_state_manager() -> SweepStateManager:
    """Create a mock SweepStateManager for explicit local development/testing."""
    from unittest.mock import MagicMock

    mock_store = MagicMock(spec=SweepStateStore)
    # Always allow sweeps in mock mode
    mock_store.can_sweep_now.return_value = (True, None)
    mock_store.record_sweep.return_value = None

    return SweepStateManager(mock_store)

def _create_mock_eod_state_manager() -> EODStateManager:
    """Create a mock EODStateManager for explicit local development/testing."""
    from unittest.mock import MagicMock

    mock_store = MagicMock(spec=EODStateStore)
    mock_store.get_state.return_value = None
    mock_store.save_state.return_value = None
    manager = EODStateManager(mock_store)
    manager.has_exited_today = MagicMock(return_value=False)
    manager.record_eod_exit = MagicMock(return_value=True)
    return manager


__all__ = ["HubRuntime", "get_hub_runtime"]
