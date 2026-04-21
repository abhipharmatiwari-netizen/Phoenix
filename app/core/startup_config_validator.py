from __future__ import annotations

import logging
from datetime import time as dt_time
from typing import Any, Dict, Iterable, Mapping

from app.core.lot_size import lot_size_for_symbol_optional
from app.core.broker_network_identity import live_broker_network_identity_env_errors
from app.core.operating_mode import (
    OperatingMode,
    resolve_operating_mode_from_runtime,
)
from app.core.underlyings import canonical_underlying_key, underlying_lookup_candidates
from app.runners.stream_indicator_plan import build_indicator_engine_plan
from app.strategies.ema20_config import EMA20_STRATEGY_NAMES, build_ema20_config
from app.strategies.naming import canonicalize_strategy_name

logger = logging.getLogger(__name__)
_LOCAL_APP_ENVS = {"local", "dev", "test"}
_MOCK_SWEEP_STATE_ESCAPE_HATCH_ENVS = ("ALLOW_MOCK_SWEEP_STATE",)
_LIVE_CAPITAL_CHECKS_DISABLED_OVERRIDE_ENV = "ALLOW_LIVE_CAPITAL_CHECKS_DISABLED"


def _normalize_strategy_entries(entries: Any) -> list[Dict[str, Any]]:
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    if isinstance(entries, dict):
        out: list[Dict[str, Any]] = []
        for name, cfg in entries.items():
            if not isinstance(cfg, dict):
                continue
            row = dict(cfg)
            row.setdefault("name", name)
            out.append(row)
        return out
    return []


def _iter_instruments(items: Any) -> Iterable[Dict[str, Any]]:
    for item in items or []:
        if isinstance(item, str):
            yield {"label": item}
            continue
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("underlying") or item.get("instrument")
        if not label:
            continue
        out = dict(item)
        out["label"] = str(label)
        yield out


def _parse_hhmm(value: str) -> bool:
    try:
        hour_txt, minute_txt = str(value).strip().split(":", 1)
        hour = int(hour_txt)
        minute = int(minute_txt)
    except Exception:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _positive_float(value: object, *, minimum: float = 0.0) -> bool:
    try:
        parsed = float(value)
    except Exception:
        return False
    return parsed > minimum


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalized_backend(value: object) -> str:
    return str(value or "").strip().lower()


def _configured_mock_sweep_state_escape_hatches(
    env: Mapping[str, Any],
    *,
    include_disabled: bool = False,
) -> list[str]:
    configured: list[str] = []
    for name in _MOCK_SWEEP_STATE_ESCAPE_HATCH_ENVS:
        if name not in env:
            continue
        if include_disabled or _truthy(env.get(name)):
            configured.append(name)
    return configured


def _ema20_underlying_key(underlying_label: str) -> str:
    return canonical_underlying_key(underlying_label)


def _ema20_expected_lot_size(underlying_label: str) -> int | None:
    underlying_key = _ema20_underlying_key(underlying_label)
    if not underlying_key:
        return None
    for candidate in underlying_lookup_candidates(underlying_key):
        lot_size = lot_size_for_symbol_optional(candidate)
        if lot_size:
            return int(lot_size)
    return None


def _resolve_ema20_config_value(
    env: Mapping[str, Any],
    *,
    underlying_label: str,
) -> tuple[str | None, int | None]:
    underlying_key = _ema20_underlying_key(underlying_label)
    keys = [
        f"{underlying_key}_EMA20_LOTS",
        "EMA20_LOTS",
        f"{underlying_key}_EMA20_QTY",
        "EMA20_QTY",
    ]
    for key in keys:
        raw = str(env.get(key, "") or "").strip()
        if not raw:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return key, parsed
    return None, None


