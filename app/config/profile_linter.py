from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values

from app.config.boot_config import RuntimeConfig
from app.config.runtime_config import (
    _LIVE_BREAK_GLASS_APPROVED_BY_ENV,
    _LIVE_BREAK_GLASS_FLAG_ENV,
    _LIVE_BREAK_GLASS_UNTIL_ENV,
)
from app.core.startup_config_validator import validate_runtime_startup_settings

_LOCAL_APP_ENVS = {"local", "dev", "test"}
_EXAMPLE_LINT_NETWORK_IDENTITY = {
    "CLIENT_LOCAL_IP": "10.0.0.10",
    "CLIENT_PUBLIC_IP": "203.0.113.10",
    "MAC_ADDRESS": "02:00:00:00:00:10",
}


def _parse_bool(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_env_file(path: Path | str) -> dict[str, str]:
    resolved = Path(path)
    payload = dotenv_values(resolved)
    return {
        str(key): str(value)
        for key, value in payload.items()
        if key and value is not None
    }


def _build_lint_settings(env: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        eod_exit_time=str(env.get("EOD_EXIT_TIME", "15:20") or "15:20"),
        eod_exit_retry_cutoff_time=str(
            env.get("EOD_EXIT_RETRY_CUTOFF_TIME", "15:30") or "15:30"
        ),
        hub_subscription_poll_interval=_parse_float(
            env.get("HUB_SUBSCRIPTION_POLL_INTERVAL")
        )
        or 60.0,
        risk_enable_daily_loss=_parse_bool(
            env.get("RISK_ENABLE_DAILY_LOSS"),
            default=False,
        ),
        risk_max_daily_loss=_parse_float(env.get("RISK_MAX_DAILY_LOSS")),
        admin_api_key=env.get("ADMIN_API_KEY") or env.get("ADMIN_API_KEY_SECRET") or None,
        dashboard_hmac_auth_enabled=_parse_bool(
            env.get("DASHBOARD_HMAC_AUTH_ENABLED"),
            default=False,
        ),
        dashboard_hmac_secret=env.get("DASHBOARD_HMAC_SECRET"),
        control_plane_backend=str(env.get("CONTROL_PLANE_BACKEND", "") or ""),
        sweep_state_backend=str(env.get("SWEEP_STATE_BACKEND", "") or ""),
        enable_capital_checks=_parse_bool(
            env.get("ENABLE_CAPITAL_CHECKS"),
            default=True,
        ),
        capital_fail_closed_on_missing_state=_parse_bool(
            env.get("CAPITAL_FAIL_CLOSED_ON_MISSING_STATE"),
            default=False,
        ),
        capital_fail_closed_on_missing_notional_price=_parse_bool(
            env.get("CAPITAL_FAIL_CLOSED_ON_MISSING_NOTIONAL_PRICE"),
            default=False,
        ),
        enable_risk_checks=_parse_bool(
            env.get("ENABLE_RISK_CHECKS"),
            default=True,
        ),
        enable_profit_checks=_parse_bool(
            env.get("ENABLE_PROFIT_CHECKS"),
            default=False,
        ),
        profit_enable_daily_target=_parse_bool(
            env.get("PROFIT_ENABLE_DAILY_TARGET"),
            default=False,
        ),
        profit_daily_target=_parse_float(env.get("PROFIT_DAILY_TARGET")),
        order_router_enforce_idempotency=_parse_bool(
            env.get("ORDER_ROUTER_ENFORCE_IDEMPOTENCY"),
            default=False,
        ),
        position_ownership_enabled=_parse_bool(
            env.get("POSITION_OWNERSHIP_ENABLED"),
            default=False,
        ),
        order_lifecycle_persist_markers_required=_parse_bool(
            env.get("ORDER_LIFECYCLE_PERSIST_MARKERS_REQUIRED"),
            default=False,
        ),
        enable_multi_hub=_parse_bool(
            env.get("ENABLE_MULTI_HUB"),
            default=False,
        ),
        use_hub_router=_parse_bool(
            env.get("USE_HUB_ROUTER"),
            default=False,
        ),
        risk_fail_open_on_missing_pnl=_parse_bool(
            env.get("RISK_FAIL_OPEN_ON_MISSING_PNL"),
            default=False,
        ),
        risk_include_unrealized_in_daily_loss=_parse_bool(
            env.get("RISK_INCLUDE_UNREALIZED_IN_DAILY_LOSS"),
            default=True,
        ),
        position_ownership_unknown_mode=str(
            env.get("POSITION_OWNERSHIP_UNKNOWN_MODE", "") or ""
        ),
        eod_cancel_open_orders_enabled=_parse_bool(
            env.get("EOD_CANCEL_OPEN_ORDERS_ENABLED"),
            default=False,
        ),
        circuit_breaker_persist_state=_parse_bool(
            env.get("CIRCUIT_BREAKER_PERSIST_STATE"),
            default=False,
        ),
        dashboard_auth_disabled=_parse_bool(
            env.get("DASHBOARD_AUTH_DISABLED"),
            default=False,
        ),
    )


def _extract_validator_errors(exc: ValueError) -> list[str]:
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    if not lines:
        return ["unknown config validation error"]
    header = "Startup runtime setting validation failed:"
    if lines[0] == header:
        lines = lines[1:]
    return [line[2:] if line.startswith("- ") else line for line in lines]


def _validator_env_for_profile(
    *,
    path: Path,
    env: dict[str, str],
    trade_mode: str,
) -> dict[str, str]:
    validator_env = dict(env)
    if trade_mode == "LIVE" and path.name.endswith(".example"):
        for key, value in _EXAMPLE_LINT_NETWORK_IDENTITY.items():
            validator_env.setdefault(key, value)
    return validator_env


def lint_env_profile(path: Path | str) -> list[str]:
    resolved = Path(path)
    env = parse_env_file(resolved)
    errors: list[str] = []
    app_env_raw = str(env.get("APP_ENV", "") or "").strip()
    app_env = app_env_raw.lower() if app_env_raw else ""
    trade_mode = str(env.get("TRADE_MODE", "PAPER") or "PAPER").strip().upper()

    if not app_env_raw:
        errors.append("APP_ENV must be set explicitly")
    if _parse_bool(env.get(_LIVE_BREAK_GLASS_FLAG_ENV), default=False):
        errors.append(
            f"{_LIVE_BREAK_GLASS_FLAG_ENV} must be false in committed env profiles"
        )
    if str(env.get(_LIVE_BREAK_GLASS_APPROVED_BY_ENV, "") or "").strip():
        errors.append(
            f"{_LIVE_BREAK_GLASS_APPROVED_BY_ENV} must be unset in committed env profiles"
        )
    if str(env.get(_LIVE_BREAK_GLASS_UNTIL_ENV, "") or "").strip():
        errors.append(
            f"{_LIVE_BREAK_GLASS_UNTIL_ENV} must be unset in committed env profiles"
        )
    if trade_mode == "LIVE" and app_env in _LOCAL_APP_ENVS:
        errors.append("TRADE_MODE=LIVE requires APP_ENV outside local/dev/test")
    if trade_mode == "LIVE" and _parse_bool(
        env.get("DISABLE_TRADING_WINDOW_FILTER"),
        default=False,
    ):
        errors.append("TRADE_MODE=LIVE requires DISABLE_TRADING_WINDOW_FILTER=false")
    if trade_mode == "LIVE":
        broker_secret_backend = str(
            env.get("BROKER_SECRET_BACKEND", "") or ""
        ).strip().lower()
        _linter_live_approved = {
            "secret_manager",
            "secretmanager",
            "postgres",
            "pg",
            "db",
            "database",
        }
        if broker_secret_backend not in _linter_live_approved:
            errors.append(
                "TRADE_MODE=LIVE requires BROKER_SECRET_BACKEND to resolve to secret_manager or postgres"
            )
        dashboard_auth_disabled = _parse_bool(
            env.get("DASHBOARD_AUTH_DISABLED"), default=False
        )
        if dashboard_auth_disabled:
            errors.append(
                "TRADE_MODE=LIVE forbids DASHBOARD_AUTH_DISABLED=true"
            )
    if app_env and app_env not in _LOCAL_APP_ENVS:
        if not _parse_bool(env.get("APP_RUNTIME_STARTUP_VALIDATE"), default=False):
            errors.append(
                "non-local profiles require APP_RUNTIME_STARTUP_VALIDATE=true"
            )
        if str(env.get("SCHEMA_CHECK_MODE", "") or "").strip().lower() != "strict":
            errors.append("non-local profiles require SCHEMA_CHECK_MODE=strict")

    try:
        validator_env = _validator_env_for_profile(
            path=resolved,
            env=env,
            trade_mode=trade_mode,
        )
        validate_runtime_startup_settings(
            settings=_build_lint_settings(validator_env),
            runtime_cfg=RuntimeConfig.from_env(validator_env),
            env=validator_env,
        )
    except ValueError as exc:
        errors.extend(_extract_validator_errors(exc))

    return errors


def lint_env_profiles(paths: list[Path | str]) -> dict[Path, list[str]]:
    results: dict[Path, list[str]] = {}
    for path in paths:
        resolved = Path(path)
        errors = lint_env_profile(resolved)
        if errors:
            results[resolved] = errors
    return results


__all__ = ["lint_env_profile", "lint_env_profiles", "parse_env_file"]
