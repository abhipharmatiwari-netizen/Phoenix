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
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from app.config.settings import get_settings
from app.core.clock import IClock, SystemClock
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
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
from app.pnl.position_trailing_lock import (
    PositionTrailingLockManager,
    PostgresPositionTrailingLockBackend,
)
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
    PositionTrailingLockEngine,
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

    seeded = 0      # new snapshots created (first start of day for this account)
    restored = 0    # existing same-day snapshots retained with persisted realized_pnl
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
        else:
            restored += 1

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
            else:
                restored += 1
                account_pairs.add(fallback_key)

    # Determine bootstrap outcome status
    if not accounts_loaded:
        status = "failed"
        error_detail = "active accounts unavailable"
    elif len(account_pairs) == 0 and seeded == 0 and restored == 0:
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
    # §75: seeded=N means new snapshots (first start of today).
    # restored=N means existing same-day snapshots were found in the state
    # store with their persisted realized_pnl intact — PnL IS carried forward
    # across intra-day restarts when PostgresPnLStateStore is active.
    logger.info(
        "PnL snapshot bootstrap %s: seeded=%d restored=%d account_pairs=%d%s",
        result.status,
        result.seeded,
        restored,
        result.account_pairs_count,
        f" error={result.error}" if result.error else "",
    )

    # Restore ephemeral open-position state (net_open_qty / open_avg_price) from
    # internal_position_records so display_realized_pnl is correct after restarts.
    _restore_pnl_open_positions(pnl_engine)

    return result


