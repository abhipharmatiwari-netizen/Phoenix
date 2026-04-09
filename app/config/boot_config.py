"""Boot-time normalized config snapshot.

Loads and normalizes configuration once (settings + strategy YAML)
and exposes a typed snapshot for downstream services.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Mapping, Optional

from app.config.settings import Settings, get_settings
from app.core.strategy_config_loader import load_strategy_env_from_yaml


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass(frozen=True)
class RuntimeConfig:
    app_env: str
    runner_mode: str
    stream_watchdog_interval_seconds: float
    stream_watchdog_restart_backoff_base_seconds: float
    stream_watchdog_restart_backoff_max_seconds: float
    stream_watchdog_restart_backoff_jitter_ratio: float
    stream_watchdog_stable_run_window_seconds: float
    disable_stream_worker: bool
    leader_lease_enabled_override: Optional[bool]
    leader_lease_backend: str
    leader_lease_id: Optional[str]
    leader_lease_ttl_seconds: int
    leader_lease_renew_seconds: int
    leader_lease_collection: str
    demo_auth_requested: bool
    enable_demo_auth: bool
    disable_control_tower_routes: bool
    dashboard_auth_disabled: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RuntimeConfig":
        app_env = str(env.get("APP_ENV", env.get("ENV", "local")) or "local").strip().lower() or "local"
        demo_auth_requested = _coerce_bool(
            env.get("ENABLE_DEMO_AUTH"),
            default=False,
        )
        lease_raw = env.get("LEADER_LEASE_ENABLED")
        leader_override: Optional[bool]
        if lease_raw is None:
            leader_override = None
        else:
            leader_override = _coerce_bool(lease_raw, default=False)
        return cls(
            app_env=app_env,
            runner_mode=str(env.get("RUNNER_MODE", "uvicorn") or "uvicorn").strip()
            or "uvicorn",
            stream_watchdog_interval_seconds=_coerce_float(
                env.get("STREAM_WATCHDOG_INTERVAL"),
                default=15.0,
            ),
            stream_watchdog_restart_backoff_base_seconds=_coerce_float(
                env.get("STREAM_WATCHDOG_RESTART_BACKOFF_BASE_SECONDS"),
                default=5.0,
            ),
            stream_watchdog_restart_backoff_max_seconds=_coerce_float(
                env.get("STREAM_WATCHDOG_RESTART_BACKOFF_MAX_SECONDS"),
                default=300.0,
            ),
            stream_watchdog_restart_backoff_jitter_ratio=_coerce_float(
                env.get("STREAM_WATCHDOG_RESTART_BACKOFF_JITTER_RATIO"),
                default=0.2,
            ),
            stream_watchdog_stable_run_window_seconds=_coerce_float(
                env.get("STREAM_WATCHDOG_STABLE_RUN_WINDOW_SECONDS"),
                default=180.0,
            ),
            disable_stream_worker=_coerce_bool(
                env.get("DISABLE_STREAM_WORKER"),
                default=False,
            ),
            leader_lease_enabled_override=leader_override,
            leader_lease_backend=(
                str(env.get("LEADER_LEASE_BACKEND", "")).strip().lower()
            ),
            leader_lease_id=(
                str(env.get("LEADER_LEASE_ID", "")).strip() or None
            ),
            leader_lease_ttl_seconds=_coerce_int(
                env.get("LEADER_LEASE_TTL_SECONDS"),
                default=90,
            ),
            leader_lease_renew_seconds=_coerce_int(
                env.get("LEADER_LEASE_RENEW_SECONDS"),
                default=30,
            ),
            leader_lease_collection=(
                str(env.get("LEADER_LEASE_COLLECTION", "leader_leases")).strip()
                or "leader_leases"
            ),
            demo_auth_requested=demo_auth_requested,
            enable_demo_auth=bool(demo_auth_requested and app_env in {"local", "dev"}),
            disable_control_tower_routes=_coerce_bool(
                env.get("DISABLE_CONTROL_TOWER_ROUTES"),
                default=False,
            ),
            dashboard_auth_disabled=_coerce_bool(
                env.get("DASHBOARD_AUTH_DISABLED"),
                default=False,
            ),
        )


@dataclass(frozen=True)
class StrategyValueResolver:
    """Resolve per-strategy values with env-overrides over YAML params."""

    env_prefix: str
    params: Mapping[str, Any]
    env: Mapping[str, str]

    def _param_value(self, key: str) -> Any:
        if key in self.params:
            return self.params[key]
        lower_key = key.lower()
        if lower_key in self.params:
            return self.params[lower_key]
        return None

    def _env_value(self, key: str) -> Optional[str]:
        prefixed = f"{self.env_prefix}{key}"
        prefixed_val = self.env.get(prefixed)
        if prefixed_val not in (None, ""):
            return prefixed_val
        plain_val = self.env.get(key)
        if plain_val not in (None, ""):
            return plain_val
        return None

    def _prefixed_env_value(self, key: str) -> Optional[str]:
        prefixed = f"{self.env_prefix}{key}"
        prefixed_val = self.env.get(prefixed)
        if prefixed_val not in (None, ""):
            return prefixed_val
        return None

    def get(self, key: str, default: Any = None) -> Any:
        """
        Precedence: ENV(<PREFIX><KEY>) > ENV(<KEY>) > YAML params > default.
        """
        env_val = self._env_value(key)
        if env_val is not None:
            return env_val
        param_val = self._param_value(key)
        if param_val not in (None, ""):
            return param_val
        return default

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        if value is None:
            return default
        return str(value)

    def get_prefixed(self, key: str, default: Any = None) -> Any:
        env_val = self._prefixed_env_value(key)
        if env_val is not None:
            return env_val
        param_val = self._param_value(key)
        if param_val not in (None, ""):
            return param_val
        return default

    def get_prefixed_str(self, key: str, default: str = "") -> str:
        value = self.get_prefixed(key, default)
        if value is None:
            return default
        return str(value)

    def get_prefixed_float(self, key: str, default: float) -> float:
        return _coerce_float(self.get_prefixed(key, default), default=default)

    def get_prefixed_int(self, key: str, default: int) -> int:
        return _coerce_int(self.get_prefixed(key, default), default=default)

    def get_prefixed_bool(self, key: str, default: bool = False) -> bool:
        return _coerce_bool(self.get_prefixed(key, default), default=default)

    def get_int(self, key: str, default: int) -> int:
        return _coerce_int(self.get(key, default), default=default)

    def get_float(self, key: str, default: float) -> float:
        return _coerce_float(self.get(key, default), default=default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return _coerce_bool(self.get(key, default), default=default)

    def get_first_non_empty(
        self, keys: list[str], default: Any = None
    ) -> tuple[Any, Optional[str]]:
        for key in keys:
            value = self.env.get(key)
            if value not in (None, ""):
                return value, key
            param_value = self._param_value(key)
            if param_value not in (None, ""):
                return param_value, key
        return default, None


@dataclass(frozen=True)
class BootConfigSnapshot:
    created_at_utc: str
    settings: Settings
    runtime: RuntimeConfig
    strategy_env: Dict[str, Dict[str, Any]]
    env: Dict[str, str]

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "created_at_utc": self.created_at_utc,
            "enable_multi_hub": bool(getattr(self.settings, "enable_multi_hub", False)),
            "use_hub_router": bool(getattr(self.settings, "use_hub_router", False)),
            "default_time_zone": str(getattr(self.settings, "default_time_zone", "UTC")),
            "app_env": self.runtime.app_env,
            "runner_mode": self.runtime.runner_mode,
            "disable_stream_worker": self.runtime.disable_stream_worker,
            "stream_watchdog_interval_seconds": self.runtime.stream_watchdog_interval_seconds,
            "stream_watchdog_restart_backoff_base_seconds": self.runtime.stream_watchdog_restart_backoff_base_seconds,
            "stream_watchdog_restart_backoff_max_seconds": self.runtime.stream_watchdog_restart_backoff_max_seconds,
            "stream_watchdog_restart_backoff_jitter_ratio": self.runtime.stream_watchdog_restart_backoff_jitter_ratio,
            "stream_watchdog_stable_run_window_seconds": self.runtime.stream_watchdog_stable_run_window_seconds,
            "strategy_sections": sorted(list(self.strategy_env.keys())),
        }

    def strategy_resolver(
        self,
        *,
        env_prefix: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> StrategyValueResolver:
        return StrategyValueResolver(
            env_prefix=str(env_prefix or ""),
            params=params or {},
            env=self.env,
        )


_BOOT_CONFIG: BootConfigSnapshot | None = None
_BOOT_CONFIG_LOCK = Lock()


def build_boot_config_snapshot() -> BootConfigSnapshot:
    env = dict(os.environ)
    settings = get_settings()
    runtime = RuntimeConfig.from_env(env)
    strategy_env = load_strategy_env_from_yaml()
    return BootConfigSnapshot(
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        settings=settings,
        runtime=runtime,
        strategy_env=strategy_env,
        env=env,
    )


def initialize_boot_config(*, force: bool = False) -> BootConfigSnapshot:
    global _BOOT_CONFIG
    with _BOOT_CONFIG_LOCK:
        if _BOOT_CONFIG is None or force:
            _BOOT_CONFIG = build_boot_config_snapshot()
        return _BOOT_CONFIG


def get_boot_config() -> BootConfigSnapshot:
    snap = _BOOT_CONFIG
    if snap is not None:
        return snap
    return initialize_boot_config()


__all__ = [
    "BootConfigSnapshot",
    "RuntimeConfig",
    "StrategyValueResolver",
    "build_boot_config_snapshot",
    "initialize_boot_config",
    "get_boot_config",
]
