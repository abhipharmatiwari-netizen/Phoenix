"""
Hub controller for AccountRunners (§4 Execution Hub).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Set
from app.brokers.factory import create_broker_client
from app.config.settings import get_settings
from app.core.identifiers import BrokerAccountId
from app.core.logging_utils import log_event
from app.data.state_store import StateStore
from app.hub.account_runner import AccountRunner
from app.orders.position_ownership import PositionOwnershipStore
from app.pnl.pnl_engine import PnLEngine
from app.tenants.firestore_client import (
    get_all_active_tenants,
    get_enabled_broker_accounts,
    get_all_subscriptions,
    get_broker_account,
    upsert_broker_account,
)
from app.tenants.models import SubscriptionModel, BrokerAccountModel
from app.tenants.subscription_service import (
    compute_account_runtime_mode,
    is_subscription_active,
)

logger = logging.getLogger(__name__)
_CONTROL_PLANE_BACKOFF_BASE_SECONDS = 5.0
_CONTROL_PLANE_BACKOFF_MAX_SECONDS = 300.0

# Aliases for testing purposes
get_active_broker_accounts = get_enabled_broker_accounts


# Return subscriptions filtered for a broker account id.
def get_subscriptions_for_account(broker_account_id):
    """Helper function to get subscriptions for a specific account from all subscriptions."""
    return [sub for sub in get_all_subscriptions() if sub.broker_account_id == broker_account_id]


# Find a tenant by id from the active tenants list.
def get_tenant(tenant_id):
    """Helper function to get a specific tenant from all active tenants."""
    tenants = get_all_active_tenants()
    for tenant in tenants:
        if tenant.tenant_id == tenant_id:
            return tenant
    return None


# Pick the active subscription with the latest start time.
def pick_active_subscription(
    subscriptions: list[SubscriptionModel],
) -> Optional[SubscriptionModel]:
    active = [sub for sub in subscriptions if is_subscription_active(sub)]
    if not active:
        return None
    # Pick the one with latest start time
    return sorted(active, key=lambda s: s.start_at, reverse=True)[0]





class Hub:
    # Initialize hub state, runner tracking, and watchdog tasks.
    def __init__(
        self,
        state_store: Optional[StateStore] = None,
        *,
        pnl_engine: Optional[PnLEngine] = None,
        position_ownership_store: Optional[PositionOwnershipStore] = None,
    ) -> None:
        self._settings = get_settings()
        self._reconcile_verbose = bool(
            getattr(self._settings, "hub_reconcile_verbose", False)
        )
        self._state_store = state_store or StateStore()
        self._pnl_engine = pnl_engine
        self._position_ownership_store = position_ownership_store
        self._account_runners: Dict[BrokerAccountId, AccountRunner] = {}
        self._runner_tasks: Dict[BrokerAccountId, asyncio.Task[None]] = {}
        self._subscription_watchdog_task: Optional[asyncio.Task[None]] = None
        self._profit_watchdog_task: Optional[asyncio.Task[None]] = None
        self._initialized: bool = False
        self._running: bool = False
        self._last_subscription_refresh: Optional[datetime] = None
        self._control_plane_probe_failures: int = 0
        self._control_plane_next_probe_ts: float = 0.0
        self._subscription_watchdog_failures: int = 0
        self._subscription_watchdog_next_ts: float = 0.0

    # Expose the shared state store.
    @property
    def state_store(self) -> StateStore:
        return self._state_store

    # Return the last time subscriptions were refreshed.
    @property
    def last_subscription_refresh(self) -> Optional[datetime]:
        return self._last_subscription_refresh

    # Return the number of registered account runners.
    @property
    def runner_count(self) -> int:
        return self.registered_runner_count

    # Return the number of registered account runners.
    @property
    def registered_runner_count(self) -> int:
        return len(self._account_runners)

    # Return the number of runners that successfully started and are actively running.
    @property
    def running_runner_count(self) -> int:
        return sum(1 for r in self._account_runners.values() if r.is_running)

    # Return the number of runners that failed to start (registered but not running).
    @property
    def failed_runner_count(self) -> int:
        return sum(1 for r in self._account_runners.values() if not r.is_running)

    # List broker account ids that have runners.
    def list_runner_ids(self) -> list[BrokerAccountId]:
        return list(self._account_runners.keys())

    # Get the runner for a specific broker account id.
    def get_runner(self, broker_account_id: BrokerAccountId) -> Optional[AccountRunner]:
        return self._account_runners.get(broker_account_id)

    async def wait_for_runner_startup(self) -> None:
        startup_tasks = [task for task in self._runner_tasks.values() if task is not None]
        if not startup_tasks:
            return
        await asyncio.gather(*startup_tasks)

    def _uses_postgres_control_plane(self) -> bool:
        return (
            str(getattr(self._settings, "control_plane_backend", "firestore")).lower()
            == "postgres"
        )

    @staticmethod
    def _backoff_seconds(
        failures: int,
        *,
        base_seconds: float,
        max_seconds: float,
    ) -> float:
        return min(
            float(max_seconds),
            float(base_seconds) * (2 ** max(0, int(failures) - 1)),
        )

    @staticmethod
    def _is_control_plane_outage_error(error_text: str) -> bool:
        low = str(error_text or "").lower()
        markers = (
            "connection timeout expired",
            "connection refused",
            "could not connect",
            "psycopg",
            "postgres",
            "control-plane",
        )
        return any(marker in low for marker in markers)

    def _register_control_plane_failure(self, error_text: str) -> float:
        self._control_plane_probe_failures += 1
        backoff_seconds = self._backoff_seconds(
            self._control_plane_probe_failures,
            base_seconds=_CONTROL_PLANE_BACKOFF_BASE_SECONDS,
            max_seconds=_CONTROL_PLANE_BACKOFF_MAX_SECONDS,
        )
        self._control_plane_next_probe_ts = max(
            self._control_plane_next_probe_ts,
            time.monotonic() + backoff_seconds,
        )
        return backoff_seconds

    def _probe_control_plane_sync(self) -> Optional[str]:
        if not self._uses_postgres_control_plane():
            return None
        try:
            from app.data.postgres import connect_with_retry, get_control_plane_dsn

            dsn = get_control_plane_dsn(self._settings)
            conn = connect_with_retry(
                dsn,
                autocommit=True,
                max_attempts=1,
                base_backoff_seconds=0.0,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return None
        except Exception as exc:
            return repr(exc)

    async def _control_plane_ready(self) -> bool:
        if not self._uses_postgres_control_plane():
            return True
        now_mono = time.monotonic()
        if now_mono < self._control_plane_next_probe_ts:
            return False
        probe_error = await asyncio.to_thread(self._probe_control_plane_sync)
        if probe_error is None:
            if self._control_plane_probe_failures:
                logger.info(
                    "Control-plane Postgres probe recovered after %d failure(s).",
                    self._control_plane_probe_failures,
                )
            self._control_plane_probe_failures = 0
            self._control_plane_next_probe_ts = 0.0
            return True
        backoff_seconds = self._register_control_plane_failure(probe_error)
        log_event(
            logger,
            event_type="CONTROL_PLANE_NOT_READY",
            message="Control-plane Postgres unavailable; skipping reconcile cycle.",
            level=logging.WARNING,
            error=probe_error,
            failures=self._control_plane_probe_failures,
            retry_after=f"{backoff_seconds:.1f}s",
        )
        return False

    # Start a runner task if it is not already running.
    async def _start_runner(
        self, broker_account_id: BrokerAccountId, runner: AccountRunner
    ) -> None:
        """
        Ensure the given AccountRunner is running.

        If a task already exists and the runner is marked running, this is a no-op.
        Otherwise it will create a new task calling runner.start().
        """
        if runner.is_running:
            return

        loop = asyncio.get_running_loop()
        task = loop.create_task(
            runner.start(),
            name=f"account-runner-{broker_account_id}",
        )
        self._runner_tasks[broker_account_id] = task

    # Stop a runner and clean up its task.
    async def _stop_runner(self, broker_account_id: BrokerAccountId) -> None:
        """
        Request a graceful stop for the AccountRunner associated with the given id.
        """
        runner = self._account_runners.get(broker_account_id)
        if runner is None:
            self._runner_tasks.pop(broker_account_id, None)
            return

        if not runner.is_running:
            self._runner_tasks.pop(broker_account_id, None)
            return

        try:
            await runner.stop()
        finally:
            task = self._runner_tasks.pop(broker_account_id, None)
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(
                        "Runner task cancelled for broker_account_id=%s",
                        broker_account_id,
                    )

    # Reconcile runner set with the control-plane state.
    async def _reconcile_runners_once(self) -> None:
        """
        Reconcile AccountRunners with current Firestore control-plane state.

        For each broker account:
        - Determine runtime_mode (DISABLED/PAPER/LIVE/SHADOW).
        - Ensure an AccountRunner exists for active modes.
        - Start it if the hub is running and the runner is not.

        For any existing runners whose account is no longer active or has become
        DISABLED, stop and remove them.
        """
        if not await self._control_plane_ready():
            return

        active_tenants = get_all_active_tenants()
        tenant_by_id = {t.tenant_id: t for t in active_tenants}

        broker_accounts = get_enabled_broker_accounts()
        all_subscriptions = list(get_all_subscriptions())
        subs_by_account: Dict[BrokerAccountId, list[SubscriptionModel]] = {}
        for sub in all_subscriptions:
            subs_by_account.setdefault(sub.broker_account_id, []).append(sub)

        log_event(
            logger,
            event_type="ACTIVE_TENANTS_SUMMARY",
            message="Active tenants discovered during reconcile.",
            level=logging.DEBUG,
            active_tenant_count=len(active_tenants),
            tenant_ids=",".join(sorted({str(t.tenant_id) for t in active_tenants})),
        )
        
        # Enhanced diagnostics - Log subscription details BEFORE processing
        if self._reconcile_verbose:
            logger.info(
                "[RECONCILE] Starting with %d enabled broker accounts, %d total subscriptions",
                len(broker_accounts),
                len(all_subscriptions),
            )
            logger.info(
                "[RECONCILE] Active tenants: %s",
                ",".join(sorted(str(t.tenant_id) for t in active_tenants)),
            )
        
        # Log subscription details for debugging
        for sub in all_subscriptions:
            is_active = is_subscription_active(sub)
            if self._reconcile_verbose:
                logger.info(
                    "[RECONCILE] Subscription: %s@%s mode=%s start=%s end=%s is_active=%s",
                    sub.broker_account_id,
                    sub.tenant_id,
                    sub.mode,
                    sub.start_at,
                    sub.end_at,
                    is_active,
                )
        
        for acct in broker_accounts:
            acct_subs = subs_by_account.get(acct.broker_account_id, [])
            if self._reconcile_verbose:
                logger.info(
                    "[RECONCILE] Broker account: %s (tenant=%s, enabled=%s, subs=%d)",
                    acct.broker_account_id,
                    acct.tenant_id,
                    acct.enabled,
                    len(acct_subs),
                )
        
        # Validate subscription broker_account_id format
        for sub in all_subscriptions:
            # Check if subscription broker_account_id matches any broker account
            matching_account = None
            for acct in broker_accounts:
                if acct.broker_account_id == sub.broker_account_id:
                    matching_account = acct
                    break
            if matching_account is None and self._reconcile_verbose:
                logger.warning(
                    "[RECONCILE] ⚠️  SUBSCRIPTION MISMATCH: Subscription has broker_account_id=%s but "
                    "NO broker account exists with this ID. Available accounts: %s",
                    sub.broker_account_id,
                    ",".join(str(a.broker_account_id) for a in broker_accounts),
                )
        
        desired_ids: Set[BrokerAccountId] = set()

        for account in broker_accounts:
            tenant = tenant_by_id.get(account.tenant_id)
            
            # Check tenant status FIRST
            if tenant is None:
                logger.warning(
                    "[RECONCILE] ⚠️ SKIPPED %s: tenant_id=%s is NOT in active tenants list. "
                    "Active tenants: %s",
                    account.broker_account_id,
                    account.tenant_id,
                    ",".join(sorted(str(t.tenant_id) for t in active_tenants)),
                )
                continue

            # If we reach here, tenant IS active
            subscriptions = subs_by_account.get(account.broker_account_id, [])
            active_sub = pick_active_subscription(subscriptions)
            mode = compute_account_runtime_mode(account, active_sub)

            if mode == "DISABLED":
                # Provide UNAMBIGUOUS diagnostics - only one reason
                if not account.enabled:
                    reason = "disabled_flag"
                    logger.warning(
                        "[RECONCILE] ⚠️ DISABLED %s@%s: Account explicitly disabled (broker_account.enabled=False)",
                        account.broker_account_id,
                        account.tenant_id,
                    )
                elif not subscriptions:
                    reason = "no_subscription"
                    logger.warning(
                        "[RECONCILE] ⚠️ DISABLED %s@%s: No subscriptions found for this account",
                        account.broker_account_id,
                        account.tenant_id,
                    )
                elif not any(is_subscription_active(sub) for sub in subscriptions):
                    reason = "subscription_inactive"
                    logger.warning(
                        "[RECONCILE] ⚠️ DISABLED %s@%s: %d subscriptions exist but NONE are active. "
                        "Details: %s",
                        account.broker_account_id,
                        account.tenant_id,
                        len(subscriptions),
                        "; ".join(
                            f"[{s.broker_account_id}] mode={s.mode} start={s.start_at} end={s.end_at}"
                            for s in subscriptions
                        ),
                    )
                else:
                    reason = "invalid_mode"
                    logger.warning(
                        "[RECONCILE] ⚠️ DISABLED %s@%s: Active subscription exists but mode is invalid. "
                        "Active sub mode: %s",
                        account.broker_account_id,
                        account.tenant_id,
                        active_sub.mode if active_sub else "None",
                    )
                continue

            # Account IS ENABLED - will create runner
            broker_account_id = account.broker_account_id
            desired_ids.add(broker_account_id)
            
            if self._reconcile_verbose:
                logger.info(
                    "[RECONCILE] ✓ ENABLED %s@%s: mode=%s (Active tenant, active subscription)",
                    account.broker_account_id,
                    account.tenant_id,
                    mode,
                )

            runner = self._account_runners.get(broker_account_id)
            if runner is None:
                broker_client = create_broker_client(
                    account=account,
                    runtime_mode=mode,
                )
                runner = AccountRunner(
                    tenant_id=tenant.tenant_id,
                    broker_account=account,
                    broker_client=broker_client,
                    runtime_mode=mode,
                    state_store=self._state_store,
                    pnl_engine=self._pnl_engine,
                    position_ownership_store=self._position_ownership_store,
                )
                self._account_runners[broker_account_id] = runner
                if self._reconcile_verbose:
                    logger.info(
                        "[RECONCILE] ✅ Runner CREATED: %s@%s (%s mode)",
                        broker_account_id,
                        tenant.tenant_id,
                        mode,
                    )
            else:
                # §104: Detect runtime mode changes on existing runners.
                # A runner created in PAPER mode must not silently continue
                # when the control plane promotes the account to LIVE, or vice
                # versa — the broker client and risk profile would be wrong.
                existing_mode = getattr(runner, "_runtime_mode", None) or getattr(
                    runner, "runtime_mode", None
                )
                if existing_mode is not None and str(existing_mode).upper() != mode.upper():
                    logger.error(
                        "[RECONCILE] runtime_mode_mismatch: runner %s@%s was created "
                        "in mode=%s but control-plane now says mode=%s. "
                        "Stopping stale runner — it will be recreated on next reconcile.",
                        broker_account_id,
                        tenant.tenant_id,
                        existing_mode,
                        mode,
                    )
                    try:
                        await runner.stop()
                    except Exception as _stop_exc:
                        logger.warning(
                            "[RECONCILE] Failed to stop stale mode runner %s: %s",
                            broker_account_id,
                            _stop_exc,
                        )
                    # Remove so next reconcile recreates with correct mode.
                    self._account_runners.pop(broker_account_id, None)
                    # Do NOT proceed to start — the next reconcile iteration
                    # will re-enter the runner-is-None branch and create fresh.
                    continue
                if self._reconcile_verbose:
                    logger.info(
                        "[RECONCILE] ℹ️ Runner EXISTS: %s@%s (%s mode, running=%s)",
                        broker_account_id,
                        tenant.tenant_id,
                        mode,
                        runner.is_running,
                    )

            if self._running and not runner.is_running:
                await self._start_runner(broker_account_id, runner)

        existing_ids = set(self._account_runners.keys())
        obsolete_ids = existing_ids - desired_ids
        for broker_account_id in obsolete_ids:
            if self._reconcile_verbose:
                logger.info(
                    "[RECONCILE] ❌ Stopping runner for %s (no longer active)",
                    broker_account_id,
                )
            await self._stop_runner(broker_account_id)
            self._account_runners.pop(broker_account_id, None)

        self._last_subscription_refresh = datetime.now(timezone.utc)
        
        # Summary log with clear breakdown
        enabled_count = len(desired_ids)
        existing_count = len(existing_ids)
        removed_count = len(obsolete_ids)
        total_accounts = len(broker_accounts)
        disabled_count = total_accounts - enabled_count
        
        if self._reconcile_verbose:
            logger.info(
                "[RECONCILE] ══════════════════════════════════════════════════════════",
            )
            logger.info(
                "[RECONCILE] RECONCILIATION COMPLETE",
            )
            logger.info(
                "[RECONCILE] Total broker accounts: %d",
                total_accounts,
            )
            logger.info(
                "[RECONCILE]   ✓ ENABLED (will have runners): %d",
                enabled_count,
            )
            logger.info(
                "[RECONCILE]   ⚠️  DISABLED (no runners): %d",
                disabled_count,
            )
            logger.info(
                "[RECONCILE] Runner status: desired=%d, existing=%d, removed=%d",
                enabled_count,
                existing_count,
                removed_count,
            )
            logger.info(
                "[RECONCILE] Active runner IDs: %s",
                ",".join(sorted(str(rid) for rid in desired_ids)) if desired_ids else "(none)",
            )
            logger.info(
                "[RECONCILE] ══════════════════════════════════════════════════════════",
            )
        
        log_event(
            logger,
            event_type="SUBSCRIPTION_RECONCILE",
            message="Reconciled account runners.",
            level=logging.INFO,
            runner_count=len(self._account_runners),
            desired_count=len(desired_ids),
            removed_count=len(obsolete_ids),
        )

    # Initialize hub state and run a first reconciliation.
    async def initialize(self) -> None:
        if self._initialized:
            logger.warning("Hub.initialize() called more than once; ignoring.")
            return

        logger.info("Hub.initialize(): reconciling initial account runners.")
        await self._reconcile_runners_once()
        self._initialized = True
        logger.info(
            "Hub.initialize(): %d AccountRunner instance(s) currently registered.",
            len(self._account_runners),
        )

    # Start all runners and watchdog loops.
    async def start_all(self) -> None:
        """
        Start all AccountRunners and begin subscription reconciliation.

        This method is idempotent: repeated calls while the hub is running
        are ignored.
        """
        if not self._initialized:
            await self.initialize()

        if self._running:
            logger.warning("Hub.start_all() called but hub is already running.")
            return

        self._running = True
        log_event(
            logger,
            event_type="HUB_START",
            message="Hub start_all invoked.",
            level=logging.INFO,
        )
        logger.info(
            "Hub.start_all(): hub marked as running; reconciling runners and starting subscription watchdog.",
        )

        await self._reconcile_runners_once()

        loop = asyncio.get_running_loop()
        if self._subscription_watchdog_task is None:
            self._subscription_watchdog_task = loop.create_task(
                self._subscription_watchdog_loop(),
                name="subscription-watchdog",
            )
        if self._profit_watchdog_task is None:
            self._profit_watchdog_task = loop.create_task(
                self._profit_watchdog_loop(),
                name="profit-watchdog",
            )

    # Stop all runners and background tasks.
    async def stop_all(self) -> None:
        """
        Request a graceful stop for all AccountRunners and background tasks.
        """
        logger.info("Hub.stop_all(): stopping all account runners and watchdog.")
        self._running = False
        log_event(
            logger,
            event_type="HUB_STOP",
            message="Hub stop_all invoked.",
            level=logging.INFO,
        )

        if self._subscription_watchdog_task:
            self._subscription_watchdog_task.cancel()
            try:
                await self._subscription_watchdog_task
            except asyncio.CancelledError:
                logger.debug("Subscription watchdog task cancelled.")
            finally:
                self._subscription_watchdog_task = None
        if self._profit_watchdog_task:
            self._profit_watchdog_task.cancel()
            try:
                await self._profit_watchdog_task
            except asyncio.CancelledError:
                logger.debug("Profit watchdog task cancelled.")
            finally:
                self._profit_watchdog_task = None

        for broker_account_id in list(self._account_runners.keys()):
            await self._stop_runner(broker_account_id)

        logger.info("Hub.stop_all(): all runners requested to stop.")

    # Periodically reconcile runner state from Firestore.
    async def _subscription_watchdog_loop(self) -> None:
        """
        Periodically reconcile AccountRunners with the control-plane.

        This allows the hub to respond to:
        - subscription start/expiry,
        - broker_account enable/disable changes,
        - newly created accounts.
        """
        interval = float(self._settings.hub_subscription_poll_interval or 60.0)
        interval = max(10.0, interval)
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._running:
                    continue
                if time.monotonic() < self._subscription_watchdog_next_ts:
                    continue

                logger.debug("Hub subscription watchdog: reconciling runners.")
                try:
                    await self._reconcile_runners_once()
                    if self._subscription_watchdog_failures:
                        logger.info(
                            "Hub subscription watchdog recovered after %d failure(s).",
                            self._subscription_watchdog_failures,
                        )
                    self._subscription_watchdog_failures = 0
                    self._subscription_watchdog_next_ts = 0.0
                except Exception as exc:
                    err_text = repr(exc)
                    self._subscription_watchdog_failures += 1
                    watchdog_backoff = self._backoff_seconds(
                        self._subscription_watchdog_failures,
                        base_seconds=interval,
                        max_seconds=_CONTROL_PLANE_BACKOFF_MAX_SECONDS,
                    )
                    self._subscription_watchdog_next_ts = (
                        time.monotonic() + watchdog_backoff
                    )
                    level = logging.ERROR
                    control_plane_retry_after = None
                    if self._is_control_plane_outage_error(err_text):
                        level = logging.WARNING
                        cp_backoff = self._register_control_plane_failure(err_text)
                        control_plane_retry_after = f"{cp_backoff:.1f}s"
                    log_event(
                        logger,
                        event_type="SUBSCRIPTION_WATCHDOG_ERROR",
                        message="Subscription watchdog reconcile failed",
                        level=level,
                        error=err_text,
                        failures=self._subscription_watchdog_failures,
                        retry_after=f"{watchdog_backoff:.1f}s",
                        control_plane_retry_after=control_plane_retry_after,
                    )
                    continue
        except asyncio.CancelledError:
            logger.debug("Hub._subscription_watchdog_loop() cancelled.")
            raise

    # Periodically run profit sweep and EOD exit engines.
    async def _profit_watchdog_loop(self) -> None:
        """
        Periodically runs hub-level profit and EOD exit engines:

        - ProfitSweepEngine: sweep accounts that hit daily profit target.
        - EODExitEngine: force-exit all positions after configured EOD time.
        """
        interval = float(self._settings.hub_subscription_poll_interval or 60.0)
        interval = max(10.0, interval)
        try:
            while True:
                await asyncio.sleep(interval)
                if not self._running:
                    continue
                try:
                    from app.hub.runtime import (
                        get_hub_runtime,
                    )  # local import to avoid circular

                    runtime = get_hub_runtime()
                    runners = list(self._account_runners.values())

                    if getattr(runtime, "hub_profit_sweep_engine", None) is not None:
                        try:
                            engine = runtime.hub_profit_sweep_engine
                            if engine._dual_sweep_enabled():
                                await engine.maybe_sweep_simple(runners)
                                await engine.maybe_sweep_multiple(runners)
                            else:
                                await engine.maybe_sweep_for_runners(runners)
                        except Exception as exc:
                            log_event(
                                logger,
                                event_type="PROFIT_SWEEP_ERROR",
                                message="ProfitSweepEngine encountered an error",
                                level=logging.ERROR,
                                error=repr(exc),
                            )

                    if getattr(runtime, "eod_exit_engine", None) is not None:
                        try:
                            await runtime.eod_exit_engine.maybe_force_exit_all(runners)
                        except Exception as exc:
                            log_event(
                                logger,
                                event_type="EOD_EXIT_ERROR",
                                message="EODExitEngine encountered an error",
                                level=logging.ERROR,
                                error=repr(exc),
                            )

                except Exception as exc:  # pragma: no cover - watchdog safety net
                    log_event(
                        logger,
                        event_type="PROFIT_WATCHDOG_ERROR",
                        message="profit watchdog loop encountered an error",
                        level=logging.ERROR,
                        error=repr(exc),
                    )
                    continue
        except asyncio.CancelledError:
            logger.debug("Hub._profit_watchdog_loop() cancelled.")
            raise


# Note:
# Capital/Risk/Profit guard run at order creation time (OrderRouter).
# ProfitSweepEngine and EODExitEngine in the watchdog loop send EXIT orders when
# profit targets or end-of-day conditions are met.

__all__ = ["Hub", "pick_active_subscription"]