def _validate_live_ema20_quantity_settings(
    *,
    strategy_cfg: Mapping[str, Any],
    env: Mapping[str, Any],
    ema20_names: set[str],
) -> list[str]:
    if _truthy(env.get("ALLOW_LEGACY_EMA20_QTY_IN_LIVE")):
        return []

    underlyings: set[str] = set()
    for row in _normalize_strategy_entries(strategy_cfg.get("strategies", [])):
        if not bool(row.get("enabled", True)):
            continue
        name_raw = str(row.get("name", "")).strip()
        name = canonicalize_strategy_name(
            name_raw,
            source="startup_validator.live_ema20_qty",
            warn_alias=False,
        )
        if name not in ema20_names:
            continue
        for inst in _iter_instruments(row.get("instruments", [])):
            label = str(inst.get("label") or "").strip().upper()
            if label:
                underlyings.add(label)

    errors: list[str] = []
    for underlying_label in sorted(underlyings):
        selected_key, selected_value = _resolve_ema20_config_value(
            env,
            underlying_label=underlying_label,
        )
        if selected_key not in {
            "EMA20_QTY",
            f"{_ema20_underlying_key(underlying_label)}_EMA20_QTY",
        }:
            continue
        expected_lot_size = _ema20_expected_lot_size(underlying_label)
        if expected_lot_size is None or selected_value != expected_lot_size:
            continue
        canonical_key = f"{_ema20_underlying_key(underlying_label)}_EMA20_LOTS"
        errors.append(
            "TRADE_MODE=LIVE rejects legacy "
            f"{selected_key}={selected_value} for {underlying_label} because it matches "
            f"the known lot size {expected_lot_size}. Use {canonical_key}=1 "
            "or set ALLOW_LEGACY_EMA20_QTY_IN_LIVE=true for the compatibility window."
        )
    return errors


