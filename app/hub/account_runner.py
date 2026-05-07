"""
AccountRunner: per-broker-account runner managed by the Hub (§4 Execution Hub).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from app.brokers.backoff import BackoffState
from app.core.audit_log import emit_audit_event
from app.brokers.base import (
    Balance,
    BrokerClient,
    OrderRequest,
    OrderResponse,
    OrderStatus,
)
from app.brokers.execution import ExecutionPort, create_execution_port
from app.brokers.positions_types import PositionsFetchResult, PositionsStatus
from app.core.feature_flags import load_stability_feature_flags
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.core.logging_utils import log_event
from app.data.state_store import StateStore
from app.orders.position_ownership import (
    PositionOwnershipStore,
    UNKNOWN_OWNER,
    UNKNOWN_STRATEGY_ID,
    derive_contract_key_from_position,
)
from app.pnl.pnl_engine import PnLEngine
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)

_POSITIONS_LOCKS: dict[tuple[str, BrokerAccountId], asyncio.Lock] = {}
_POSITIONS_BACKOFF: dict[tuple[str, BrokerAccountId], BackoffState] = {}
_POSITIONS_REFCOUNTS: dict[tuple[str, BrokerAccountId], int] = {}
_POSITIONS_REGISTRY_LOCK = threading.Lock()
_OPEN_ORDER_STATUSES = {
    "OPEN",
    "PENDING",
    "TRIGGER PENDING",
    "PUT ORDER REQ RECEIVED",
    "VALIDATION PENDING",
    "MODIFY PENDING",
    "AMO REQ RECEIVED",
    "AFTER MARKET ORDER REQ RECEIVED",
}
_TERMINAL_ORDER_STATUSES = {
    "COMPLETE",
    "COMPLETED",
    "REJECTED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "EXPIRED",
}


def _positions_cleanup_enabled() -> bool:
    return str(
        os.getenv("ACCOUNT_RUNNER_SHARED_POSITIONS_CLEANUP_ENABLED", "true")
    ).strip().lower() in {"1", "true", "yes", "on"}


class AccountRunner:
    # Initialize per-account runner state and polling settings.
    def __init__(
        self,
        *,
        tenant_id: TenantId,
        broker_account: BrokerAccountModel,
        broker_client: BrokerClient,
        execution_port: Optional[ExecutionPort] = None,
        runtime_mode: str,
        state_store: StateStore,
        pnl_engine: Optional[PnLEngine] = None,
        position_ownership_store: Optional[PositionOwnershipStore] = None,
        poll_interval_seconds: float = 15.0,
    ) -> None:
        self._tenant_id = tenant_id
        self._broker_account = broker_account
        self._broker_client = broker_client
        self._stability_flags = load_stability_feature_flags()
        self._runtime_mode = runtime_mode
        self._shadow_mode = str(runtime_mode or "").upper() == "SHADOW"
        execution_broker_type = (
            "shadow"
            if self._shadow_mode
            else str(getattr(broker_account, "broker_type", "") or "")
        )
        self._execution_port = execution_port or create_execution_port(
            broker_client=broker_client,
            broker_type=execution_broker_type,
        )
        self._state_store = state_store
        self._pnl_engine = pnl_engine
        self._position_ownership_store = position_ownership_store
        self._poll_interval = max(1.0, float(poll_interval_seconds))
        self._shadow_heartbeat_interval_seconds = max(
            5.0,
            float(os.getenv("SHADOW_HEARTBEAT_SECONDS", "60")),
        )
        self._last_shadow_heartbeat_ts = 0.0
        # Positions sync interval gate (separate from runner loop poll interval)
        self._positions_interval = max(
            5.0,
            float(os.getenv("POSITION_SYNC_INTERVAL_SECONDS", "30")),
        )
        self._last_positions_sync_ts = 0.0
        # Orders sync interval gate (separate from runner loop poll interval)
        # Default to 90s to respect Angel API rate limits (60s base + buffer)
        self._orders_interval = max(
            5.0,
            float(os.getenv("ORDERS_SYNC_INTERVAL_SECONDS", "90")),
        )
        self._last_orders_sync_ts = 0.0

        self._broker_account_id: BrokerAccountId = broker_account.broker_account_id

        self._running: bool = False
        self._main_task: Optional[asyncio.Task[None]] = None

        self._last_balance: Optional[Balance] = None
        self._last_positions: list = []
        self._last_orders: list[OrderStatus] = []
        self._positions_registry_released = False
        self._register_positions_key()

        # §106: Balance sync readiness tracking.
        # has_ever_synced_balance is set True on the first successful balance fetch.
        # Used by /readyz to gate readiness in LIVE (no false-green when RMS is down).
        self._has_ever_synced_balance: bool = False
        # §116: Consecutive failure tracking for structured alert emission.
        self._consecutive_balance_failures: int = 0
        self._balance_alert_threshold: int = max(
            1,
            int(os.getenv("BALANCE_SYNC_ALERT_THRESHOLD", "3")),
        )

    # ---------- Properties ----------
    # Return the tenant id for this runner.
    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    # Return the broker account id for this runner.
    @property
    def broker_account_id(self) -> BrokerAccountId:
        return self._broker_account.broker_account_id

    # Return the runtime mode (PAPER/LIVE/SHADOW) for this runner.
    @property
    def runtime_mode(self) -> str:
        return self._runtime_mode

    # Return True if the runner loop is active.
    @property
    def is_running(self) -> bool:
        """Return True if the runner main loop is currently active."""
        return self._running

    @property
    def has_ever_synced_balance(self) -> bool:
        """True once at least one successful balance fetch has completed. §106"""
        return self._has_ever_synced_balance

    # Build a key used for shared position sync locks/backoff.
    def _positions_key(self) -> tuple[str, BrokerAccountId]:
        broker_name = str(self._broker_account.broker_type or "").lower()
        if not broker_name:
            broker_name = type(self._broker_client).__name__.lower()
        return broker_name, self._broker_account_id

    # Get or create the shared positions lock for this broker.
    def _positions_lock(self) -> asyncio.Lock:
        key = self._positions_key()
        with _POSITIONS_REGISTRY_LOCK:
            lock = _POSITIONS_LOCKS.get(key)
            if lock is None:
                lock = asyncio.Lock()
                _POSITIONS_LOCKS[key] = lock
            return lock

    # Get or create the shared positions backoff state.
    def _positions_backoff(self) -> BackoffState:
        key = self._positions_key()
        with _POSITIONS_REGISTRY_LOCK:
            state = _POSITIONS_BACKOFF.get(key)
            if state is None:
                state = BackoffState()
                _POSITIONS_BACKOFF[key] = state
            return state

    def _register_positions_key(self) -> None:
        key = self._positions_key()
        with _POSITIONS_REGISTRY_LOCK:
            _POSITIONS_REFCOUNTS[key] = _POSITIONS_REFCOUNTS.get(key, 0) + 1
            if key not in _POSITIONS_LOCKS:
                _POSITIONS_LOCKS[key] = asyncio.Lock()
            if key not in _POSITIONS_BACKOFF:
                _POSITIONS_BACKOFF[key] = BackoffState()

    def _release_positions_key(self) -> None:
        if self._positions_registry_released:
            return
        self._positions_registry_released = True
        if not _positions_cleanup_enabled():
            return
        key = self._positions_key()
        with _POSITIONS_REGISTRY_LOCK:
            current = _POSITIONS_REFCOUNTS.get(key, 0)
            if current <= 1:
                _POSITIONS_REFCOUNTS.pop(key, None)
                _POSITIONS_LOCKS.pop(key, None)
                _POSITIONS_BACKOFF.pop(key, None)
            else:
                _POSITIONS_REFCOUNTS[key] = current - 1

    # ---------- Lifecycle ----------
    # Start the runner main loop.
    async def start(self) -> None:
        if self._running:
            return
        try:
            await self._broker_client.login()
        except Exception as exc:
            logger.error(
                "AccountRunner login failed for broker_account_id=%s: %s",
                self.broker_account_id,
                exc,
            )
            self._running = False
            return

        self._running = True
        logger.info(
            "AccountRunner started for tenant_id=%s broker_account_id=%s mode=%s",
            self.tenant_id,
            self.broker_account_id,
            self.runtime_mode,
        )
        await self.refresh_state(force=True)

        loop = asyncio.get_running_loop()
        self._main_task = loop.create_task(
            self._run(), name=f"account-runner-{self.broker_account_id}"
        )

    # Stop the runner and cancel the main task.
    async def stop(self) -> None:
        if self._running:
            self._running = False
            task = self._main_task
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(
                        "AccountRunner task cancelled for %s", self.broker_account_id
                    )
            self._main_task = None
        self._log_shadow_summary(reason="runner_stop")
        self._release_positions_key()

    # ---------- Internal loop ----------
    # Main loop: login, then periodically sync balance/positions/orders.
    async def _run(self) -> None:
        try:
            while self._running:
                # §79: Proactively refresh the broker auth token before the
                # daily 00:00 IST reset boundary (default: 10 min before).
                # This prevents the position-sync degradation that occurs
                # when a stale token is detected only after auth errors fire.
                proactive_relogin = getattr(
                    self._broker_client, "proactive_relogin_if_near_expiry", None
                )
                if callable(proactive_relogin):
                    try:
                        await proactive_relogin(margin_minutes=10)
                    except Exception:
                        pass  # logged inside; reactive relogin still available
                await self._sync_balance()
                await self._sync_positions()
                now = time.monotonic()
                if now - self._last_orders_sync_ts >= self._orders_interval:
                    await self._sync_orders()
                    self._last_orders_sync_ts = time.monotonic()
                self._maybe_emit_shadow_heartbeat(time.monotonic())
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.debug("AccountRunner loop cancelled for %s", self.broker_account_id)
        except Exception as exc:
            logger.exception(
                "AccountRunner loop error for broker_account_id=%s: %s",
                self.broker_account_id,
                exc,
            )
        finally:
            self._running = False

    async def refresh_state(self, *, force: bool = False) -> None:
        await self._sync_balance()
        await self._sync_positions(force=force)
        await self._sync_orders(force=force)

    # Sync balance into state store.
    async def _sync_balance(self) -> None:
        try:
            balance = await self._broker_client.get_balance()
            self._last_balance = balance
            self._state_store.set_balance(self._broker_account_id, balance)
            # §106: Mark first-ever success so /readyz can gate on it.
            if not self._has_ever_synced_balance:
                self._has_ever_synced_balance = True
                logger.info(
                    "balance_sync.first_success: broker_account_id=%s",
                    self.broker_account_id,
                )
            # §116: Reset consecutive failure counter on success.
            if self._consecutive_balance_failures > 0:
                prev = self._consecutive_balance_failures
                self._consecutive_balance_failures = 0
                # Audit emission is a side-effect, not a correctness invariant;
                # never let it propagate back into the balance try/except and
                # poison the recovery path into a self-perpetuating loop.
                try:
                    emit_audit_event(
                        actor="system",
                        action="balance_sync.recovered",
                        resource_type="broker_account",
                        resource_id=self.broker_account_id,
                        metadata={
                            "tenant_id": self._tenant_id,
                            "previous_consecutive_failures": prev,
                        },
                    )
                except Exception:
                    logger.warning(
                        "balance_sync.recovered audit emit failed for %s",
                        self.broker_account_id,
                        exc_info=True,
                    )
        except Exception as exc:
            self._consecutive_balance_failures += 1
            failures = self._consecutive_balance_failures
            # §116: Escalate to ERROR and emit structured alert after threshold.
            if failures >= self._balance_alert_threshold:
                logger.error(
                    "balance_sync.persistent_failure broker_account_id=%s "
                    "consecutive_failures=%d err=%s",
                    self.broker_account_id,
                    failures,
                    exc,
                    exc_info=True,
                )
                try:
                    emit_audit_event(
                        actor="system",
                        action="balance_sync.persistent_failure",
                        resource_type="broker_account",
                        resource_id=self.broker_account_id,
                        metadata={
                            "tenant_id": self._tenant_id,
                            "consecutive_failures": failures,
                            "last_error": str(exc),
                        },
                    )
                except Exception:
                    logger.warning(
                        "balance_sync.persistent_failure audit emit failed for %s",
                        self.broker_account_id,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "AccountRunner balance sync failed for %s: %s",
                    self.broker_account_id,
                    exc,
                    exc_info=True,
                )

    # Sync positions into state store with backoff handling.
    async def _sync_positions(self, *, force: bool = False) -> None:
        backoff = self._positions_backoff()
        now = time.monotonic()
        if (not force) and (now - self._last_positions_sync_ts < self._positions_interval):
            return

        now = time.monotonic()
        if (not force) and (not backoff.can_attempt(now)):
            return
        lock = self._positions_lock()
        async with lock:
            now = time.monotonic()
            if (not force) and (not backoff.can_attempt(now)):
                return
            self._last_positions_sync_ts = time.monotonic()
            result = await self._broker_client.get_positions()

        if not isinstance(result, PositionsFetchResult):
            result = PositionsFetchResult(
                status=PositionsStatus.OK,
                positions=result,
                reason="ok",
            )

        if result.status == PositionsStatus.OK:
            backoff.register_ok()
            positions = result.positions or []
            self._last_positions = positions
            self._state_store.set_positions(self._broker_account_id, positions)
            if self._position_ownership_store is not None:
                try:
                    reconcile_result = self._position_ownership_store.reconcile_broker_positions(
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        positions=list(positions),
                    )
                    if reconcile_result.changed:
                        logger.warning(
                            "AccountRunner ownership reconcile for %s: added_unknown=%d converted_to_unknown=%d removed_stale=%d parse_errors=%d",
                            self.broker_account_id,
                            reconcile_result.added_unknown,
                            reconcile_result.converted_to_unknown,
                            reconcile_result.removed_stale,
                            reconcile_result.parse_errors,
                        )
                        if int(reconcile_result.converted_to_unknown or 0) > 0:
                            log_event(
                                logger,
                                event_type="OWNERSHIP_CONVERTED_TO_UNKNOWN",
                                message="Ownership reconcile converted live broker positions to UNKNOWN.",
                                tenant_id=self._tenant_id,
                                broker_account_id=self._broker_account_id,
                                level=logging.WARNING,
                                converted_to_unknown=reconcile_result.converted_to_unknown,
                                added_unknown=reconcile_result.added_unknown,
                                removed_stale=reconcile_result.removed_stale,
                                parse_errors=reconcile_result.parse_errors,
                            )
                except Exception as exc:
                    logger.warning(
                        "AccountRunner ownership reconcile failed for %s: %s",
                        self.broker_account_id,
                        exc,
                    )
            if self._pnl_engine is not None:
                try:
                    self._sync_pnl_from_positions(positions)
                except Exception as exc:
                    logger.warning(
                        "AccountRunner PnL sync failed for %s: %s",
                        self.broker_account_id,
                        exc,
                    )
            self._state_store.update_positions_status(
                self._broker_account_id,
                status="OK",
                last_ok_ts=datetime.now(timezone.utc).isoformat(),
                last_count=len(positions),
                error_reason=None,
                blocked_ts=None,
                retry_after_seconds=None,
            )
            return

        if result.status == PositionsStatus.BLOCKED:
            delay = backoff.register_blocked(
                result.reason,
                retry_after_seconds=result.retry_after_seconds,
            )
            self._state_store.update_positions_status(
                self._broker_account_id,
                status="BLOCKED",
                last_ok_ts=None,
                last_count=None,
                error_reason=result.reason,
                blocked_ts=datetime.now(timezone.utc).isoformat(),
                retry_after_seconds=delay,
            )
            logger.warning(
                "AccountRunner positions sync blocked for %s: reason=%s http_status=%s retry_after=%.1fs snippet=%s",
                self.broker_account_id,
                result.reason,
                result.http_status,
                delay,
                result.raw_snippet or "-",
            )
            return

        self._state_store.update_positions_status(
            self._broker_account_id,
            status="ERROR",
            last_ok_ts=None,
            last_count=None,
            error_reason=result.reason,
            blocked_ts=None,
            retry_after_seconds=None,
        )
        logger.warning(
            "AccountRunner positions sync failed for %s: reason=%s",
            self.broker_account_id,
            result.reason,
        )

    def _sync_pnl_from_positions(self, positions: list[Any]) -> None:
        if self._pnl_engine is None:
            return

        account_unrealized_pnl = 0.0
        account_gross_exposure = 0.0
        per_strategy_marks: dict[StrategyId, tuple[float, float]] = {}
        get_owner = getattr(self._position_ownership_store, "get_owner", None)
        _live_mode = str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper() == "LIVE"
        _synthetic_mark_count = 0

        for pos in positions or []:
            qty = int(getattr(pos, "quantity", 0) or 0)
            if qty == 0:
                continue
            symbol = str(getattr(pos, "symbol", "") or "").strip()
            avg_price = float(getattr(pos, "avg_price", 0.0) or 0.0)
            ltp = dashboard_bus.get_last_price(symbol) if symbol else None

            # §94: In LIVE mode, do NOT use avg_price as a synthetic mark.
            # Using avg_price when ltp is None produces unrealized_pnl=0 which
            # makes a losing position appear flat and misleads risk/PnL decisions.
            # Skip the PnL contribution for positions without a live mark; use
            # avg_price only for gross_exposure (conservative capital accounting).
            if ltp is None:
                _synthetic_mark_count += 1
                if _live_mode:
                    logger.warning(
                        "mark.unavailable: no live LTP for symbol=%s qty=%d "
                        "broker_account=%s — skipping unrealized PnL contribution "
                        "for this position in LIVE mode",
                        symbol, qty, self._broker_account_id,
                    )
                    gross_exposure = abs(avg_price * qty)
                    account_gross_exposure += gross_exposure
                    continue  # skip PnL — we cannot compute it correctly
                # Non-LIVE: retain legacy avg_price fallback behaviour.
                mark = avg_price
            else:
                mark = float(ltp)

            gross_exposure = abs(mark * qty)
            unrealized_pnl = (mark - avg_price) * qty

            account_unrealized_pnl += unrealized_pnl
            account_gross_exposure += gross_exposure

            owner_strategy_id: Optional[StrategyId] = None
            if callable(get_owner):
                contract_key, _ = derive_contract_key_from_position(pos)
                if contract_key is not None:
                    owner = get_owner(
                        tenant_id=self._tenant_id,
                        broker_account_id=self._broker_account_id,
                        contract_key=contract_key,
                    )
                    owner_text = str(owner or "").strip()
                    if owner_text and owner_text not in {
                        UNKNOWN_OWNER,
                        UNKNOWN_STRATEGY_ID,
                    }:
                        owner_strategy_id = StrategyId(owner_text)

            if owner_strategy_id is None:
                continue

            current_unrealized, current_exposure = per_strategy_marks.get(
                owner_strategy_id,
                (0.0, 0.0),
            )
            per_strategy_marks[owner_strategy_id] = (
                float(current_unrealized) + unrealized_pnl,
                float(current_exposure) + gross_exposure,
            )

            # Refresh control_open_pnl for short positions on every broker sync.
            # qty < 0 means the broker reports a short leg; mark is the LTP or avg fallback.
            # For long or flat positions control_open_qty is 0, so this is a harmless no-op.
            if qty < 0:
                _update_fn = getattr(self._pnl_engine, "update_control_open_pnl", None)
                if callable(_update_fn):
                    try:
                        _update_fn(
                            tenant_id=self._tenant_id,
                            broker_account_id=self._broker_account_id,
                            strategy_id=owner_strategy_id,
                            current_ltp=mark,
                        )
                    except Exception:
                        pass  # non-critical; control PnL update is best-effort

        _sync_source = (
            "broker_sync_stale_mark" if _synthetic_mark_count > 0 else "broker_sync"
        )
        self._pnl_engine.sync_account_mark_to_market(
            tenant_id=self._tenant_id,
            broker_account_id=self._broker_account_id,
            account_unrealized_pnl=account_unrealized_pnl,
            account_gross_exposure=account_gross_exposure,
            per_strategy_marks=per_strategy_marks,
            as_of=datetime.now(timezone.utc),
            source=_sync_source,
        )

    # Sync orders into state store.
    async def _sync_orders(self, *, force: bool = False) -> None:
        try:
            if not force:
                now = time.monotonic()
                if now - self._last_orders_sync_ts < self._orders_interval:
                    return
            orders = await self._broker_client.get_orders()
            self._last_orders = orders
            self._last_orders_sync_ts = time.monotonic()
            self._state_store.set_order_snapshot(self._broker_account_id, orders)
            self._state_store.update_orders_ok_ts(
                self._broker_account_id,
                last_ok_ts=datetime.now(timezone.utc).isoformat(),
            )
            active_orders = self._derive_active_orders(orders)
            self._state_store.set_orders(self._broker_account_id, active_orders)
            log_event(
                logger,
                event_type="ACCOUNT_RUNNER_ORDERS_SYNC",
                message="Persisted broker order snapshot into state store.",
                tenant_id=self._tenant_id,
                broker_account_id=self._broker_account_id,
                level=logging.DEBUG,
                preserve_terminal_mode=self._stability_flags.order_snapshot_preserve_terminal,
                snapshot_count=len(orders),
                active_count=len(active_orders),
            )
        except Exception as exc:
            logger.warning(
                "AccountRunner orders sync failed for %s: %s",
                self.broker_account_id,
                exc,
            )

    @staticmethod
    def _derive_active_orders(orders: list[OrderStatus]) -> list[OrderStatus]:
        active_orders: list[OrderStatus] = []
        for order in orders:
            status = str(getattr(order, "status", "") or "").strip().upper()
            if status in _TERMINAL_ORDER_STATUSES:
                continue
            active_orders.append(order)
        return active_orders

    # Cancel open intraday orders (used by EOD strict cleanup).
    async def cancel_open_intraday_orders(
        self,
        *,
        statuses: Optional[set[str]] = None,
        exclude_exchanges: Optional[set[str]] = None,
        refresh: bool = True,
        reason_tag: str = "EOD_CANCEL_OPEN_ORDERS",
    ) -> dict[str, int]:
        def _field(order: Any, key: str, default: Any = None) -> Any:
            if isinstance(order, dict):
                return order.get(key, default)
            return getattr(order, key, default)

        target_statuses = {
            str(s or "").strip().upper()
            for s in (statuses or _OPEN_ORDER_STATUSES)
            if str(s or "").strip()
        }
        exclude_set = {
            str(s or "").strip().upper()
            for s in (exclude_exchanges or set())
            if str(s or "").strip()
        }

        orders: list[Any] = []
        if refresh:
            try:
                orders = list(await self._broker_client.get_orders())
                self._last_orders = orders  # type: ignore[assignment]
                self._state_store.set_orders(self._broker_account_id, orders)  # type: ignore[arg-type]
            except Exception as exc:
                logger.warning(
                    "AccountRunner order refresh failed before cancel pass for %s: %s",
                    self.broker_account_id,
                    exc,
                )

        if not orders:
            orders = list(self._state_store.get_orders(self._broker_account_id) or [])

        attempted = 0
        cancelled = 0
        failed = 0
        unsupported = 0
        skipped = 0
        seen_ids: set[str] = set()

        for order in orders:
            oid = str(_field(order, "order_id", "") or "").strip()
            if not oid or oid in seen_ids:
                continue
            seen_ids.add(oid)

            status = str(_field(order, "status", "") or "").strip().upper()
            if target_statuses and status and status not in target_statuses:
                skipped += 1
                continue

            product_type = str(_field(order, "product_type", "") or "").strip().upper()
            if product_type and product_type != "INTRADAY":
                skipped += 1
                continue

            exchange = str(_field(order, "exchange", "") or "").strip().upper()
            if exchange and exclude_set:
                if any(exchange == ex or exchange.startswith(ex) for ex in exclude_set):
                    skipped += 1
                    continue

            symbol = str(_field(order, "symbol", "") or "").strip() or None
            variety = str(_field(order, "variety", "") or "").strip().upper() or "NORMAL"
            attempted += 1
            try:
                cancel_fn = getattr(self._broker_client, "cancel_order", None)
                if callable(cancel_fn):
                    resp = await cancel_fn(oid, symbol=symbol, variety=variety)
                else:
                    resp = await self._execution_port.cancel(oid, symbol=symbol)
            except Exception as exc:
                failed += 1
                logger.warning(
                    "AccountRunner cancel failed for %s order_id=%s reason=%s err=%s",
                    self.broker_account_id,
                    oid,
                    reason_tag,
                    exc,
                )
                continue

            resp_status = str(getattr(resp, "status", "") or "").strip().upper()
            if resp_status in {"CANCELLED", "CANCELED", "SUCCESS", "OK"}:
                cancelled += 1
            elif resp_status == "UNSUPPORTED":
                unsupported += 1
            else:
                failed += 1
                logger.warning(
                    "AccountRunner cancel non-terminal response for %s order_id=%s status=%s message=%s reason=%s",
                    self.broker_account_id,
                    oid,
                    getattr(resp, "status", None),
                    getattr(resp, "message", None),
                    reason_tag,
                )

        if attempted or skipped:
            logger.info(
                "AccountRunner open-order cancel summary for %s | reason=%s attempted=%d cancelled=%d failed=%d unsupported=%d skipped=%d",
                self.broker_account_id,
                reason_tag,
                attempted,
                cancelled,
                failed,
                unsupported,
                skipped,
            )
        return {
            "attempted": attempted,
            "cancelled": cancelled,
            "failed": failed,
            "unsupported": unsupported,
            "skipped": skipped,
        }

    # Placeholder for tick handling (when wired to market data streams)
    # Handle tick updates (currently a stub).
    async def on_tick(self, symbol: str, price: float) -> None:  # noqa: D401
        """Handle price ticks (stub for future wiring)."""
        return

    # Forward an order to the broker client and persist the response.
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """
        Forward an order to the configured execution adapter.

        Assumes the broker client has been logged in by the runner's startup
        sequence. This method does not perform risk/capital checks itself;
        those are handled by OrderRouter.
        """
        response = await self._execution_port.place(order)
        self._state_store.set_last_order_response(self._broker_account_id, response)
        return response

    def _maybe_emit_shadow_heartbeat(self, now: float) -> None:
        if not self._shadow_mode:
            return
        if (now - self._last_shadow_heartbeat_ts) < self._shadow_heartbeat_interval_seconds:
            return
        self._last_shadow_heartbeat_ts = now

        positions = list(self._state_store.get_positions(self._broker_account_id) or [])
        gross_exposure = 0.0
        unrealized_pnl = 0.0
        for pos in positions:
            qty = int(getattr(pos, "quantity", 0) or 0)
            if qty == 0:
                continue
            avg_price = float(getattr(pos, "avg_price", 0.0) or 0.0)
            symbol = str(getattr(pos, "symbol", "") or "").strip()
            ltp = dashboard_bus.get_last_price(symbol) if symbol else None
            mark = float(ltp if ltp is not None else avg_price)
            gross_exposure += abs(mark * qty)
            unrealized_pnl += (mark - avg_price) * qty

        metrics_getter = getattr(self._broker_client, "shadow_metrics_snapshot", None)
        metrics = metrics_getter() if callable(metrics_getter) else {}
        logger.info(
            "SHADOW_HEARTBEAT tenant_id=%s broker_account_id=%s execution_mode=SHADOW broker_call_blocked=true positions=%d gross_exposure=%.2f unrealized_pnl=%.2f net_realized_pnl=%.2f orders_submitted=%s orders_filled=%s orders_rejected=%s",
            self.tenant_id,
            self.broker_account_id,
            len([p for p in positions if int(getattr(p, 'quantity', 0) or 0) != 0]),
            gross_exposure,
            unrealized_pnl,
            float(metrics.get("net_realized_pnl", 0.0) or 0.0),
            metrics.get("orders_submitted", 0),
            metrics.get("orders_filled", 0),
            metrics.get("orders_rejected", 0),
        )

    def _log_shadow_summary(self, *, reason: str) -> None:
        if not self._shadow_mode:
            return
        summary_getter = getattr(self._broker_client, "shadow_summary", None)
        summary = summary_getter() if callable(summary_getter) else {}
        logger.info(
            "SHADOW_RUN_SUMMARY tenant_id=%s broker_account_id=%s reason=%s execution_mode=SHADOW broker_call_blocked=true orders_submitted=%s orders_filled=%s orders_rejected=%s fills_full=%s fills_partial=%s gross_realized_pnl=%.2f fees_total=%.2f net_realized_pnl=%.2f open_positions=%s",
            self.tenant_id,
            self.broker_account_id,
            reason,
            summary.get("orders_submitted", 0),
            summary.get("orders_filled", 0),
            summary.get("orders_rejected", 0),
            summary.get("fills_full", 0),
            summary.get("fills_partial", 0),
            float(summary.get("gross_realized_pnl", 0.0) or 0.0),
            float(summary.get("fees_total", 0.0) or 0.0),
            float(summary.get("net_realized_pnl", 0.0) or 0.0),
            summary.get("open_positions", 0),
        )

    def __del__(self) -> None:
        try:
            self._release_positions_key()
        except Exception:
            return


__all__ = ["AccountRunner"]