def _restore_pnl_open_positions(pnl_engine: "PnLEngine") -> None:
    """Populate net_open_qty/open_avg_price on PnL snapshots from DB position records.

    These ephemeral fields are not persisted in pnl_snapshots; without this
    restore step the display_realized_pnl correction would be wrong after a
    container restart because net_open_qty would start at 0, causing the
    cash-flow sell proceeds of open shorts to appear as realized profit.
    """
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT tenant_id, account_id, strategy_id, net_qty, avg_open_price
                    FROM internal_position_records
                    WHERE position_state NOT IN ('FLAT', 'NONE')
                      AND net_qty != 0
                """)
                rows = cur.fetchall()
        for tenant_id, account_id, strategy_id, net_qty, avg_open_price in rows:
            try:
                pnl_engine.restore_open_position(
                    tenant_id=TenantId(tenant_id),
                    broker_account_id=BrokerAccountId(account_id),
                    strategy_id=StrategyId(strategy_id),
                    net_open_qty=int(net_qty or 0),
                    open_avg_price=float(avg_open_price or 0.0),
                )
            except Exception as exc:
                logger.warning("pnl.restore_open_position failed for %s/%s/%s: %s",
                               tenant_id, account_id, strategy_id, exc)
        if rows:
            logger.info(
                "pnl.open_position_restore: restored net_open_qty for %d position(s)", len(rows)
            )
    except Exception as exc:
        logger.warning("pnl.open_position_restore failed (non-fatal): %s", exc)


class HubRuntime:
    # Wire together core hub engines, routers, and state.
    def __init__(self) -> None:
        self.settings = get_settings()
        self.clock: IClock = SystemClock()
        # Issue #222: legacy-kill-switch state publisher.
        # Stream-path ``RiskManager`` lives in the stream worker (separate
        # object instance from the hub) but the same process. The bridge
        # in ``RiskManager._propagate_kill_switch_to_durable_manager``
        # also publishes its current legacy ``kill_switch_activated`` flag
        # here so the hub can detect divergence — i.e. legacy=True while
        # durable ``KillSwitchManager`` is INACTIVE. Such divergence
        # indicates the bridge has not yet succeeded (transient infra
        # failure) and is the silent-bypass condition that bit live on
        # 2026-05-08 for 23 minutes.
        # Thread-safe via _legacy_lock; serializable via
        # ``get_legacy_kill_switch_snapshot``.
        from threading import Lock as _Lock
        self._legacy_kill_switch_lock = _Lock()
        self._legacy_kill_switch_active: bool = False
        self._legacy_kill_switch_reason: Optional[str] = None
        self._legacy_kill_switch_updated_at: Optional[Any] = None  # datetime
        self._legacy_kill_switch_publisher_seen: bool = False
        # PR #234 round-1 review P2: track the first-active timestamp
        # separately from updated_at so divergence_age_seconds reports
        # how long legacy has been halted, not the heartbeat interval
        # since the last republish.
        self._legacy_kill_switch_first_active_at: Optional[Any] = None

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
        # Single shared processed-trade store: OrderLifecycleService and the
        # external-fill reconciler both atomically claim_processed against
        # the same backing store so a fill is booked exactly once regardless
        # of which path observes it first.
        from app.orders.trade_processed_store import build_processed_trade_store
        self.processed_trade_store = build_processed_trade_store()
        # Reconciler ingests broker-side fills that Phoenix didn't place
        # (manual UI exits, broker-side stops). Constructed before the Hub
        # so the Hub can pass it to each AccountRunner at creation time.
        from app.orders.external_fill_reconciler import ExternalFillReconciler
        self.external_fill_reconciler = ExternalFillReconciler(
            pnl_engine=self.pnl_engine,
            processed_store=self.processed_trade_store,
        )
        self.hub = Hub(
            pnl_engine=self.pnl_engine,
            position_ownership_store=self.position_ownership_store,
            external_fill_reconciler=self.external_fill_reconciler,
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
            processed_store=self.processed_trade_store,
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

        # Routing table: build initial strategy -> (tenant, broker) map from control-plane backend (Postgres in LIVE)
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

        # Per-position trailing profit lock (independent of account-level lock).
        # Postgres-backed in LIVE so peaks survive restarts; no-op backend
        # otherwise. Engine is instantiated unconditionally; the engine's own
        # `_enabled()` check (driven by POSITION_TRAILING_LOCK_ENABLED) gates
        # whether it actually evaluates anything.
        position_trailing_backend = None
        if _runtime_trade_mode() == "LIVE":
            try:
                position_trailing_backend = PostgresPositionTrailingLockBackend(
                    dsn=get_sweep_state_dsn(self.settings)
                )
            except Exception as exc:
                logger.warning(
                    "position_trailing_lock: Postgres backend init failed; "
                    "trailing lock will run with in-memory state only: %s",
                    exc,
                )
        self.position_trailing_lock_manager = PositionTrailingLockManager(
            default_time_zone=self.settings.default_time_zone,
            backend=position_trailing_backend,
        )
        # Issue #219: pass a provider closure rather than a direct reference
        # so the engine always observes the CURRENT durable KillSwitchManager.
        # ``AppRuntime`` replaces ``self.kill_switch_manager`` after loading
        # state from Postgres at startup (see ``app/runtime/app_runtime.py``
        # step 3 — kill-switch durable state restore). A direct reference
        # captured here would point at the original empty pre-load instance
        # while the bridge / interceptor / admin routes all consult the
        # post-load replacement — Codex flagged this on PR #231 and it is a
        # real bug in the LIVE startup path.
        self.position_trailing_lock_engine = PositionTrailingLockEngine(
            settings=self.settings,
            state_store=self.state_store,
            order_router=self.order_router,
            manager=self.position_trailing_lock_manager,
            clock=self.clock,
            kill_switch_manager_provider=lambda: self.kill_switch_manager,
        )

    def audit_position_avg_price_corruption(self) -> dict:
        """Issue #226: scan all account-scoped position views for the
        ``qty != 0 and avg_price <= 0`` corruption pattern and emit a
        rate-limited ALERT for each finding.

        Background: ``app/dashboard/tenant_routes.py`` already refuses to
        compute Unrealized PnL when an open position has avg_price<=0
        (the 2026-05-08 ``POSITION_VIEW_AVG_PRICE_INVALID qty=7500
        avg_price=0.0`` log). That defence is reactive (per dashboard
        request). This audit is proactive — runs on every readiness
        probe — so corruption cannot hide between dashboard views.

        Returns ``{corrupt_count, samples}``. ``samples`` is a small
        list of ``{tenant_id, account_id, symbol, quantity, avg_price}``
        for the first few corrupt rows (capped to keep the payload
        small).
        """
        # Lazy import to avoid heavy hub initialisation cycles in test
        # contexts that build a HubRuntime-shaped stub.
        # PR #237 round-4 review P2: when account enumeration raises
        # we MUST surface that as an audit error rather than silently
        # returning ``corrupt_count=0``; otherwise /readyz can pass
        # with no accounts ever scanned.
        try:
            hub = getattr(self, "hub", None)
            state_store = getattr(self, "state_store", None)
            if hub is None or state_store is None:
                return {
                    "corrupt_count": 0,
                    "samples": [],
                    "read_failures": [],
                    "setup_error": None,
                }
            account_ids: list = []
            list_ids = getattr(hub, "list_runner_ids", None)
            if callable(list_ids):
                account_ids = [str(a) for a in list_ids()]
        except Exception as exc:
            logger.error(
                "audit_position_avg_price_corruption: account enumeration "
                "failed (%s) — failing closed in caller", exc,
            )
            return {
                "corrupt_count": 0,
                "samples": [],
                "read_failures": [],
                "setup_error": repr(exc),
            }

        # Issue #226 (PR #237 review P2): dict/alias normalization.
        # Some adapters / test harnesses store positions as dicts with
        # field aliases (``qty``/``net_qty`` for quantity, ``avgPrice``/
        # ``entry_price`` for avg_price) — the same shapes the dashboard
        # normalizer accepts. Without this, the audit misses exactly the
        # corrupt rows that the dashboard's reactive defence already
        # surfaces.
        def _first_present(p, *keys):
            """Return the first key whose value is present (not None
            and not a blank string). PR #237 round-5 review P2/P3:
            blank strings are treated as MISSING so a row carrying
            ``{'netqty': '', 'net': '65', ...}`` falls through to
            ``net``, matching the angel_client normalizer; same for a
            blank ``symbol`` field falling through to ``tradingsymbol``.
            """
            for k in keys:
                if isinstance(p, dict):
                    if k in p:
                        v = p[k]
                        if v is not None and not (
                            isinstance(v, str) and not v.strip()
                        ):
                            return v
                else:
                    v = getattr(p, k, None)
                    if v is not None and not (
                        isinstance(v, str) and not v.strip()
                    ):
                        return v
            return None

        def _pick_nonzero(*values) -> Optional[float]:
            """Return the first non-zero, non-None float in the list.
            Mirrors ``app/core/position_sync.py::_pick_nonzero_price``
            so the audit's broker-fallback handling matches the
            production normalizer exactly."""
            for v in values:
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                f = _coerce_float(v)
                if f != 0.0:
                    return f
            return None

        # Issue #226 (PR #237 round-2 review P2/P3): comprehensive alias
        # support + decimal-string coercion. Aliases observed in this
        # repo:
        #   quantity: quantity, qty, net_qty, netqty, net
        #   avg_price: avg_price, average_price, avgPrice, avgprice,
        #              averageprice, entry_price
        #   broker fallback: netavgprice/netAvgPrice, netprice/netPrice,
        #                    buyavgprice/buyAvgPrice (qty>0 only),
        #                    sellavgprice/sellAvgPrice (qty<0 only)
        #   symbol:    symbol, tradingsymbol, trading_symbol
        # Decimal strings like "65.0" must coerce via int(float(v))
        # rather than int(v) directly — otherwise the audit silently
        # drops corrupt records that have serialized numeric quantities.
        def _coerce_int(value) -> int:
            try:
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        return 0
                return int(float(value))
            except (TypeError, ValueError):
                return 0

        def _coerce_float(value) -> float:
            try:
                if isinstance(value, str):
                    value = value.strip()
                    if not value:
                        return 0.0
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        corrupt: list[dict] = []
        # Issue #226 (PR #237 round-2 review P2): per-account read
        # failures must be SURFACED — otherwise a get_positions
        # exception leaves the audit with corrupt_count=0 and /readyz
        # passes despite that account never being scanned.
        read_failures: list[dict] = []
        # PR #237 round-3 review P3: rate-limit per-account read-failure
        # ERRORs so a persistent state-store outage cannot flood logs
        # on every readiness probe; the structured ``read_failures``
        # list still flows back to /readyz which fails closed in LIVE.
        if not hasattr(self, "_avg_price_audit_read_failure_log_state"):
            self._avg_price_audit_read_failure_log_state: Dict[str, Any] = {}
        for account_id in account_ids:
            try:
                positions = state_store.get_positions(account_id) or []
            except Exception as exc:
                read_failures.append(
                    {"account_id": str(account_id), "error": repr(exc)}
                )
                from datetime import datetime as _dt, timezone as _tz
                _now = _dt.now(_tz.utc)
                _last = self._avg_price_audit_read_failure_log_state.get(
                    str(account_id)
                )
                if _last is None or (_now - _last).total_seconds() >= 60.0:
                    self._avg_price_audit_read_failure_log_state[
                        str(account_id)
                    ] = _now
                    logger.error(
                        "audit_position_avg_price_corruption: get_positions "
                        "failed for account=%s — account NOT scanned (%s)",
                        account_id, exc,
                    )
                continue
            def _maybe_get(p, key):
                if isinstance(p, dict):
                    return p.get(key)
                return getattr(p, key, None)

            def _alias_is_present_skip_blank(value) -> bool:
                """True iff value is non-None and not a blank string."""
                if value is None:
                    return False
                if isinstance(value, str) and not value.strip():
                    return False
                return True

            for pos in positions:
                # PR #237 round-2/round-6 review P2: resolve quantity
                # via FIRST-NON-ZERO across all known qty aliases so a
                # broker-shaped row like ``{'qty': 0, 'netqty': 65}``
                # detects the open position. Order matches the Angel
                # parser (``netqty``→``net``) and falls back to the
                # dataclass / normalized aliases.
                qty_value = _pick_nonzero(
                    _maybe_get(pos, "netqty"),
                    _maybe_get(pos, "net"),
                    _maybe_get(pos, "quantity"),
                    _maybe_get(pos, "qty"),
                    _maybe_get(pos, "net_qty"),
                )
                if qty_value is None:
                    continue
                qty = _coerce_int(qty_value)
                if qty == 0:
                    continue

                # PR #237 round-4/round-5/round-6 review P2: tiered
                # avg-price resolution.
                #
                #   Tier 1 — normalized authoritative ``avg_price``
                #   (snake_case): if PRESENT (including blank string,
                #   which dashboard ``_first_present`` treats as
                #   present and coerces to 0), apply dashboard
                #   semantics — zero or negative is corrupt regardless
                #   of any other alias. This preserves the existing
                #   POSITION_VIEW_AVG_PRICE_INVALID signal.
                #
                #   Tier 2-3 — broker primary + net chain (single
                #   first-non-zero across all aliases, ordering matches
                #   ``app/core/position_sync.py::_extract_broker_avg_price``
                #   exactly: ``avgprice`` first, then ``avgPrice``,
                #   ``average_price``, ``averageprice``, then
                #   ``netprice``, ``netPrice``, ``netavgprice``,
                #   ``netAvgPrice``). A negative result is corrupt
                #   regardless of any later fallback.
                #
                #   Tier 4 — side-specific fallback gated by qty sign:
                #     qty>0: ``buyavgprice``/``buyAvgPrice``/
                #            ``buyaverageprice``/``buyAveragePrice``
                #     qty<0: ``sellavgprice``/``sellAvgPrice``/
                #            ``sellaverageprice``/``sellAveragePrice``
                #
                #   Tier 5 — final fallback ``entry_price`` ONLY when
                #   no broker chain alias was present at all (any
                #   broker alias being present at zero already makes
                #   the row corrupt — entry_price cannot mask it).

                # Tier 1: snake_case avg_price authoritative when
                # present. Match dashboard ``_first_present``: only
                # ``None`` is missing — blank strings, zero, and
                # negatives are PRESENT and govern the verdict.
                snake_case_avg = _maybe_get(pos, "avg_price")
                if snake_case_avg is not None:
                    snake_f = _coerce_float(snake_case_avg)
                    if snake_f > 0.0:
                        continue  # healthy via snake_case
                    avg_f_for_report = snake_f
                else:
                    # Tier 2-3: production broker chain (primary then
                    # net), single first-non-zero in production order.
                    broker_chain_keys = (
                        "avgprice", "avgPrice",
                        "average_price", "averageprice",
                        "netprice", "netPrice",
                        "netavgprice", "netAvgPrice",
                    )
                    broker_chain_values = [
                        _maybe_get(pos, k) for k in broker_chain_keys
                    ]
                    broker_chain_present = any(
                        _alias_is_present_skip_blank(v)
                        for v in broker_chain_values
                    )
                    broker_value = _pick_nonzero(*broker_chain_values)
                    if broker_value is not None and broker_value < 0.0:
                        # Negative broker avg → corrupt regardless of
                        # any later fallback (entry_price must not
                        # mask a negative net/primary).
                        avg_f_for_report = broker_value
                    elif broker_value is not None and broker_value > 0.0:
                        continue  # healthy via broker chain
                    else:
                        # broker_value is None — broker chain returned
                        # no non-zero value. Try side-specific fallback.
                        if qty > 0:
                            side_keys = (
                                "buyavgprice", "buyAvgPrice",
                                "buyaverageprice", "buyAveragePrice",
                            )
                        elif qty < 0:
                            side_keys = (
                                "sellavgprice", "sellAvgPrice",
                                "sellaverageprice", "sellAveragePrice",
                            )
                        else:
                            side_keys = ()
                        side_values = [
                            _maybe_get(pos, k) for k in side_keys
                        ]
                        side_present = any(
                            _alias_is_present_skip_blank(v)
                            for v in side_values
                        )
                        side_value = _pick_nonzero(*side_values)
                        if side_value is not None and side_value < 0.0:
                            # Negative side-specific avg → corrupt.
                            avg_f_for_report = side_value
                        elif side_value is not None and side_value > 0.0:
                            continue  # healthy via side fallback
                        elif broker_chain_present or side_present:
                            # Some broker alias was PRESENT but all
                            # were zero — corrupt. entry_price must
                            # not mask a broker-explicit zero.
                            avg_f_for_report = 0.0
                        else:
                            # No broker alias at all → tier 5.
                            entry_raw = _maybe_get(pos, "entry_price")
                            if _alias_is_present_skip_blank(entry_raw):
                                entry_f = _coerce_float(entry_raw)
                                if entry_f > 0.0:
                                    continue  # healthy via entry_price
                                avg_f_for_report = entry_f
                            else:
                                # Truly nothing — corrupt.
                                avg_f_for_report = 0.0
                # Found a corrupt record.
                symbol = str(
                    _first_present(
                        pos, "symbol", "tradingsymbol", "trading_symbol",
                    ) or ""
                )
                tenant_id = str(_first_present(pos, "tenant_id") or "")
                corrupt.append(
                    {
                        "tenant_id": tenant_id,
                        "account_id": str(account_id),
                        "symbol": symbol,
                        "quantity": qty,
                        "avg_price": avg_f_for_report,
                    }
                )
                # Rate-limit alerts per (account, symbol) — once per 60s
                # to avoid flooding logs while corruption persists.
                self._maybe_emit_avg_price_corruption_alert(
                    account_id=str(account_id),
                    symbol=symbol,
                    quantity=qty,
                    avg_price=avg_f_for_report,
                )
        return {
            "corrupt_count": len(corrupt),
            "samples": corrupt[:5],  # cap payload
            # Issue #226 (PR #237 round-2 review P2): surface per-account
            # read failures so /readyz can fail closed when accounts
            # were not scanned. Empty list means every account was
            # successfully scanned.
            "read_failures": read_failures,
            "setup_error": None,
        }

    def _maybe_emit_avg_price_corruption_alert(
        self, *, account_id: str, symbol: str, quantity: int, avg_price: float,
    ) -> None:
        """Issue #226: rate-limited (1 per 60s per scope) ALERT-level
        event for each detected corruption."""
        from datetime import datetime as _dt, timezone as _tz
        if not hasattr(self, "_avg_price_alert_log_state"):
            self._avg_price_alert_log_state: Dict[Tuple[str, str], Any] = {}
        key = (account_id, symbol)
        now = _dt.now(_tz.utc)
        last = self._avg_price_alert_log_state.get(key)
        if last is not None and (now - last).total_seconds() < 60.0:
            return
        self._avg_price_alert_log_state[key] = now
        try:
            from app.core.logging_utils import log_event as _log_event
            import logging as _logging
            _log_event(
                logger,
                event_type="POSITION_AVG_PRICE_CORRUPTION",
                message=(
                    "Internal position record has qty != 0 but avg_price "
                    "<= 0 — Unrealized PnL cannot be computed and exit "
                    "engines / risk gates may operate on fiction. "
                    "Verify broker-side position and reconcile internal "
                    "records (issue #226)."
                ),
                level=_logging.ERROR,
                broker_account_id=account_id,
                instrument=symbol,
                quantity=quantity,
                avg_price=avg_price,
            )
        except Exception:
            logger.error(
                "POSITION_AVG_PRICE_CORRUPTION account=%s symbol=%s "
                "qty=%d avg_price=%s",
                account_id, symbol, quantity, avg_price,
            )

    def update_volatility(self, volatility_proxy: float) -> None:
        """Feed current volatility (e.g. India VIX) into the circuit breaker.

        Call this from any tick handler, scheduled job, or API endpoint that
        obtains the current India VIX level. The circuit breaker will block
        new entry orders when volatility exceeds its configured threshold.
        """
        if self.circuit_breaker is not None:
            self.circuit_breaker.update_volatility(volatility_proxy)

    # ----------------------------------------------------------------------
    # Issue #222: legacy-kill-switch publisher + divergence detection.
    # ----------------------------------------------------------------------

    def record_legacy_kill_switch_state(
        self,
        *,
        active: bool,
        reason: Optional[str] = None,
    ) -> None:
        """Publish the stream-path ``RiskManager.kill_switch_activated``
        state to the hub so that ``/readyz`` and admin endpoints can
        detect divergence with the durable ``KillSwitchManager``.

        Called by ``RiskManager.evaluate_account_loss`` on every cycle
        (including when the legacy flag is False — so ``False`` overwrites
        any earlier ``True``). Thread-safe via ``_legacy_kill_switch_lock``.
        """
        from datetime import datetime as _dt, timezone as _tz
        now_utc = _dt.now(_tz.utc)
        new_active = bool(active)
        with self._legacy_kill_switch_lock:
            prev_active = self._legacy_kill_switch_active
            self._legacy_kill_switch_active = new_active
            self._legacy_kill_switch_reason = reason
            self._legacy_kill_switch_updated_at = now_utc
            self._legacy_kill_switch_publisher_seen = True
            # PR #234 round-1 review P2: track first-active timestamp
            # separately so divergence_age_seconds reports how long
            # legacy has been halted, not just the heartbeat interval.
            # Set on False→True transition; clear on True→False.
            if new_active and not prev_active:
                self._legacy_kill_switch_first_active_at = now_utc
            elif not new_active:
                self._legacy_kill_switch_first_active_at = None

    def get_legacy_kill_switch_snapshot(self) -> dict:
        """Return a serializable snapshot of the published legacy state.

        ``publisher_seen`` is False until ``record_legacy_kill_switch_state``
        has been called at least once — useful for distinguishing
        "no publisher running" from "publisher running and reports False".
        """
        with self._legacy_kill_switch_lock:
            updated_iso = (
                self._legacy_kill_switch_updated_at.isoformat().replace(
                    "+00:00", "Z"
                )
                if self._legacy_kill_switch_updated_at is not None
                else None
            )
            first_active_iso = (
                self._legacy_kill_switch_first_active_at.isoformat().replace(
                    "+00:00", "Z"
                )
                if self._legacy_kill_switch_first_active_at is not None
                else None
            )
            return {
                "publisher_seen": bool(self._legacy_kill_switch_publisher_seen),
                "legacy_active": bool(self._legacy_kill_switch_active),
                "legacy_reason": self._legacy_kill_switch_reason,
                "legacy_updated_at": updated_iso,
                # PR #234 round-1 review P2: first-active timestamp for
                # accurate divergence-age reporting.
                "legacy_first_active_at": first_active_iso,
            }

    def compute_kill_switch_divergence(self) -> dict:
        """Detect divergence between legacy ``RiskManager.kill_switch_activated``
        and durable ``KillSwitchManager`` GLOBAL state.

        Divergence definition (issue #222): ``legacy_active=True`` AND the
        durable manager has NO active record at GLOBAL scope (i.e.
        ``is_tripped(GLOBAL, "GLOBAL")=False``). This is the silent-
        bypass scenario — the legacy stream-path treats trading as
        halted but the hub OrderRouter is still routing entries.

        Returns a dict with:
        - ``divergent``: bool
        - ``legacy_active``: bool
        - ``durable_global_active``: bool (None if KSM unavailable)
        - ``legacy_reason``: str | None
        - ``divergence_age_seconds``: float | None — seconds since the
          publisher last reported (None if never reported)
        """
        from datetime import datetime as _dt, timezone as _tz
        snapshot = self.get_legacy_kill_switch_snapshot()
        legacy_active = snapshot["legacy_active"]
        try:
            from app.risk.kill_switch import KillSwitchScope
            ksm = getattr(self, "kill_switch_manager", None)
            durable_global_active = bool(
                ksm.is_tripped(KillSwitchScope.GLOBAL, "GLOBAL")
            ) if ksm is not None else None
        except Exception:
            durable_global_active = None

        if durable_global_active is None:
            divergent = False  # cannot compute without KSM
        else:
            divergent = legacy_active and not durable_global_active

        # PR #234 round-1 review P2: divergence_age_seconds reports
        # the time since legacy first became active in this episode,
        # not the heartbeat interval since the last republish. Falls
        # back to legacy_updated_at if first_active_at is unavailable
        # (e.g. legacy is currently inactive).
        age_seconds: Optional[float] = None
        age_source = (
            snapshot.get("legacy_first_active_at")
            or snapshot.get("legacy_updated_at")
        )
        if age_source:
            try:
                from datetime import datetime as _dt2
                ts_text = age_source
                if ts_text.endswith("Z"):
                    ts_text = ts_text[:-1] + "+00:00"
                ts = _dt2.fromisoformat(ts_text)
                age_seconds = (
                    _dt.now(_tz.utc) - ts
                ).total_seconds()
            except Exception:
                age_seconds = None

        return {
            "divergent": bool(divergent),
            "legacy_active": bool(legacy_active),
            "durable_global_active": durable_global_active,
            "legacy_reason": snapshot["legacy_reason"],
            "publisher_seen": snapshot["publisher_seen"],
            "divergence_age_seconds": age_seconds,
        }


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