def _positive_int_or_default(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _validate_put_momentum_indicator_footprint(
    *,
    strategy_cfg: Mapping[str, Any],
    trade_mode: str,
) -> list[str]:
    mode = str(trade_mode or "").strip().upper()
    plan = build_indicator_engine_plan(dict(strategy_cfg or {}))
    available: dict[int, set[int]] = {}
    for timeframe_seconds, periods in (plan.ema_periods_by_timeframe or {}).items():
        try:
            timeframe_key = int(timeframe_seconds)
        except (TypeError, ValueError):
            continue
        normalized_periods: set[int] = set()
        for period in periods or []:
            try:
                period_value = int(period)
            except (TypeError, ValueError):
                continue
            if period_value > 0:
                normalized_periods.add(period_value)
        available[timeframe_key] = normalized_periods

    messages: list[str] = []
    for row in _normalize_strategy_entries(strategy_cfg.get("strategies", [])):
        if not bool(row.get("enabled", True)):
            continue
        name = canonicalize_strategy_name(
            row.get("name"),
            source="startup_validator.put_momentum",
            warn_alias=False,
        )
        if name != "put_momentum_scalper":
            continue
        base_params = row.get("params", {})
        if not isinstance(base_params, dict):
            base_params = {}
        for inst in _iter_instruments(row.get("instruments", [])):
            underlying = str(inst.get("label") or "").strip().upper()
            if not underlying:
                continue
            merged_params = dict(base_params)
            merged_params.update({k: v for k, v in inst.items() if k != "label"})
            tf5 = _positive_int_or_default(
                merged_params.get("timeframe_seconds_5m"),
                300,
            )
            tf15 = _positive_int_or_default(
                merged_params.get("timeframe_seconds_15m"),
                900,
            )
            missing_specs: list[str] = []
            for timeframe_seconds, ema_period in ((tf5, 20), (tf5, 50), (tf15, 20)):
                periods = available.get(int(timeframe_seconds), set())
                if int(ema_period) not in periods:
                    missing_specs.append(
                        f"{int(timeframe_seconds)}s ema_{int(ema_period)}"
                    )
            if not missing_specs:
                continue
            messages.append(
                "put_momentum_scalper "
                f"{underlying} requires indicator footprint "
                f"{', '.join(missing_specs)} but the effective engine plan only has "
                f"{plan.ema_periods_by_timeframe!r}. Ensure 5m ema_20/ema_50 and 15m ema_20 "
                "are enabled in the shared indicator engine."
            )

    if messages and mode != "LIVE":
        logger.warning(
            "Startup indicator footprint warning for put_momentum_scalper | trade_mode=%s details=%s",
            mode or "UNKNOWN",
            messages,
        )
        return []
    return messages


def validate_runtime_startup_settings(
    *,
    settings: Any,
    runtime_cfg: Any,
    env: Mapping[str, Any],
) -> None:
    errors: list[str] = []

    eod_exit_time = str(getattr(settings, "eod_exit_time", "") or "").strip()
    if eod_exit_time and not _parse_hhmm(eod_exit_time):
        errors.append(f"EOD_EXIT_TIME must be HH:MM, got '{eod_exit_time}'")
    eod_cancel_open_orders_configured = hasattr(settings, "eod_cancel_open_orders_enabled")
    eod_retry_cutoff = str(getattr(settings, "eod_exit_retry_cutoff_time", "") or "").strip()
    if eod_retry_cutoff and not _parse_hhmm(eod_retry_cutoff):
        errors.append(
            f"EOD_EXIT_RETRY_CUTOFF_TIME must be HH:MM, got '{eod_retry_cutoff}'"
        )

    hub_interval = getattr(settings, "hub_subscription_poll_interval", None)
    if not _positive_float(hub_interval, minimum=0.0):
        errors.append(
            f"HUB_SUBSCRIPTION_POLL_INTERVAL must be > 0, got {hub_interval}"
        )

    watchdog_interval = getattr(runtime_cfg, "stream_watchdog_interval_seconds", None)
    if not _positive_float(watchdog_interval, minimum=0.0):
        errors.append(
            f"STREAM_WATCHDOG_INTERVAL must be > 0, got {watchdog_interval}"
        )
    watchdog_backoff_base = getattr(
        runtime_cfg,
        "stream_watchdog_restart_backoff_base_seconds",
        None,
    )
    if not _positive_float(watchdog_backoff_base, minimum=0.0):
        errors.append(
            "STREAM_WATCHDOG_RESTART_BACKOFF_BASE_SECONDS must be > 0"
        )
    watchdog_backoff_max = getattr(
        runtime_cfg,
        "stream_watchdog_restart_backoff_max_seconds",
        None,
    )
    if not _positive_float(watchdog_backoff_max, minimum=0.0):
        errors.append(
            "STREAM_WATCHDOG_RESTART_BACKOFF_MAX_SECONDS must be > 0"
        )
    jitter_ratio = getattr(
        runtime_cfg,
        "stream_watchdog_restart_backoff_jitter_ratio",
        None,
    )
    try:
        jitter_ratio_val = float(jitter_ratio)
    except (TypeError, ValueError):
        jitter_ratio_val = -1.0
    if jitter_ratio is None or not (0.0 <= jitter_ratio_val <= 1.0):
        errors.append(
            "STREAM_WATCHDOG_RESTART_BACKOFF_JITTER_RATIO must be between 0 and 1"
        )
    stable_window = getattr(
        runtime_cfg,
        "stream_watchdog_stable_run_window_seconds",
        None,
    )
    try:
        stable_window_val = float(stable_window)
    except (TypeError, ValueError):
        stable_window_val = -1.0
    if stable_window is None or stable_window_val < 0.0:
        errors.append(
            "STREAM_WATCHDOG_STABLE_RUN_WINDOW_SECONDS must be >= 0"
        )

    position_sync_interval = env.get("POSITION_SYNC_INTERVAL_SECONDS", "30")
    if not _positive_float(position_sync_interval, minimum=0.0):
        errors.append(
            "POSITION_SYNC_INTERVAL_SECONDS must be > 0"
        )
    orders_sync_interval = env.get("ORDERS_SYNC_INTERVAL_SECONDS", "90")
    if not _positive_float(orders_sync_interval, minimum=0.0):
        errors.append(
            "ORDERS_SYNC_INTERVAL_SECONDS must be > 0"
        )

    risk_enabled = bool(getattr(settings, "risk_enable_daily_loss", False))
    risk_max_daily_loss = getattr(settings, "risk_max_daily_loss", None)
    if risk_enabled and not _positive_float(risk_max_daily_loss, minimum=0.0):
        errors.append(
            "RISK_ENABLE_DAILY_LOSS=true requires RISK_MAX_DAILY_LOSS > 0"
        )

    app_env = str(
        getattr(runtime_cfg, "app_env", None)
        or env.get("APP_ENV")
        or env.get("ENV")
        or "local"
    ).strip().lower()
    dashboard_auth_disabled = str(
        env.get(
            "DASHBOARD_AUTH_DISABLED",
            getattr(runtime_cfg, "dashboard_auth_disabled", False),
        )
    ).strip().lower() in {"1", "true", "yes", "on"}
    admin_api_key = str(getattr(settings, "admin_api_key", None) or "").strip()
    dashboard_hmac_enabled = bool(
        getattr(settings, "dashboard_hmac_auth_enabled", False)
    )
    dashboard_hmac_secret = str(
        getattr(settings, "dashboard_hmac_secret", None) or ""
    ).strip()
    enabled_mock_sweep_state_escape_hatches = _configured_mock_sweep_state_escape_hatches(
        env
    )
    configured_mock_sweep_state_escape_hatches = (
        _configured_mock_sweep_state_escape_hatches(
            env,
            include_disabled=True,
        )
    )
    if app_env not in _LOCAL_APP_ENVS:
        if dashboard_auth_disabled:
            errors.append(
                "DASHBOARD_AUTH_DISABLED must be false outside local/dev/test"
            )
        if not admin_api_key:
            errors.append(
                "ADMIN_API_KEY must be configured outside local/dev/test"
            )
        if dashboard_hmac_enabled and not dashboard_hmac_secret:
            errors.append(
                "DASHBOARD_HMAC_AUTH_ENABLED=true requires DASHBOARD_HMAC_SECRET"
            )
        if enabled_mock_sweep_state_escape_hatches:
            errors.append(
                f"APP_ENV={app_env} forbids enabling mock sweep/EOD state escape hatches: "
                + ", ".join(enabled_mock_sweep_state_escape_hatches)
            )

    trade_mode = str(env.get("TRADE_MODE", "PAPER") or "PAPER").strip().upper()
    require_live = str(env.get("REQUIRE_LIVE_TRADE_MODE", "") or "").strip().lower()
    if require_live in {"1", "true", "yes", "on"} and trade_mode != "LIVE":
        errors.append(
            f"REQUIRE_LIVE_TRADE_MODE=true but TRADE_MODE={trade_mode}; "
            "this compose stack requires TRADE_MODE=LIVE"
        )
    if trade_mode == "LIVE":
        errors.extend(live_broker_network_identity_env_errors(env))
        if configured_mock_sweep_state_escape_hatches:
            errors.append(
                "TRADE_MODE=LIVE forbids mock sweep/EOD state escape hatches: "
                + ", ".join(configured_mock_sweep_state_escape_hatches)
            )
        operating_mode = resolve_operating_mode_from_runtime(
            settings=settings,
            runtime_cfg=runtime_cfg,
        )
        leader_lease_backend = str(
            getattr(runtime_cfg, "leader_lease_backend", "") or ""
        ).strip().lower()
        if not leader_lease_backend:
            leader_lease_backend = (
                "postgres"
                if str(getattr(settings, "control_plane_backend", "")).strip().lower()
                == "postgres"
                else "firestore"
            )
        allow_live_capital_checks_disabled = _truthy(
            env.get(_LIVE_CAPITAL_CHECKS_DISABLED_OVERRIDE_ENV)
        )
        live_summary = {
            "control_plane_backend": str(
                getattr(settings, "control_plane_backend", "")
            ).strip(),
            "sweep_state_backend": str(
                getattr(settings, "sweep_state_backend", "")
            ).strip(),
            "broker_secret_backend": str(
                getattr(settings, "broker_secret_backend", "")
            ).strip(),
            "enable_capital_checks": bool(
                getattr(settings, "enable_capital_checks", False)
            ),
            "allow_live_capital_checks_disabled": allow_live_capital_checks_disabled,
            "enable_risk_checks": bool(
                getattr(settings, "enable_risk_checks", False)
            ),
            "risk_enable_daily_loss": bool(
                getattr(settings, "risk_enable_daily_loss", False)
            ),
            "enable_profit_checks": bool(
                getattr(settings, "enable_profit_checks", False)
            ),
            "profit_enable_daily_target": bool(
                getattr(settings, "profit_enable_daily_target", False)
            ),
            "order_router_enforce_idempotency": bool(
                getattr(settings, "order_router_enforce_idempotency", False)
            ),
            "position_ownership_enabled": bool(
                getattr(settings, "position_ownership_enabled", False)
            ),
            "order_lifecycle_persist_markers_required": bool(
                getattr(settings, "order_lifecycle_persist_markers_required", False)
            ),
            "capital_fail_closed_on_missing_state": bool(
                getattr(settings, "capital_fail_closed_on_missing_state", False)
            ),
            "capital_fail_closed_on_missing_notional_price": bool(
                getattr(
                    settings,
                    "capital_fail_closed_on_missing_notional_price",
                    False,
                )
            ),
            "risk_fail_open_on_missing_pnl": bool(
                getattr(settings, "risk_fail_open_on_missing_pnl", False)
            ),
            "position_ownership_unknown_mode": str(
                getattr(settings, "position_ownership_unknown_mode", "")
            ).strip(),
            "enable_multi_hub": bool(
                getattr(settings, "enable_multi_hub", False)
            ),
            "use_hub_router": bool(
                getattr(settings, "use_hub_router", False)
            ),
            "disable_stream_worker": bool(
                getattr(runtime_cfg, "disable_stream_worker", False)
            ),
            "leader_lease_enabled": bool(
                getattr(runtime_cfg, "leader_lease_enabled_override", False)
            ),
            "leader_lease_backend": leader_lease_backend,
            "leader_lease_id": str(
                getattr(runtime_cfg, "leader_lease_id", "") or ""
            ).strip()
            or "phoenix-live-single-stack",
            "operating_mode": (
                operating_mode.mode.value
                if operating_mode.mode is not None
                else None
            ),
            "authority_path": operating_mode.authority_path,
            "operating_mode_reason": operating_mode.reason,
            "demo_auth_requested": bool(
                getattr(runtime_cfg, "demo_auth_requested", False)
            ),
            "enable_demo_auth": bool(
                getattr(runtime_cfg, "enable_demo_auth", False)
            ),
        }
        logger.info("LIVE startup safety summary: %s", live_summary)

        if not bool(getattr(settings, "enable_capital_checks", False)):
            errors.append("TRADE_MODE=LIVE requires ENABLE_CAPITAL_CHECKS=true")

        capital_limits_raw = str(getattr(settings, "capital_limits_json", "") or "").strip()
        if capital_limits_raw in ("", "{}", "null"):
            logger.warning(
                "startup.capital_limits_warning: TRADE_MODE=LIVE but CAPITAL_LIMITS_JSON is empty "
                "or not set. Per-account notional/exposure overrides are inactive — global defaults "
                "(max_notional_per_order=5L, max_gross_exposure=10L) apply to all accounts. "
                "Set CAPITAL_LIMITS_JSON with per-account limits appropriate for the funded account size."
            )

        required_true_flags = {
            "ENABLE_RISK_CHECKS": getattr(settings, "enable_risk_checks", False),
            "RISK_ENABLE_DAILY_LOSS": getattr(settings, "risk_enable_daily_loss", False),
            "ENABLE_PROFIT_CHECKS": getattr(settings, "enable_profit_checks", False),
            "PROFIT_ENABLE_DAILY_TARGET": getattr(
                settings, "profit_enable_daily_target", False
            ),
            "ORDER_ROUTER_ENFORCE_IDEMPOTENCY": getattr(
                settings, "order_router_enforce_idempotency", False
            ),
            "POSITION_OWNERSHIP_ENABLED": getattr(
                settings, "position_ownership_enabled", False
            ),
            "ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED": getattr(
                settings, "order_lifecycle_persist_markers_required", False
            ),
            "CAPITAL_FAIL_CLOSED_ON_MISSING_STATE": getattr(
                settings, "capital_fail_closed_on_missing_state", False
            ),
            "CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE": getattr(
                settings, "capital_fail_closed_on_missing_notional_price", False
            ),
        }
        for flag_name, enabled in required_true_flags.items():
            if not bool(enabled):
                errors.append(f"TRADE_MODE=LIVE requires {flag_name}=true")

        if str(getattr(settings, "control_plane_backend", "")).strip().lower() != "postgres":
            errors.append("TRADE_MODE=LIVE requires CONTROL_PLANE_BACKEND=postgres")
        if str(getattr(settings, "sweep_state_backend", "")).strip().lower() != "postgres":
            errors.append("TRADE_MODE=LIVE requires SWEEP_STATE_BACKEND=postgres")
        if getattr(runtime_cfg, "leader_lease_enabled_override", None) is not True:
            errors.append(
                "TRADE_MODE=LIVE requires LEADER_LEASE_ENABLED=true "
                "so exactly one Phoenix LIVE stack becomes authoritative against the target Postgres"
            )
        if leader_lease_backend != "postgres":
            errors.append(
                "TRADE_MODE=LIVE requires LEADER_LEASE_BACKEND=postgres "
                "so single-stack authority is enforced at the shared control-plane database"
            )
        if operating_mode.is_ambiguous:
            errors.append(
                "TRADE_MODE=LIVE requires ENABLE_MULTI_HUB and USE_HUB_ROUTER to resolve exactly one authority path"
            )
        elif operating_mode.mode == OperatingMode.LEGACY_AUTHORITATIVE:
            errors.append(
                "TRADE_MODE=LIVE rejects legacy authoritative mode because kill-switch durability is still local-file only; enable hub-authoritative routing"
            )
        if bool(getattr(runtime_cfg, "disable_stream_worker", False)):
            errors.append(
                "TRADE_MODE=LIVE requires DISABLE_STREAM_WORKER=false until an approved replacement live market-data plane is implemented"
            )
        if str(env.get("ORDER_SUBMISSION_OUTBOX_ENABLED", "true")).strip().lower() in {
            "0",
            "false",
            "no",
        }:
            errors.append(
                "TRADE_MODE=LIVE requires ORDER_SUBMISSION_OUTBOX_ENABLED=true"
            )
        if str(env.get("ORDER_SUBMISSION_OUTBOX_REQUIRED", "true")).strip().lower() in {
            "0",
            "false",
            "no",
        }:
            errors.append(
                "TRADE_MODE=LIVE requires ORDER_SUBMISSION_OUTBOX_REQUIRED=true"
            )
        outbox_backend = str(
            env.get(
                "ORDER_SUBMISSION_OUTBOX_BACKEND",
                getattr(settings, "control_plane_backend", ""),
            )
            or ""
        ).strip().lower()
        if outbox_backend not in {"postgres", "pg", "db", "database"}:
            errors.append(
                "TRADE_MODE=LIVE requires ORDER_SUBMISSION_OUTBOX_BACKEND=postgres"
            )
        secret_backend = _normalized_backend(
            getattr(settings, "broker_secret_backend", env.get("BROKER_SECRET_BACKEND"))
        )
        _live_secret_approved = {
            "secret_manager",
            "secretmanager",
            "postgres",
            "pg",
            "db",
            "database",
        }
        if secret_backend not in _live_secret_approved:
            errors.append(
                "TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND to resolve to secret_manager or postgres"
            )
        demo_auth_requested = bool(getattr(runtime_cfg, "demo_auth_requested", False))
        demo_auth_enabled = bool(getattr(runtime_cfg, "enable_demo_auth", False))
        if demo_auth_requested or demo_auth_enabled or _truthy(env.get("ENABLE_DEMO_AUTH")):
            errors.append("TRADE_MODE=LIVE requires ENABLE_DEMO_AUTH=false")
        if bool(getattr(settings, "risk_fail_open_on_missing_pnl", False)):
            errors.append("TRADE_MODE=LIVE requires RISK_FAIL_OPEN_ON_MISSING_PNL=false")
        if str(getattr(settings, "position_ownership_unknown_mode", "")).strip().lower() != "block_entries":
            errors.append(
                "TRADE_MODE=LIVE requires POSITION_OWNERSHIP_UNKNOWN_MODE=block_entries"
            )
        if not bool(getattr(settings, "ownership_persist_pending_locks", False)):
            errors.append(
                "TRADE_MODE=LIVE requires OWNERSHIP_PERSIST_PENDING_LOCKS=true "
                "so in-flight PENDING_LOCK entries survive restarts and prevent duplicate-entry windows"
            )
        if not _positive_float(getattr(settings, "risk_max_daily_loss", None), minimum=0.0):
            errors.append("TRADE_MODE=LIVE requires RISK_MAX_DAILY_LOSS > 0")
        if not _positive_float(getattr(settings, "profit_daily_target", None), minimum=0.0):
            errors.append("TRADE_MODE=LIVE requires PROFIT_DAILY_TARGET > 0")

        # Gate 6: EOD policy per-strategy validation
        if not eod_exit_time:
            errors.append(
                "TRADE_MODE=LIVE requires EOD_EXIT_TIME to be set (HH:MM)"
            )
        if not eod_cancel_open_orders_configured:
            errors.append(
                "TRADE_MODE=LIVE requires EOD_CANCEL_OPEN_ORDERS_ENABLED to be explicitly configured"
            )

        # Gate 7: Circuit-breaker durability — circuit breaker state must be
        # persisted to a Postgres backend so it survives restarts.
        # Note: this checks TradingCircuitBreaker (PostgresCircuitBreakerStateStore),
        # NOT the KillSwitchManager which has its own persistence path (migration 007).
        cb_persist = bool(getattr(settings, "circuit_breaker_persist_state", False))
        if not cb_persist:
            errors.append(
                "TRADE_MODE=LIVE requires CIRCUIT_BREAKER_PERSIST_STATE=true "
                "so circuit-breaker state survives restarts (postgres-backed)"
            )

        # Gate 8: Circuit-breaker scope isolation — HUB_INSTANCE_NAME must be
        # non-empty so each deployment gets its own circuit-breaker partition key.
        # Without it scope_id falls through to "default", which is shared across
        # all stacks on the same Postgres instance.
        hub_instance_name = str(getattr(settings, "hub_instance_name", "") or "").strip()
        if not hub_instance_name:
            errors.append(
                "TRADE_MODE=LIVE requires HUB_INSTANCE_NAME to be set to a non-empty string "
                "so the circuit-breaker state is scoped to this deployment instance "
                "(prevents state bleed across stacks sharing the same Postgres)"
            )

        # Gate: DASHBOARD_AUTH_DISABLED must be false in LIVE
        if dashboard_auth_disabled:
            errors.append(
                "TRADE_MODE=LIVE requires DASHBOARD_AUTH_DISABLED=false"
            )

    if errors:
        details = "\n".join(f"- {e}" for e in errors)
        raise ValueError(f"Startup runtime setting validation failed:\n{details}")


def validate_startup_config(
    *,
    strategy_cfg: Mapping[str, Any],
    trade_mode: str,
    disable_trading_window_filter: bool,
    known_strategy_names: Iterable[str],
    env: Mapping[str, Any] | None = None,
) -> None:
    errors: list[str] = []
    env_map = env or {}
    mode = str(trade_mode).strip().upper()
    if mode not in {"PAPER", "LIVE", "SHADOW"}:
        errors.append(
            f"TRADE_MODE must be one of PAPER|LIVE|SHADOW, got '{trade_mode}'"
        )
    known = {str(x).strip() for x in known_strategy_names if str(x).strip()}
    ema20_names = {str(name).strip() for name in EMA20_STRATEGY_NAMES if str(name).strip()}

    if mode == "LIVE" and bool(disable_trading_window_filter):
        errors.append(
            "TRADE_MODE=LIVE requires DISABLE_TRADING_WINDOW_FILTER=false"
        )
    if mode == "LIVE":
        errors.extend(
            _validate_live_ema20_quantity_settings(
                strategy_cfg=strategy_cfg,
                env=env_map,
                ema20_names=ema20_names,
            )
        )
    errors.extend(
        _validate_put_momentum_indicator_footprint(
            strategy_cfg=strategy_cfg,
            trade_mode=mode,
        )
    )

    entries = _normalize_strategy_entries(strategy_cfg.get("strategies", []))
    for row in entries:
        name_raw = str(row.get("name", "")).strip()
        if not name_raw:
            continue
        name = canonicalize_strategy_name(
            name_raw,
            source="startup_validator.strategies",
            warn_alias=False,
        )
        if not name:
            errors.append(f"Invalid strategy name in strategies[]: {name_raw}")
            continue
        if name not in known:
            errors.append(f"Unknown strategy name in strategies[]: {name_raw}")
            continue

        if name not in ema20_names:
            continue

        base_params = (
            row.get("params", {})
            if isinstance(row.get("params", {}), dict)
            else {}
        )
        for inst in _iter_instruments(row.get("instruments", [])):
            inst_label = str(inst.get("label"))
            inst_specific = {k: v for k, v in inst.items() if k != "label"}
            merged = dict(base_params)
            merged.update(inst_specific)
            try:
                cfg = build_ema20_config(
                    underlying_label=inst_label,
                    raw=strategy_cfg,
                    params=merged,
                    strict=True,
                )
            except Exception as exc:
                errors.append(
                    f"Invalid EMA20 params for {inst_label} ({name_raw}): {exc}"
                )
                continue
            if inst_label.upper() == "NG_FUT":
                min_start = dt_time(9, 30)
                if cfg.first_entry_time is None:
                    errors.append(
                        "EMA20 NG_FUT requires first_entry_time for session stabilization (>= 09:30)"
                    )
                elif cfg.first_entry_time < min_start:
                    errors.append(
                        f"EMA20 NG_FUT first_entry_time must be >= 09:30, got {cfg.first_entry_time.strftime('%H:%M')}"
                    )
            if cfg.dynamic_policy.enabled:
                non_no_trade_profiles = [
                    values
                    for regime, values in cfg.dynamic_policy.profiles.items()
                    if getattr(regime, "value", str(regime)) != "NO_TRADE"
                    and isinstance(values, dict)
                    and bool(values)
                ]
                if not non_no_trade_profiles:
                    errors.append(
                        f"EMA20 {inst_label} dynamic_policy.enabled=true requires at least one non-empty regime profile outside NO_TRADE"
                    )

    instrument_cfg = (
        strategy_cfg.get("instruments", {})
        if isinstance(strategy_cfg.get("instruments", {}), dict)
        else {}
    )
    for underlying, cfg in instrument_cfg.items():
        if not isinstance(cfg, dict):
            continue
        allowed = cfg.get("allowed_strategies", []) or []
        for strategy_name in allowed:
            txt = str(strategy_name).strip()
            if not txt:
                continue
            canonical = canonicalize_strategy_name(
                txt,
                source=f"startup_validator.instruments.{underlying}",
                warn_alias=False,
            )
            if not canonical:
                errors.append(
                    f"Unknown strategy in instruments.{underlying}.allowed_strategies: {txt}"
                )
                continue
            if canonical not in known:
                errors.append(
                    f"Unknown strategy in instruments.{underlying}.allowed_strategies: {txt}"
                )

    if errors:
        details = "\n".join(f"- {e}" for e in errors)
        raise ValueError(f"Startup configuration validation failed:\n{details}")
