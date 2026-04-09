"""App runtime orchestrator for startup and shutdown lifecycle wiring."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Optional

from app.config.boot_config import RuntimeConfig, initialize_boot_config
from app.config.settings import get_settings
from app.core.anti_pattern_guards import (
    mark_reconciliation_complete,
    reset_reconciliation_state,
)
from app.core.feature_flags import (
    load_stability_feature_flags,
    log_stability_feature_flags,
)
from app.core.instrument_control import InstrumentController
from app.core.leader_lease import LeaderLease
from app.core.operating_mode import resolve_operating_mode_from_runtime
from app.core.startup_config_validator import (
    validate_runtime_startup_settings,
    validate_startup_config,
)
from app.core.strategy_switch import StrategySwitchboard
from app.data.postgres import get_control_plane_dsn
from app.data.schema_guard import check_startup_schema
from app.hub.runtime import get_hub_runtime
from app.strategies.naming import all_canonical_strategy_names

logger = logging.getLogger(__name__)
_NON_RETRYABLE_STREAM_ERROR_MARKERS = (
    "ANGEL_TOTP_SECRET environment variable not set",
    "Missing control plane Postgres settings",
    "BROKER_SECRET_BACKEND=postgres",
    "Required runtime strategy attachment(s) missing after startup validation",
    "Hub route validation failed; missing routes for:",
)


def _is_non_retryable_stream_error(exc: Exception) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in _NON_RETRYABLE_STREAM_ERROR_MARKERS)


class StreamWorker:
    """Run the streaming loop in a background thread."""

    def __init__(
        self, switchboard: StrategySwitchboard, instrument_ctrl: InstrumentController
    ) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._switchboard = switchboard
        self._instrument_ctrl = instrument_ctrl
        self._fatal_error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                logger.debug("Stream worker already running")
                return
            if self._fatal_error:
                logger.error(
                    "Stream worker start skipped due to non-retryable configuration error: %s",
                    self._fatal_error,
                )
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="stream-worker", daemon=True
            )
            self._thread.start()
            logger.debug("Stream worker started")

    def _run(self) -> None:
        try:
            from app.runners.multi_instrument_stream import stream_multi_instruments

            stream_multi_instruments(
                stop_event=self._stop_event,
                start_health_server=False,
                strategy_switch=self._switchboard,
                instrument_controller=self._instrument_ctrl,
            )
        except Exception as exc:  # pragma: no cover - safety net
            if _is_non_retryable_stream_error(exc):
                with self._lock:
                    self._fatal_error = str(exc)
            logger.exception("Stream worker crashed: %s", exc)
            self._stop_event.set()

    def stop(self, timeout: float = 10) -> None:
        with self._lock:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=timeout)
            self._thread = None
            logger.debug("Stream worker stopped")

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def fatal_error(self) -> str | None:
        with self._lock:
            return self._fatal_error

    def status_snapshot(self) -> dict[str, object]:
        with self._lock:
            thread = self._thread
            return {
                "running": bool(thread and thread.is_alive()),
                "fatal_error": self._fatal_error,
                "stop_requested": bool(self._stop_event.is_set()),
            }


class WorkerWatchdog:
    """Restart stream worker if it stops unexpectedly."""

    def __init__(
        self,
        worker: StreamWorker,
        interval_seconds: float = 15.0,
        *,
        restart_backoff_base_seconds: float = 5.0,
        restart_backoff_max_seconds: float = 300.0,
        restart_backoff_jitter_ratio: float = 0.2,
        stable_run_window_seconds: float = 180.0,
        now_mono: Callable[[], float] | None = None,
        jitter_fn: Callable[[], float] | None = None,
    ) -> None:
        self._worker = worker
        self._interval = max(1.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_fatal_error_logged: str | None = None
        self._restart_backoff_base_seconds = max(
            1.0,
            float(restart_backoff_base_seconds),
        )
        self._restart_backoff_max_seconds = max(
            self._restart_backoff_base_seconds,
            float(restart_backoff_max_seconds),
        )
        self._restart_backoff_jitter_ratio = max(
            0.0,
            min(1.0, float(restart_backoff_jitter_ratio)),
        )
        self._stable_run_window_seconds = max(0.0, float(stable_run_window_seconds))
        self._now_mono = now_mono or time.monotonic
        self._jitter_fn = jitter_fn or random.random
        self._restart_attempts = 0
        self._current_backoff_seconds = 0.0
        self._next_restart_ts = 0.0
        self._last_restart_ts = 0.0
        self._running_since_ts = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, name="stream-watchdog", daemon=True
            )
            self._thread.start()
            logger.debug("Stream watchdog started")

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            now_mono = self._now_mono()
            if self._worker.running():
                self._last_fatal_error_logged = None
                if self._running_since_ts <= 0.0:
                    self._running_since_ts = now_mono
                elif (
                    self._restart_attempts > 0
                    and self._stable_run_window_seconds > 0
                    and (now_mono - self._running_since_ts)
                    >= self._stable_run_window_seconds
                ):
                    logger.info(
                        "Stream watchdog clearing restart backoff after stable run window (%.1fs).",
                        self._stable_run_window_seconds,
                    )
                    self._reset_restart_backoff()
                continue
            self._running_since_ts = 0.0
            fatal_error = None
            fatal_getter = getattr(self._worker, "fatal_error", None)
            if callable(fatal_getter):
                try:
                    fatal_error = fatal_getter()
                except Exception:
                    fatal_error = None
            if fatal_error:
                fatal_text = str(fatal_error)
                if fatal_text != self._last_fatal_error_logged:
                    logger.error(
                        "Stream watchdog restart suppressed due to non-retryable worker error: %s",
                        fatal_text,
                    )
                    self._last_fatal_error_logged = fatal_text
                continue
            if now_mono < self._next_restart_ts:
                continue
            try:
                self._record_restart_attempt(now_mono)
                logger.warning(
                    "Stream watchdog restarting worker (detected stopped stream). attempts=%d backoff_seconds=%.2f next_restart_in_seconds=%.2f",
                    self._restart_attempts,
                    self._current_backoff_seconds,
                    self._current_backoff_seconds,
                )
                self._worker.start()
            except Exception as exc:  # pragma: no cover - safety net
                logger.error("Stream watchdog failed to restart worker: %s", exc)

    def _record_restart_attempt(self, now_mono: float) -> None:
        self._restart_attempts += 1
        base = self._restart_backoff_base_seconds * (2 ** max(0, self._restart_attempts - 1))
        backoff = min(self._restart_backoff_max_seconds, base)
        jitter = 0.0
        if self._restart_backoff_jitter_ratio > 0.0:
            jitter = (
                float(backoff)
                * float(self._restart_backoff_jitter_ratio)
                * float(self._jitter_fn())
            )
        self._current_backoff_seconds = float(backoff + jitter)
        self._next_restart_ts = float(now_mono + self._current_backoff_seconds)
        self._last_restart_ts = float(now_mono)

    def _reset_restart_backoff(self) -> None:
        self._restart_attempts = 0
        self._current_backoff_seconds = 0.0
        self._next_restart_ts = 0.0
        self._last_restart_ts = 0.0
        self._running_since_ts = 0.0

    def stop(self, timeout: float = 5) -> None:
        with self._lock:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=timeout)
            self._thread = None
            logger.debug("Stream watchdog stopped")

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def status_snapshot(self) -> dict[str, float | int]:
        now_mono = self._now_mono()
        next_restart_in = 0.0
        if self._next_restart_ts > now_mono:
            next_restart_in = self._next_restart_ts - now_mono
        return {
            "restart_attempts": int(self._restart_attempts),
            "current_backoff_seconds": float(self._current_backoff_seconds),
            "next_restart_in_seconds": float(next_restart_in),
            "stable_run_window_seconds": float(self._stable_run_window_seconds),
            "restart_backoff_base_seconds": float(self._restart_backoff_base_seconds),
            "restart_backoff_max_seconds": float(self._restart_backoff_max_seconds),
            "restart_backoff_jitter_ratio": float(self._restart_backoff_jitter_ratio),
            "last_restart_ts_mono": float(self._last_restart_ts),
        }


class AlertEvaluationWorker:
    """Evaluate alert rules on a fixed cadence for runtime visibility."""

    def __init__(self, interval_seconds: float = 30.0) -> None:
        self._interval = max(5.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_summary: dict[str, object] = {
            "evaluated_at": None,
            "firing_count": 0,
            "total_rules": 0,
            "running": False,
        }

    def configure_interval(self, interval_seconds: float) -> None:
        self._interval = max(5.0, float(interval_seconds))

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="alert-evaluator",
                daemon=True,
            )
            self._thread.start()
            logger.debug("Alert evaluation worker started")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            evaluated_at = datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            )
            try:
                from app.observability.alert_rules import get_alert_evaluator

                summary = dict(get_alert_evaluator().summary())
            except Exception as exc:  # pragma: no cover - defensive safety net
                logger.warning("Alert evaluation poll failed: %s", exc)
                summary = {
                    "firing_count": 0,
                    "total_rules": 0,
                    "error": str(exc),
                }
            summary["evaluated_at"] = evaluated_at
            summary["running"] = True
            with self._lock:
                self._last_summary = summary
            if int(summary.get("firing_count", 0) or 0) > 0:
                logger.warning(
                    "Alert evaluation poll found firing alerts count=%s",
                    summary.get("firing_count", 0),
                )
            if self._stop_event.wait(self._interval):
                break

    def stop(self, timeout: float = 5) -> None:
        with self._lock:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=timeout)
            self._thread = None
            self._last_summary = {
                **self._last_summary,
                "running": False,
            }
            logger.debug("Alert evaluation worker stopped")

    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def status_snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._last_summary)


class AppRuntime:
    """Single orchestrator for app startup and shutdown responsibilities."""

    def __init__(
        self,
        *,
        settings_getter: Callable[[], object] = get_settings,
        hub_runtime_getter: Callable[[], object] = get_hub_runtime,
        leader_lease_factory: Callable[..., LeaderLease] = LeaderLease,
    ) -> None:
        self._settings_getter = settings_getter
        self._hub_runtime_getter = hub_runtime_getter
        self._leader_lease_factory = leader_lease_factory
        self._leader_lease: LeaderLease | None = None
        self._is_leader = True
        self._hub_started = False
        self._ready = False  # Readiness latch: set only after full startup
        self._bq_async_writer_started = False
        self._schema_status: dict[str, object] = {
            "status": "unknown",
            "checked_at": None,
            "missing_tables": [],
            "missing_indexes": [],
        }
        self._startup_recovery_status: dict[str, object] = {
            "status": "unknown",
            "reason": None,
            "summary": {},
        }
        self._operating_mode_status: dict[str, object] = {
            "mode": None,
            "authority_path": "unknown",
            "reason": "unresolved",
            "enable_multi_hub": False,
            "use_hub_router": False,
            "disable_stream_worker": False,
        }

        self.strategy_switchboard = StrategySwitchboard()
        self.instrument_controller = InstrumentController()
        self.stream_worker = StreamWorker(
            self.strategy_switchboard, self.instrument_controller
        )
        runtime_cfg = RuntimeConfig.from_env(dict(os.environ))
        watchdog_interval = max(
            1.0,
            float(getattr(runtime_cfg, "stream_watchdog_interval_seconds", 15.0)),
        )
        watchdog_backoff_base = max(
            1.0,
            float(
                getattr(
                    runtime_cfg,
                    "stream_watchdog_restart_backoff_base_seconds",
                    5.0,
                )
            ),
        )
        watchdog_backoff_max = max(
            watchdog_backoff_base,
            float(
                getattr(
                    runtime_cfg,
                    "stream_watchdog_restart_backoff_max_seconds",
                    300.0,
                )
            ),
        )
        watchdog_backoff_jitter = max(
            0.0,
            min(
                1.0,
                float(
                    getattr(
                        runtime_cfg,
                        "stream_watchdog_restart_backoff_jitter_ratio",
                        0.2,
                    )
                ),
            ),
        )
        watchdog_stable_window = max(
            0.0,
            float(
                getattr(
                    runtime_cfg,
                    "stream_watchdog_stable_run_window_seconds",
                    180.0,
                )
            ),
        )
        self.watchdog = WorkerWatchdog(
            self.stream_worker,
            interval_seconds=watchdog_interval,
            restart_backoff_base_seconds=watchdog_backoff_base,
            restart_backoff_max_seconds=watchdog_backoff_max,
            restart_backoff_jitter_ratio=watchdog_backoff_jitter,
            stable_run_window_seconds=watchdog_stable_window,
        )
        self.alert_evaluator = AlertEvaluationWorker()

    async def start(self) -> None:
        # §19.3: Reset reconciliation tracking on startup so replay is
        # blocked until reconciliation completes again.
        reset_reconciliation_state()
        boot_cfg = initialize_boot_config(force=True)
        logger.info("AppRuntime boot config snapshot: %s", boot_cfg.to_log_dict())
        flags = load_stability_feature_flags(log=logger)
        log_stability_feature_flags(flags, log=logger)
        settings = self._settings_getter()

        runtime_cfg = boot_cfg.runtime
        self.alert_evaluator.configure_interval(
            float(getattr(settings, "alert_evaluation_interval_seconds", 30.0))
        )
        disable_worker = bool(runtime_cfg.disable_stream_worker)
        operating_mode = resolve_operating_mode_from_runtime(
            settings=settings,
            runtime_cfg=runtime_cfg,
        )
        self._operating_mode_status = operating_mode.to_log_dict()
        logger.info("AppRuntime operating mode: %s", self._operating_mode_status)
        trade_mode = str(boot_cfg.env.get("TRADE_MODE", "PAPER")).strip().upper() or "PAPER"
        startup_validate_enabled = bool(
            getattr(settings, "app_runtime_startup_validate", False)
        ) or trade_mode == "LIVE"
        effective_schema_mode = "strict" if trade_mode == "LIVE" else str(
            getattr(settings, "schema_check_mode", "warn")
        )
        if trade_mode == "LIVE":
            logger.info(
                "LIVE mode startup validation forced on with schema_check_mode=%s",
                effective_schema_mode,
            )

        schema_checked_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
        try:
            schema_result = check_startup_schema(
                settings=settings,
                mode=effective_schema_mode,
            )
            schema_ok = not bool(schema_result.missing_tables or schema_result.missing_indexes)
            self._schema_status = {
                "status": "ok" if schema_ok else "degraded",
                "checked_at": schema_checked_at,
                "missing_tables": list(schema_result.missing_tables or ()),
                "missing_indexes": list(schema_result.missing_indexes or ()),
            }
        except Exception as exc:
            self._schema_status = {
                "status": "error",
                "checked_at": schema_checked_at,
                "missing_tables": [],
                "missing_indexes": [],
                "error": str(exc),
            }
            raise
        if startup_validate_enabled:
            disable_trading_window_filter = str(
                boot_cfg.env.get("DISABLE_TRADING_WINDOW_FILTER", "false")
            ).strip().lower() in {"1", "true", "yes", "on"}
            validate_runtime_startup_settings(
                settings=settings,
                runtime_cfg=runtime_cfg,
                env=boot_cfg.env,
            )
            validate_startup_config(
                strategy_cfg=boot_cfg.strategy_env,
                trade_mode=trade_mode,
                disable_trading_window_filter=disable_trading_window_filter,
                known_strategy_names=all_canonical_strategy_names(),
                env=boot_cfg.env,
            )

        enable_leader_lease = (
            runtime_cfg.leader_lease_enabled_override
            if runtime_cfg.leader_lease_enabled_override is not None
            else bool(os.getenv("K_SERVICE"))
        )
        lease_backend = str(
            getattr(runtime_cfg, "leader_lease_backend", "") or ""
        ).strip().lower()
        if not lease_backend:
            lease_backend = (
                "postgres"
                if trade_mode == "LIVE"
                and str(getattr(settings, "control_plane_backend", "")).strip().lower()
                == "postgres"
                else "firestore"
            )
        self._is_leader = True

        if enable_leader_lease:
            lease_id = (
                runtime_cfg.leader_lease_id
                or ("phoenix-live-single-stack" if trade_mode == "LIVE" else None)
                or os.getenv("K_SERVICE")
                or "trading-worker"
            )
            lease_ttl = int(runtime_cfg.leader_lease_ttl_seconds)
            lease_renew = int(runtime_cfg.leader_lease_renew_seconds)
            lease_collection = runtime_cfg.leader_lease_collection
            lease_dsn = (
                get_control_plane_dsn(settings)
                if lease_backend == "postgres"
                else None
            )
            self._leader_lease = self._leader_lease_factory(
                lease_id=lease_id,
                ttl_seconds=lease_ttl,
                renew_seconds=lease_renew,
                collection=lease_collection,
                enabled=True,
                backend=lease_backend,
                postgres_dsn=lease_dsn,
            )
            self._is_leader = await self._leader_lease.start()
            if not self._is_leader:
                logger.warning(
                    "Leader lease not acquired; trading workers will remain disabled on this instance."
                )

        if disable_worker:
            logger.info("Stream worker disabled via DISABLE_STREAM_WORKER")
        elif self._is_leader:
            self.stream_worker.start()
            self.watchdog.start()
        if self._is_leader:
            self.alert_evaluator.start()

        if self._is_leader:
            try:
                from app.data.bq_async_writer import start_global_writer
                from app.data.bq_persister import get_bq_client

                writer = start_global_writer(client_getter=get_bq_client)
                self._bq_async_writer_started = writer is not None
            except Exception as exc:
                logger.warning("Failed to start BQ async writer: %s", exc)
                self._bq_async_writer_started = False
        if bool(getattr(settings, "enable_multi_hub", False)) and self._is_leader:
            try:
                runtime = self._hub_runtime_getter()
            except Exception as exc:
                if bool(
                    getattr(
                        settings,
                        "order_lifecycle_persist_markers_required",
                        False,
                    )
                ):
                    logger.error(
                        "AppRuntime startup aborted: required order lifecycle durable marker backend unavailable: %s",
                        exc,
                    )
                raise
            await runtime.hub.initialize()
            await runtime.hub.start_all()
            wait_for_runner_startup = getattr(runtime.hub, "wait_for_runner_startup", None)
            if callable(wait_for_runner_startup):
                await wait_for_runner_startup()
            await self._mark_recovery_pending(runtime)
            order_lifecycle = getattr(runtime, "order_lifecycle", None)
            if order_lifecycle is not None:
                await order_lifecycle.start()
            order_router = getattr(runtime, "order_router", None)
            recover_submission_outbox = getattr(
                order_router,
                "recover_submission_outbox",
                None,
            )
            if callable(recover_submission_outbox):
                recovery_summary = await recover_submission_outbox()
                self._apply_startup_recovery_result(
                    order_router=order_router,
                    summary=recovery_summary,
                    strict_mode=bool(
                        getattr(
                            flags,
                            "order_lifecycle_strict_startup_recovery",
                            False,
                        )
                    ),
                )
            # §19.3: Reconciliation complete -- unlock replay for this scope.
            mark_reconciliation_complete("order_router")
            logger.info(
                "Reconciliation complete for order_router; replay is now permitted."
            )
            self._hub_started = True

        # Readiness latch: all recovery, reconciliation, and hub init are
        # complete.  Order acceptance is blocked until this is True.
        self._ready = True
        logger.info("AppRuntime RUNTIME_READY — readiness latch set, order acceptance enabled.")

    @property
    def ready(self) -> bool:
        """True only after full startup (recovery + reconciliation + hub init)."""
        if self._leader_lease is not None and not self._is_leader:
            return False
        return self._ready

    async def stop(self) -> None:
        self.alert_evaluator.stop()
        self.watchdog.stop()
        self.stream_worker.stop()

        # §16: Close dashboard WebSocket connections before flushing writers
        await self._close_dashboard_websockets()

        if self._is_leader and self._bq_async_writer_started:
            try:
                from app.data.bq_async_writer import stop_global_writer

                stop_global_writer()
            except Exception as exc:
                logger.warning("Failed to stop BQ async writer: %s", exc)
            self._bq_async_writer_started = False

        if self._is_leader and self._hub_started:
            runtime = self._hub_runtime_getter()
            order_lifecycle = getattr(runtime, "order_lifecycle", None)
            if order_lifecycle is not None:
                await order_lifecycle.stop()
            await runtime.hub.stop_all()
            self._hub_started = False

        if self._leader_lease is not None:
            await self._leader_lease.stop()
            self._leader_lease = None
        self._is_leader = True

    async def _close_dashboard_websockets(self) -> None:
        """Close active dashboard WebSocket connections per Architecture §16."""
        try:
            from app.server import close_active_dashboard_sockets

            await close_active_dashboard_sockets()
        except Exception as exc:
            logger.warning("Failed to close dashboard WebSocket connections: %s", exc)

    def stream_worker_running(self) -> bool:
        return self.stream_worker.running()

    def stream_worker_fatal_error(self) -> str | None:
        fatal_getter = getattr(self.stream_worker, "fatal_error", None)
        if callable(fatal_getter):
            try:
                fatal_error = fatal_getter()
            except Exception:
                return None
            if fatal_error:
                return str(fatal_error)
        return None

    def stream_worker_status(self) -> dict[str, object]:
        snapshot_getter = getattr(self.stream_worker, "status_snapshot", None)
        if callable(snapshot_getter):
            try:
                return dict(snapshot_getter())
            except Exception:
                return {}
        snapshot: dict[str, object] = {
            "running": bool(self.stream_worker_running()),
        }
        fatal_error = self.stream_worker_fatal_error()
        if fatal_error:
            snapshot["fatal_error"] = fatal_error
        return snapshot

    def watchdog_running(self) -> bool:
        return self.watchdog.running()

    def watchdog_status(self) -> dict[str, object]:
        snapshot_getter = getattr(self.watchdog, "status_snapshot", None)
        if callable(snapshot_getter):
            try:
                return dict(snapshot_getter())
            except Exception:
                return {}
        return {}

    def alert_evaluator_status(self) -> dict[str, object]:
        snapshot_getter = getattr(self.alert_evaluator, "status_snapshot", None)
        if callable(snapshot_getter):
            try:
                return dict(snapshot_getter())
            except Exception:
                return {}
        return {}

    def leader_lease_status(self) -> dict[str, object]:
        lease = self._leader_lease
        if lease is not None:
            snapshot_getter = getattr(lease, "status_snapshot", None)
            if callable(snapshot_getter):
                try:
                    return dict(snapshot_getter())
                except Exception:
                    return {}
        return {
            "enabled": False,
            "owned": bool(self._is_leader),
            "task_running": False,
        }

    def schema_status(self) -> dict[str, object]:
        return dict(self._schema_status)

    def operating_mode_status(self) -> dict[str, object]:
        return dict(self._operating_mode_status)

    def startup_recovery_status(self) -> dict[str, object]:
        return {
            "status": self._startup_recovery_status.get("status"),
            "reason": self._startup_recovery_status.get("reason"),
            "summary": dict(
                self._startup_recovery_status.get("summary")
                if isinstance(self._startup_recovery_status.get("summary"), dict)
                else {}
            ),
        }

    async def _mark_recovery_pending(self, runtime: object) -> None:
        """Mark all restored lifecycle contexts and ownership records as
        RECOVERY_PENDING before broker reconciliation (Architecture §11.1).

        Uses getattr with None fallback so the method is backward-compatible
        when the underlying services have not yet implemented the marking API.
        """
        logger.info(
            "Marking restored state as RECOVERY_PENDING before broker reconciliation"
        )
        # Mark order lifecycle contexts
        order_lifecycle = getattr(runtime, "order_lifecycle", None)
        if order_lifecycle is not None:
            mark_fn = getattr(order_lifecycle, "mark_recovery_pending", None)
            if callable(mark_fn):
                try:
                    result = mark_fn()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning(
                        "Failed to mark order lifecycle RECOVERY_PENDING: %s", exc
                    )

        # Mark position ownership records
        position_ownership = getattr(runtime, "position_ownership", None)
        if position_ownership is not None:
            mark_all_fn = getattr(position_ownership, "mark_all_recovery_pending", None)
            if callable(mark_all_fn):
                try:
                    result = mark_all_fn()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning(
                        "Failed to mark position ownership RECOVERY_PENDING: %s", exc
                    )

    def _apply_startup_recovery_result(
        self,
        *,
        order_router: Optional[object],
        summary: Optional[dict[str, Any]],
        strict_mode: bool,
    ) -> None:
        resolved_summary = dict(summary or {})
        failed = int(resolved_summary.get("failed", 0) or 0)
        unresolved_active = int(
            resolved_summary.get(
                "unresolved_active",
                resolved_summary.get("deferred", 0),
            )
            or 0
        )
        degraded = bool(strict_mode and (failed > 0 or unresolved_active > 0))
        reason = None
        if degraded:
            reason = (
                f"startup_recovery_degraded_failed_{failed}_unresolved_{unresolved_active}"
            )
        gate_setter = getattr(order_router, "set_startup_recovery_gate", None)
        if callable(gate_setter):
            gate_setter(
                block_new_entries=degraded,
                reason=reason,
                summary=resolved_summary,
            )
        self._startup_recovery_status = {
            "status": "degraded" if degraded else "ok",
            "reason": reason,
            "summary": resolved_summary,
        }
        logger.log(
            logging.ERROR if degraded else logging.INFO,
            "AppRuntime startup recovery status strict_mode=%s degraded=%s reason=%s summary=%s",
            strict_mode,
            degraded,
            reason,
            resolved_summary,
        )


@lru_cache(maxsize=1)
def get_app_runtime() -> AppRuntime:
    return AppRuntime()


__all__ = [
    "AlertEvaluationWorker",
    "AppRuntime",
    "get_app_runtime",
    "StreamWorker",
    "WorkerWatchdog",
]
