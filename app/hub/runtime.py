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
        try:
            hub = getattr(self, "hub", None)
            state_store = getattr(self, "state_store", None)
            if hub is None or state_store is None:
                return {"corrupt_count": 0, "samples": []}
            account_ids: list = []
            list_ids = getattr(hub, "list_runner_ids", None)
            if callable(list_ids):
                account_ids = [str(a) for a in list_ids()]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "audit_position_avg_price_corruption: setup failed (%s); "
                "returning corrupt_count=0", exc,
            )
            return {"corrupt_count": 0, "samples": []}

        # Issue #226 (PR #237 review P2): dict/alias normalization.
        # Some adapters / test harnesses store positions as dicts with
        # field aliases (``qty``/``net_qty`` for quantity, ``avgPrice``/
        # ``entry_price`` for avg_price) — the same shapes the dashboard
        # normalizer accepts. Without this, the audit misses exactly the
        # corrupt rows that the dashboard's reactive defence already
        # surfaces.
        def _first_present(p, *keys):
            for k in keys:
                if isinstance(p, dict):
                    if k in p and p[k] is not None:
                        return p[k]
                else:
                    v = getattr(p, k, None)
                    if v is not None:
                        return v
            return None

        # Issue #226 (PR #237 round-2 review P2/P3): comprehensive alias
        # support + decimal-string coercion. Aliases observed in this
        # repo:
        #   quantity: quantity, qty, net_qty, netqty
        #   avg_price: avg_price, average_price, avgPrice, avgprice,
        #              averageprice, entry_price
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
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        corrupt: list[dict] = []
        # Issue #226 (PR #237 round-2 review P2): per-account read
        # failures must be SURFACED — otherwise a get_positions
        # exception leaves the audit with corrupt_count=0 and /readyz
        # passes despite that account never being scanned.
        read_failures: list[dict] = []
        for account_id in account_ids:
            try:
                positions = state_store.get_positions(account_id) or []
            except Exception as exc:
                read_failures.append(
                    {"account_id": str(account_id), "error": repr(exc)}
                )
                logger.error(
                    "audit_position_avg_price_corruption: get_positions "
                    "failed for account=%s — account NOT scanned (%s)",
                    account_id, exc,
                )
                continue
            for pos in positions:
                # PR #237 round-2 review P2: include ``net`` alias for
                # broker-shaped position rows where ``netqty`` is
                # absent — the Angel parser explicitly falls back from
                # ``netqty`` to ``net``.
                qty_raw = _first_present(
                    pos, "quantity", "qty", "net_qty", "netqty", "net",
                )
                qty = _coerce_int(qty_raw)
                if qty == 0:
                    continue
                # PR #237 round-2 review P2: honour broker fallback
                # average-price fields. Raw Angel rows often carry
                # ``avgprice=0`` while a valid ``netavgprice`` /
                # ``netprice`` / ``buyavgprice`` / ``sellavgprice`` is
                # present — the angel_client and position_sync paths
                # treat zero as missing and fall back. The audit must
                # do the same; otherwise a healthy open row like
                # ``netqty=65, avgprice=0, netavgprice=100`` is
                # incorrectly reported as corrupt.
                avg_raw = _first_present(
                    pos,
                    "avg_price", "average_price", "avgPrice", "avgprice",
                    "averageprice", "entry_price",
                )
                avg_f = _coerce_float(avg_raw)
                if avg_f <= 0.0:
                    # Try broker fallback fields (Angel-shaped rows).
                    fallback_raw = _first_present(
                        pos,
                        "netavgprice", "netprice",
                        "buyavgprice", "sellavgprice",
                    )
                    avg_f = _coerce_float(fallback_raw)
                if avg_f > 0.0:
                    continue
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
                        "avg_price": avg_f,
                    }
                )
                # Rate-limit alerts per (account, symbol) — once per 60s
                # to avoid flooding logs while corruption persists.
                self._maybe_emit_avg_price_corruption_alert(
                    account_id=str(account_id),
                    symbol=symbol,
                    quantity=qty,
                    avg_price=avg_f,
                )
        return {
            "corrupt_count": len(corrupt),
            "samples": corrupt[:5],  # cap payload
            # Issue #226 (PR #237 round-2 review P2): surface per-account
            # read failures so /readyz can fail closed when accounts
            # were not scanned. Empty list means every account was
            # successfully scanned.
            "read_failures": read_failures,
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
