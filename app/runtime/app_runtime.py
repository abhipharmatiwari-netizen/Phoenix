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
from app.config.runtime_config import get_runtime_settings
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
from app.data.postgres import connect_with_retry, get_control_plane_dsn
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
_LOCAL_POSTGRES_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "postgres",
        "phoenix-oci-postgres",
    }
)


def _is_non_retryable_stream_error(exc: Exception) -> bool:
    text = str(exc or "")
    return any(marker in text for marker in _NON_RETRYABLE_STREAM_ERROR_MARKERS)


def _is_local_postgres_host(settings: Any) -> bool:
    host = str(
        getattr(settings, "control_plane_pg_host", None)
        or os.getenv("CONTROL_PLANE_PG_HOST", "")
        or ""
    ).strip().lower()
    return host in _LOCAL_POSTGRES_HOSTS


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
                last_tick_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                status_getter = getattr(self._worker, "status_snapshot", None)
                if callable(status_getter):
                    try:
                        _snap = status_getter()
                        if isinstance(_snap, dict) and _snap.get("last_tick_ts"):
                            last_tick_ts = str(_snap["last_tick_ts"])
                    except Exception:
                        pass
                logger.warning(
                    "stream_worker.watchdog_restart_detected attempts=%d backoff_seconds=%.2f "
                    "last_tick_ts=%s",
                    self._restart_attempts,
                    self._current_backoff_seconds,
                    last_tick_ts,
                )
                self._worker.start()
            except Exception as exc:  # pragma: no cover - safety net
                logger.error("Stream watchdog failed to restart worker: %s", exc)

    def _record_restart_attempt(self, now_mono: float) -> None:
        self._restart_attempts += 1
        # Use float base to avoid OverflowError when attempt count is very large (>~1023);
        # 2.0**huge_exp yields inf, and min(max_seconds, inf) == max_seconds.
        base = self._restart_backoff_base_seconds * (2.0 ** max(0, self._restart_attempts - 1))
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
        self._position_authority_restored = False  # True after load_position_records succeeds
        self._position_records_loaded = 0  # Count of non-terminal position records restored
        self._startup_ownership_gap_detected = False  # True if positions loaded but 0 ownership records
        self._bq_async_writer_started = False
        self._schema_status: dict[str, object] = {
            "status": "unknown",
            "checked_at": None,
            "missing_tables": [],
            "missing_indexes": [],
            "missing_columns": [],
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
            import os as _os
            import pathlib as _pathlib
            _repo_root = _pathlib.Path(__file__).resolve().parents[2]
            _runtime_artifacts = [
                str(p.relative_to(_repo_root))
                for p in _repo_root.iterdir()
                if p.is_file() and (
                    p.suffix in (".runtime",)
                    or p.name.endswith(".env.runtime")
                    or p.name.endswith(".runtime.env")
                )
            ]
            if _runtime_artifacts:
                raise ValueError(
                    f"startup.env_runtime_artifact_error: TRADE_MODE=LIVE but repo-root secret "
                    f"artifacts were found: {_runtime_artifacts}. These files blur the approved "
                    f"secret boundary. Remove them and inject secrets only from approved transient "
                    f"sources (SecretStore, Secret Manager, or process environment)."
                )
            _placeholder_checks = {
                "CONTROL_PLANE_PG_PASSWORD": getattr(settings, "control_plane_pg_password", None),
                "ADMIN_API_KEY": _os.getenv("ADMIN_API_KEY", ""),
                "DEMO_AUTH_TOKEN_SECRET": _os.getenv("DEMO_AUTH_TOKEN_SECRET", ""),
            }
            _placeholder_markers = ("CHANGE_ME", "CHANGEME", "TODO", "REPLACE_ME", "PLACEHOLDER")
            _bad_placeholders = [
                name for name, val in _placeholder_checks.items()
                if val and any(m in str(val).upper() for m in _placeholder_markers)
            ]
            if _bad_placeholders:
                raise ValueError(
                    f"startup.placeholder_secret_error: TRADE_MODE=LIVE but placeholder/template "
                    f"values detected in secrets: {_bad_placeholders}. This indicates a legacy "
                    f"reference env file (e.g. .docker-live.env) was used as-is. Inject real "
                    f"secrets from SecretStore or Secret Manager before deploying to LIVE."
                )
            _pg_sslmode = str(
                getattr(settings, "control_plane_pg_sslmode", None) or ""
            ).strip().lower()
            _ssl_check_ok = _pg_sslmode in ("require", "verify-ca", "verify-full")
            _ssl_skip = _os.getenv("LIVE_PG_SSL_SKIP_CHECK", "").strip().lower() in ("1", "true", "yes")
            # §105: Detect remote/cloud deployment.
            # Signals checked (any one is sufficient):
            #   K_SERVICE          — Google Cloud Run
            #   OCI_RESOURCE_PRINCIPAL_REGION_URL — Oracle Cloud instance principal
            #   REMOTE_DEPLOYMENT  — generic operator flag (set in any non-local env file)
            # LIVE_PG_SSL_SKIP_CHECK=true is NEVER acceptable in remote/cloud deployments.
            # For local Docker Desktop (none of the above set), the skip remains
            # a warning-only exception.
            _is_cloud = bool(
                _os.getenv("K_SERVICE", "").strip()
                or _os.getenv("OCI_RESOURCE_PRINCIPAL_REGION_URL", "").strip()
                or _os.getenv("REMOTE_DEPLOYMENT", "").strip().lower() in ("1", "true", "yes")
            )
            if not _ssl_check_ok:
                if _ssl_skip and _is_cloud:
                    raise RuntimeError(
                        "startup.ssl_error: LIVE_PG_SSL_SKIP_CHECK=true is forbidden in "
                        "remote/cloud deployments (K_SERVICE, OCI_RESOURCE_PRINCIPAL_REGION_URL, "
                        "or REMOTE_DEPLOYMENT is set). "
                        f"CONTROL_PLANE_PG_SSLMODE={_pg_sslmode!r} does not enforce encrypted "
                        "Postgres transport. Set CONTROL_PLANE_PG_SSLMODE=require and remove "
                        "LIVE_PG_SSL_SKIP_CHECK from the deployment environment."
                    )
                elif _ssl_skip:
                    _is_local_pg_host = _is_local_postgres_host(settings)
                    _ssl_log_level = logging.INFO if _is_local_pg_host else logging.WARNING
                    _ssl_event = (
                        "startup.ssl_skip_info"
                        if _is_local_pg_host
                        else "startup.ssl_warning"
                    )
                    _ssl_note = (
                        "Plaintext Postgres transport accepted for local Docker "
                        "Postgres host. This must not be used for network-attached "
                        "or externally published Postgres deployments."
                        if _is_local_pg_host
                        else "Plaintext Postgres transport accepted without a "
                        "recognized local Postgres host. Set CONTROL_PLANE_PG_SSLMODE=require "
                        "for network-attached deployments."
                    )
                    logger.log(
                        _ssl_log_level,
                        "%s: LIVE_PG_SSL_SKIP_CHECK=true; "
                        "CONTROL_PLANE_PG_SSLMODE=%r does not enforce encrypted "
                        "Postgres transport. %s",
                        _ssl_event,
                        _pg_sslmode,
                        _ssl_note,
                    )
                    # Issue #6 / #342: Emit a durable audit event so every LIVE
                    # deployment with plaintext Postgres transport has an auditable record.
                    try:
                        from app.core.audit_log import emit_audit_event
                        emit_audit_event(
                            actor="startup",
                            action="live_pg_ssl_skip_check_active",
                            resource_type="postgres_transport",
                            resource_id="control_plane",
                            after={
                                "sslmode": _pg_sslmode,
                                "live_pg_ssl_skip_check": True,
                                "is_cloud": False,
                                "local_postgres_host": _is_local_pg_host,
                                "note": _ssl_note,
                            },
                        )
                    except Exception as _audit_exc:
                        logger.warning(
                            "startup: failed to emit ssl_skip audit event: %s",
                            _audit_exc,
                        )
                else:
                    raise ValueError(
                        f"startup.ssl_error: TRADE_MODE=LIVE requires CONTROL_PLANE_PG_SSLMODE=require "
                        f"(or verify-ca / verify-full). Current value is {_pg_sslmode!r}. "
                        f"For local Postgres without SSL, set LIVE_PG_SSL_SKIP_CHECK=true in the "
                        f"deployment manifest (local dev only — never in remote/cloud deployments)."
                    )
            _broker_schema_mode = str(
                _os.getenv("BROKER_SCHEMA_CHECK_MODE", "") or ""
            ).strip().lower()
            if _broker_schema_mode != "strict":
                logger.warning(
                    "startup.broker_schema_check_warning: TRADE_MODE=LIVE but "
                    "BROKER_SCHEMA_CHECK_MODE=%s (not strict); malformed AngelOne API "
                    "responses will not be rejected at the integration boundary. "
                    "Set BROKER_SCHEMA_CHECK_MODE=strict in production.",
                    _broker_schema_mode or "off",
                )

            # §133 / Issue #12: Warn when RISK_STATE_PATH or EXECUTED_TOKENS_STATE_PATH
            # resolves to a path inside the repo root.  This means pytest runs on the
            # same machine may overwrite production state files between LIVE sessions.
            # The default compose binds ./state and ./logs (inside the repo).
            # Operators should set PHOENIX_STATE_HOST_PATH and PHOENIX_LOG_HOST_PATH
            # to paths outside the repo for production deployments.
            # Skip this warning inside Docker containers: inside the container /app is
            # the workdir and /app/state is a proper volume mount; pytest does not run
            # in the production container so there is no contamination risk.
            _is_in_container = _pathlib.Path("/.dockerenv").exists() or bool(
                _os.getenv("KUBERNETES_SERVICE_HOST")
                or _os.getenv("K_SERVICE")  # Cloud Run
            )
            if not _is_in_container:
                try:
                    _risk_state = _pathlib.Path(
                        _os.getenv("RISK_STATE_PATH", "./state/risk_positions.json")
                    ).resolve()
                    _token_state = _pathlib.Path(
                        _os.getenv(
                            "EXECUTED_TOKENS_STATE_PATH",
                            "/app/logs/executed_tokens_state.json",
                        )
                    ).resolve()
                    for _state_label, _state_path in (
                        ("RISK_STATE_PATH", _risk_state),
                        ("EXECUTED_TOKENS_STATE_PATH", _token_state),
                    ):
                        try:
                            _state_path.relative_to(_repo_root)
                            logger.warning(
                                "startup.state_path_inside_repo: %s=%s is inside the repo root %s. "
                                "pytest runs on this machine may overwrite production state files. "
                                "Set PHOENIX_STATE_HOST_PATH (and PHOENIX_LOG_HOST_PATH) to a path "
                                "outside the repo for production deployments (e.g. /var/lib/phoenix/state).",
                                _state_label,
                                _state_path,
                                _repo_root,
                            )
                        except ValueError:
                            pass  # path is outside repo root — safe
                except Exception as _sp_exc:
                    logger.debug("startup: state-path repo-check failed (non-fatal): %s", _sp_exc)

        schema_checked_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
        try:
            schema_result = check_startup_schema(
                settings=settings,
                mode=effective_schema_mode,
            )
            missing_columns = list(getattr(schema_result, "missing_columns", ()) or ())
            schema_ok = not bool(
                schema_result.missing_tables
                or schema_result.missing_indexes
                or missing_columns
            )
            self._schema_status = {
                "status": "ok" if schema_ok else "degraded",
                "checked_at": schema_checked_at,
                "missing_tables": list(schema_result.missing_tables or ()),
                "missing_indexes": list(schema_result.missing_indexes or ()),
                "missing_columns": missing_columns,
            }
        except Exception as exc:
            self._schema_status = {
                "status": "error",
                "checked_at": schema_checked_at,
                "missing_tables": [],
                "missing_indexes": [],
                "missing_columns": [],
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
            # PR #265 round-9 (Codex round-8 P2 "Resolve provider
            # overrides before AppRuntime validation"): the actual
            # ``StrategySelector`` is constructed from
            # ``get_runtime_settings(base_settings=settings)``, which
            # layers runtime-config-provider overrides on top of the
            # env-backed ``Settings``. Resolve the same proxy here so
            # the cap-vs-mapping validator sees the values the stream
            # will actually use. Round-8 passed the base settings
            # which missed the env-unset + provider-overrides case.
            resolved_settings_for_validator = get_runtime_settings(
                base_settings=settings
            )
            validate_startup_config(
                strategy_cfg=boot_cfg.strategy_env,
                trade_mode=trade_mode,
                disable_trading_window_filter=disable_trading_window_filter,
                known_strategy_names=all_canonical_strategy_names(),
                env=boot_cfg.env,
                runtime_settings=resolved_settings_for_validator,
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

        hub_runtime_for_startup = None
        if bool(getattr(settings, "enable_multi_hub", False)) and self._is_leader:
            try:
                # Materialize the singleton before the stream worker can touch it.
                # Otherwise concurrent first access can create a second HubRuntime.
                hub_runtime_for_startup = self._hub_runtime_getter()
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
            runtime = hub_runtime_for_startup or self._hub_runtime_getter()
            # Architecture startup contract (§11.1 / §64):
            # restore authoritative state → mark RECOVERY_PENDING
            # → broker reconciliation → replay evaluation.
            await runtime.hub.initialize()

            # Step 1: Start order lifecycle service before any broker I/O.
            order_lifecycle = getattr(runtime, "order_lifecycle", None)
            if order_lifecycle is not None:
                await order_lifecycle.start()

            # Step 1b: LIVE-only pre-load cleanup — terminate expired-contract
            # position records BEFORE load_position_records() so they are never
            # loaded into the in-memory graph.  This must run before Step 2 to
            # prevent the RECONCILING→DEGRADED cascade during outbox recovery.
            if trade_mode == "LIVE" and order_lifecycle is not None:
                try:
                    from app.orders.order_lifecycle import OrderLifecycleService
                    _pre_cleaned = OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
                        symbol_pattern="",  # not used for position records (age-only)
                        min_age_days=7,  # weekly expiry — 7 days is sufficient
                    )
                    if _pre_cleaned:
                        logger.info(
                            "startup.expired_position_cleanup: force-terminated %d non-terminal "
                            "position records older than 7 days before position load.",
                            _pre_cleaned,
                        )
                    else:
                        logger.info("startup.expired_position_cleanup: no expired position records found (clean DB).")
                except Exception as _pre_exc:
                    logger.warning("startup.expired_position_cleanup (pre-load) failed: %s", _pre_exc)

            # Step 2: Restore persisted authoritative position state (§48).
            if order_lifecycle is not None:
                try:
                    _pos_dsn = get_control_plane_dsn()
                    with connect_with_retry(_pos_dsn, autocommit=True) as _pos_conn:
                        _loaded = order_lifecycle.load_position_records(_pos_conn)
                    self._position_records_loaded = _loaded
                    self._position_authority_restored = _loaded > 0
                    logger.info(
                        "startup.position_records_loaded: restored %d authoritative "
                        "position record(s) from Postgres",
                        _loaded,
                    )
                except Exception as _pos_exc:
                    if trade_mode == "LIVE":
                        logger.error(
                            "startup.position_records_load_failed: TRADE_MODE=LIVE requires "
                            "durable position-state restore — aborting startup: %s", _pos_exc
                        )
                        raise
                    logger.warning(
                        "startup.position_records_load_failed (non-fatal): %s", _pos_exc
                    )
                    self._position_authority_restored = False

            # Step 3: Restore kill-switch durable state (§58). Fail-stop in LIVE.
            ksm = getattr(runtime, "kill_switch_manager", None)
            if ksm is not None:
                try:
                    from app.risk.kill_switch import KillSwitchManager
                    _ks_dsn = get_control_plane_dsn()
                    with connect_with_retry(_ks_dsn, autocommit=True) as _ks_conn:
                        loaded_ksm = KillSwitchManager.load_state(_ks_conn)
                    runtime.kill_switch_manager = loaded_ksm
                    active = loaded_ksm.get_all_active()
                    logger.info(
                        "startup.kill_switch_loaded: restored %d non-INACTIVE "
                        "kill-switch records from Postgres",
                        len(active),
                    )
                    if active:
                        logger.warning(
                            "startup.kill_switch_active: %d kill switch(es) are "
                            "currently non-INACTIVE: %s",
                            len(active),
                            [(r.scope.value, r.scope_id, r.state.value) for r in active],
                        )
                except Exception as _ks_exc:
                    if trade_mode == "LIVE":
                        logger.error(
                            "startup.kill_switch_load_failed: TRADE_MODE=LIVE requires "
                            "durable kill-switch restore — aborting startup: %s", _ks_exc
                        )
                        raise
                    logger.warning(
                        "startup.kill_switch_load_failed (non-fatal): %s", _ks_exc
                    )

            # Step 4: Mark all restored records RECOVERY_PENDING before broker sync.
            await self._mark_recovery_pending(runtime, trade_mode=trade_mode)

            # Step 5: Start runners — initial broker sync (reconciliation) happens here.
            await runtime.hub.start_all()
            wait_for_runner_startup = getattr(runtime.hub, "wait_for_runner_startup", None)
            if callable(wait_for_runner_startup):
                await wait_for_runner_startup()

            order_router = getattr(runtime, "order_router", None)
            recover_submission_outbox = getattr(
                order_router,
                "recover_submission_outbox",
                None,
            )
            if trade_mode == "LIVE":
                try:
                    # Markers checked against BOTH hub_order_id and broker_order_id.
                    # These are test-fixture strings that never appear in real orders.
                    _SYNTHETIC_MARKERS = (
                        "B123", "B124", "OID-ONCE", "OID-PARALLEL",
                        "TEST-", "FAKE-", "MOCK-", "FIXTURE",
                        "NIFTY17FEB2625750CE",
                    )
                    # §74: __runtime_exit__ appears in hub_order_id of REAL historical
                    # orders (March 2026 safety exits) where the strategy was unresolvable.
                    # It must only be flagged when it appears in broker_order_id, because
                    # real Angel broker IDs are numeric and never contain this string.
                    _BROKER_ONLY_MARKERS = ("__runtime_exit__",)
                    _dsn = get_control_plane_dsn()
                    with connect_with_retry(_dsn, autocommit=True) as _conn:
                        with _conn.cursor() as _cur:
                            _cur.execute(
                                """
                                SELECT hub_order_id, broker_order_id
                                FROM order_submission_outbox
                                WHERE status NOT IN (
                                    'TERMINAL_FILL', 'TERMINAL_NON_FILL',
                                    'TERMINAL_ERROR', 'CANCELLED', 'EXPIRED'
                                )
                                LIMIT 500
                                """
                            )
                            _rows = _cur.fetchall()
                    _contaminated = [
                        f"{r[0]}|{r[1]}"
                        for r in (_rows or [])
                        if (
                            any(m in str(r[0] or "") or m in str(r[1] or "") for m in _SYNTHETIC_MARKERS)
                            or any(m in str(r[1] or "") for m in _BROKER_ONLY_MARKERS)
                        )
                    ]
                    if _contaminated:
                        raise ValueError(
                            f"startup.synthetic_contamination_error: TRADE_MODE=LIVE but "
                            f"{len(_contaminated)} outbox record(s) contain known synthetic/test "
                            f"identifiers: {_contaminated[:5]}. The control-plane DB appears to "
                            f"contain unit-test fixtures. Sanitize the DB before starting LIVE."
                        )
                except ValueError:
                    raise
                except Exception as _exc:
                    logger.warning("startup.synthetic_contamination_check failed (non-fatal): %s", _exc)

                try:
                    from app.orders.order_outbox import force_terminal_outbox_by_symbol_pattern
                    import datetime as _dt
                    # Compute the PREVIOUS calendar month label (e.g. "MAR26" when
                    # running in April).  The day-subtraction approach is wrong here:
                    # today - 7d on April 24 gives April 17 → "APR26", not "MAR26".
                    # Instead, go to the first day of the current month then back 1 day
                    # to land reliably in the previous month.
                    _today = _dt.date.today()
                    _first_of_month = _today.replace(day=1)
                    _last_prev_month = _first_of_month - _dt.timedelta(days=1)
                    _cutoff_month = _last_prev_month.strftime("%b%y").upper()
                    _cleaned = force_terminal_outbox_by_symbol_pattern(
                        symbol_pattern=f"%{_cutoff_month}%",
                        min_age_days=7,
                    )
                    if _cleaned:
                        logger.info(
                            "startup.expired_contract_cleanup: force-terminated %d non-terminal "
                            "outbox records for contracts expiring on or before %s.",
                            _cleaned, _cutoff_month,
                        )
                except Exception as _exc:
                    logger.warning("startup.expired_contract_cleanup failed (non-fatal): %s", _exc)

                # §73: Also terminate expired-contract position records in
                # internal_position_records so they don't produce DEGRADED
                # cascades during outbox recovery on every restart.
                try:
                    from app.orders.order_lifecycle import OrderLifecycleService
                    import datetime as _dt2
                    _pos_cutoff = (_dt2.date.today() - _dt2.timedelta(days=45)).strftime("%b%y").upper()
                    _pos_cleaned = OrderLifecycleService.force_terminal_positions_by_symbol_pattern(
                        symbol_pattern=f"%{_pos_cutoff}%",
                        min_age_days=45,
                    )
                    if _pos_cleaned:
                        logger.info(
                            "startup.expired_position_cleanup: force-terminated %d non-terminal "
                            "position records for contracts expiring on or before %s.",
                            _pos_cleaned, _pos_cutoff,
                        )
                except Exception as _exc:
                    logger.warning("startup.expired_position_cleanup failed (non-fatal): %s", _exc)

                # Data-retention purges: remove old terminal/flat records so
                # tables don't grow unboundedly over months of trading.
                import os as _os_ret
                _outbox_retain  = int(_os_ret.getenv("OUTBOX_RETENTION_DAYS",  "90"))
                _pos_retain     = int(_os_ret.getenv("POSITION_RETENTION_DAYS", "90"))
                _markers_retain = int(_os_ret.getenv("MARKERS_RETENTION_DAYS", "180"))

                try:
                    from app.orders.order_outbox import purge_old_terminal_outbox_records
                    purge_old_terminal_outbox_records(retain_days=_outbox_retain)
                except Exception as _exc:
                    logger.warning("retention.outbox_purge failed (non-fatal): %s", _exc)

                try:
                    from app.orders.order_lifecycle import OrderLifecycleService
                    OrderLifecycleService.purge_old_flat_position_records(retain_days=_pos_retain)
                except Exception as _exc:
                    logger.warning("retention.position_purge failed (non-fatal): %s", _exc)

                try:
                    from app.orders.trade_processed_store import purge_old_processed_markers
                    purge_old_processed_markers(retain_days=_markers_retain)
                except Exception as _exc:
                    logger.warning("retention.markers_purge failed (non-fatal): %s", _exc)

            if callable(recover_submission_outbox):
                recovery_summary = await recover_submission_outbox()
                self._apply_startup_recovery_result(
                    order_router=order_router,
                    summary=recovery_summary,
                    strict_mode=(
                        trade_mode == "LIVE"
                        or bool(
                            getattr(
                                flags,
                                "order_lifecycle_strict_startup_recovery",
                                False,
                            )
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
        logger.info(
            "startup.runtime_ready: readiness latch set — outbox evaluated, "
            "reconciliation complete, order acceptance enabled. "
            "recovery_status=%s",
            self._startup_recovery_status.get("status"),
        )

    @property
    def ready(self) -> bool:
        """True only after full startup (recovery + reconciliation + hub init).

        §122 / Issue #1: Also returns False when _startup_ownership_gap_detected is
        True so the strategy bridge is gated by the same condition as /readyz.
        Previously the bridge only checked the _ready latch, meaning live orders could
        flow while Docker considered the container unhealthy due to an ownership gap.
        """
        if self._leader_lease is not None and not self._is_leader:
            return False
        if self._startup_ownership_gap_detected:
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
                # Flush position records before stopping so the last authoritative
                # state survives a graceful restart.
                try:
                    with connect_with_retry(get_control_plane_dsn(), autocommit=True) as _conn:
                        order_lifecycle.save_position_records(_conn)
                except Exception as _exc:
                    logger.warning("shutdown.position_records_save_failed (non-fatal): %s", _exc)
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
        summary = dict(
            self._startup_recovery_status.get("summary")
            if isinstance(self._startup_recovery_status.get("summary"), dict)
            else {}
        )
        # Inject position-restore counters so readyz and release evidence can gate on them.
        summary["position_records_loaded"] = self._position_records_loaded
        summary["startup_ownership_gap_detected"] = self._startup_ownership_gap_detected
        return {
            "status": self._startup_recovery_status.get("status"),
            "reason": self._startup_recovery_status.get("reason"),
            "summary": summary,
        }

    def release_evidence_snapshot(self) -> dict[str, Any]:
        """Return a structured JSON-serialisable release-evidence bundle.

        Captures the authoritative proof that a LIVE backend started safely:
        trade mode, authority path, runner counts, position restore status,
        outbox recovery summary, schema guard status, and key safety flags.
        Operators must review this bundle before approving a LIVE deployment.
        """
        import os as _os
        evidence: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "trade_mode": str(_os.getenv("TRADE_MODE", "unknown")).upper(),
            "app_env": str(_os.getenv("APP_ENV", "unknown")),
            "hub_instance": str(_os.getenv("HUB_INSTANCE_NAME", "unknown")),
        }

        # Runtime readiness
        evidence["runtime_ready"] = bool(self._ready)
        evidence["is_leader"] = bool(self._is_leader)
        evidence["position_authority_restored"] = bool(self._position_authority_restored)

        # Operating mode (authority path)
        evidence["operating_mode"] = self.operating_mode_status()

        # Schema guard
        evidence["schema_guard"] = self.schema_status()

        # Outbox recovery summary
        evidence["startup_recovery"] = self.startup_recovery_status()

        # Stream worker state
        evidence["stream_worker"] = self.stream_worker_status()

        # Runner counts
        try:
            runtime = self._hub_runtime_getter()
            hub = getattr(runtime, "hub", None)
            if hub is not None:
                runners = getattr(hub, "_runners", {})
                fallback_runner_count = (
                    len(runners) if isinstance(runners, dict) else 0
                )
                registered = int(
                    getattr(
                        hub,
                        "registered_runner_count",
                        getattr(hub, "runner_count", fallback_runner_count),
                    )
                    or 0
                )
                running = int(getattr(hub, "running_runner_count", 0) or 0)
                failed = int(getattr(hub, "failed_runner_count", 0) or 0)
                runner_ids_getter = getattr(hub, "list_runner_ids", None)
                if callable(runner_ids_getter):
                    runner_ids = [str(runner_id) for runner_id in runner_ids_getter()]
                elif isinstance(runners, dict):
                    runner_ids = [str(runner_id) for runner_id in runners.keys()]
                else:
                    runner_ids = []

                evidence["runner_count"] = registered
                evidence["registered_runner_count"] = registered
                evidence["running_runner_count"] = running
                evidence["failed_runner_count"] = failed
                evidence["runner_ids"] = runner_ids
        except Exception:
            evidence["runner_count"] = -1

        # Position records loaded
        try:
            runtime = self._hub_runtime_getter()
            ol = getattr(runtime, "order_lifecycle", None)
            if ol is not None:
                pos_recs = getattr(ol, "_position_records", {})
                evidence["position_records_in_memory"] = len(pos_recs)
                non_flat = sum(
                    1 for r in pos_recs.values()
                    if str(getattr(r, "position_state", "NONE")) not in ("PositionState.FLAT", "FLAT", "NONE", "PositionState.NONE")
                )
                evidence["position_records_active"] = non_flat
        except Exception:
            evidence["position_records_in_memory"] = -1
        try:
            from app.orders.position_record_invariants import (
                count_terminal_nonzero_position_records,
            )

            evidence["position_record_invariants"] = {
                "terminal_nonzero_net_qty_count": (
                    count_terminal_nonzero_position_records()
                ),
            }
        except Exception as exc:
            evidence["position_record_invariants"] = {
                "terminal_nonzero_net_qty_count": None,
                "error": str(exc),
            }

        # Kill switch state
        try:
            from app.risk.kill_switch import get_kill_switch_state
            evidence["kill_switch"] = get_kill_switch_state()
        except Exception:
            evidence["kill_switch"] = {"source": "unavailable"}

        # Synthetic contamination: just presence of the startup guard result
        evidence["synthetic_contamination_cleared"] = bool(
            str(self._startup_recovery_status.get("status", "")).lower() != "failed"
        )

        # Postgres transport security (§99)
        _pg_sslmode = str(_os.getenv("CONTROL_PLANE_PG_SSLMODE", "prefer") or "prefer").lower()
        _ssl_skip = str(_os.getenv("LIVE_PG_SSL_SKIP_CHECK", "false") or "false").lower() in ("1", "true", "yes")
        evidence["postgres_transport"] = {
            "sslmode": _pg_sslmode,
            "ssl_enforced": _pg_sslmode in ("require", "verify-ca", "verify-full"),
            "live_pg_ssl_skip_check": _ssl_skip,
            "transport_note": (
                "local-only-bypass-approved" if _ssl_skip else
                ("encrypted" if _pg_sslmode in ("require", "verify-ca", "verify-full") else "prefer-may-be-plaintext")
            ),
        }

        # Safety flags from env
        evidence["safety_flags"] = {
            "enable_capital_checks": str(_os.getenv("ENABLE_CAPITAL_CHECKS", "")).lower() == "true",
            "enable_risk_checks": str(_os.getenv("ENABLE_RISK_CHECKS", "")).lower() == "true",
            "order_router_enforce_global_kill_switch": str(
                _os.getenv("ORDER_ROUTER_ENFORCE_GLOBAL_KILL_SWITCH", "")
            ).lower() == "true",
            "order_submission_outbox_required": str(
                _os.getenv("ORDER_SUBMISSION_OUTBOX_REQUIRED", "")
            ).lower() == "true",
            "position_ownership_enabled": str(
                _os.getenv("POSITION_OWNERSHIP_ENABLED", "")
            ).lower() == "true",
            "disable_trading_window_filter": str(
                _os.getenv("DISABLE_TRADING_WINDOW_FILTER", "")
            ).lower() != "false",
            "risk_fail_open_on_missing_pnl": str(
                _os.getenv("RISK_FAIL_OPEN_ON_MISSING_PNL", "true")
            ).lower() == "true",
        }

        return evidence

    async def _mark_recovery_pending(
        self, runtime: object, *, trade_mode: str = "PAPER"
    ) -> None:
        """Mark all restored lifecycle contexts and ownership records as
        RECOVERY_PENDING before broker reconciliation (Architecture §11.1).

        §100: In LIVE mode, failures to mark recovery-pending are fatal
        because they mean the broker reconciliation phase runs against an
        unfenced state graph — positions or ownership records that should
        be gated as RECOVERY_PENDING are silently treated as authoritative.
        """
        logger.info(
            "startup.recovery_pending_marking: marking restored state before broker reconciliation"
        )
        lifecycle_marked = 0
        ownership_marked = 0
        _live = trade_mode == "LIVE"

        # Mark order lifecycle contexts
        order_lifecycle = getattr(runtime, "order_lifecycle", None)
        if order_lifecycle is not None:
            mark_fn = getattr(order_lifecycle, "mark_recovery_pending", None)
            if callable(mark_fn):
                try:
                    result = mark_fn()
                    if hasattr(result, "__await__"):
                        result = await result
                    if isinstance(result, int):
                        lifecycle_marked = result
                except Exception as exc:
                    if _live:
                        logger.error(
                            "startup.recovery_pending_mark_failed: LIVE startup cannot proceed — "
                            "order lifecycle RECOVERY_PENDING marking failed: %s", exc
                        )
                        raise RuntimeError(
                            f"LIVE startup aborted: failed to mark order lifecycle "
                            f"RECOVERY_PENDING before broker reconciliation: {exc}"
                        ) from exc
                    logger.warning(
                        "Failed to mark order lifecycle RECOVERY_PENDING: %s", exc
                    )

        # §124 / Issue #3: Pre-load ownership records from Postgres into memory before
        # calling mark_all_recovery_pending().  mark_all_recovery_pending() iterates
        # _ownership_records which is populated lazily — at startup it is always empty
        # unless we explicitly trigger _ensure_account_loaded() for each active account.
        # Without this step the mark always returns 0, firing a false ownership gap on
        # every restart that has any non-terminal position records in the DB.
        position_ownership = getattr(runtime, "position_ownership_store", None)
        if position_ownership is not None:
            ensure_loaded_fn = getattr(position_ownership, "_ensure_account_loaded", None)
            if callable(ensure_loaded_fn):
                try:
                    hub = getattr(runtime, "hub", None)
                    runner_ids = []
                    if hub is not None:
                        list_fn = getattr(hub, "list_runner_ids", None)
                        if callable(list_fn):
                            runner_ids = list(list_fn())
                    from app.core.identifiers import BrokerAccountId, TenantId
                    for broker_account_id in runner_ids:
                        runner = hub.get_runner(BrokerAccountId(str(broker_account_id)))
                        if runner is None:
                            continue
                        tenant_id = getattr(runner, "tenant_id", None)
                        if not tenant_id:
                            continue
                        ensure_loaded_fn(
                            tenant_id=TenantId(str(tenant_id)),
                            broker_account_id=BrokerAccountId(str(broker_account_id)),
                        )
                    if runner_ids:
                        logger.info(
                            "startup.ownership_preload: pre-loaded ownership store for %d "
                            "account(s) before recovery-pending marking",
                            len(runner_ids),
                        )
                except Exception as exc:
                    if _live:
                        logger.error(
                            "startup.ownership_preload_failed: LIVE startup cannot proceed — "
                            "ownership store pre-load failed: %s", exc
                        )
                        raise RuntimeError(
                            f"LIVE startup aborted: failed to pre-load ownership store "
                            f"before recovery-pending marking: {exc}"
                        ) from exc
                    logger.warning(
                        "startup.ownership_preload_failed (non-fatal): %s", exc
                    )

        # Mark position ownership records
        if position_ownership is not None:
            mark_all_fn = getattr(position_ownership, "mark_all_recovery_pending", None)
            if callable(mark_all_fn):
                try:
                    result = mark_all_fn()
                    if hasattr(result, "__await__"):
                        result = await result
                    if isinstance(result, int):
                        ownership_marked = result
                except Exception as exc:
                    if _live:
                        logger.error(
                            "startup.recovery_pending_mark_failed: LIVE startup cannot proceed — "
                            "position ownership RECOVERY_PENDING marking failed: %s", exc
                        )
                        raise RuntimeError(
                            f"LIVE startup aborted: failed to mark position ownership "
                            f"RECOVERY_PENDING before broker reconciliation: {exc}"
                        ) from exc
                    logger.warning(
                        "Failed to mark position ownership RECOVERY_PENDING: %s", exc
                    )

        # §107: Detect ownership gap — non-terminal position records with no
        # corresponding ownership records.  This means positions exist in the
        # authoritative store but the ownership layer has nothing to fence.
        # EOD exit and hub router exit rely on ownership records; missing ones
        # mean those positions cannot be safely managed.
        if self._position_records_loaded > 0 and ownership_marked == 0:
            self._startup_ownership_gap_detected = True
            logger.error(
                "startup.ownership_gap_detected: %d non-terminal position record(s) "
                "restored from Postgres but 0 ownership records were marked "
                "RECOVERY_PENDING.  These positions are untracked by the ownership "
                "layer (EOD exits, hub router exits, and ATM remap may not cover them). "
                "Investigate internal_position_records table before accepting orders.",
                self._position_records_loaded,
            )
        else:
            self._startup_ownership_gap_detected = False

        logger.info(
            "startup.recovery_pending_marked: lifecycle_contexts=%d ownership_records=%d"
            " ownership_gap_detected=%s",
            lifecycle_marked,
            ownership_marked,
            self._startup_ownership_gap_detected,
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
            "startup.outbox_evaluated: strict_mode=%s degraded=%s reason=%s summary=%s",
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
