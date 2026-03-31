"""
Dynamic runtime config provider.

Provides low-latency cached overrides from Firestore and/or Secret Manager and
merges them on top of environment-backed Settings at runtime.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.config.settings import Settings, get_settings
from app.core.audit_log import emit_audit_event
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId

logger = logging.getLogger(__name__)


_BOOL_OVERRIDE_KEYS = {
    "use_hub_router",
    "use_hub_router_for_ng_options",
    "use_hub_router_for_nifty_options",
    "use_hub_router_for_nifty_delta",
    "use_hub_router_for_banknifty_options",
    "use_hub_router_for_banknifty_delta",
    "use_hub_router_for_finnifty_options",
    "use_hub_router_for_sensex_options",
    "use_hub_router_for_midcpnifty_options",
    "enable_capital_checks",
    "capital_auto_resize_enabled",
    "capital_fail_closed_on_missing_state",
    "capital_fail_closed_on_missing_notional_price",
    "enable_risk_checks",
    "risk_enable_daily_loss",
    "risk_fail_open_on_missing_pnl",
    "risk_fail_closed_on_missing_pnl",
    "risk_include_unrealized_in_daily_loss",
    "enable_profit_checks",
    "position_ownership_enabled",
    "position_ownership_allow_system_exit",
    "auto_strategy_select_enabled",
    "profit_enable_daily_target",
    "profit_block_new_orders_on_target",
    "profit_lock_enabled",
    "profit_lock_require_signal",
}

_FLOAT_OVERRIDE_KEYS = {
    "risk_max_daily_loss",
    "profit_daily_target",
    "profit_daily_target_paper",
    "profit_lock_giveback_pct",
    "profit_lock_exit_cooldown_seconds",
    "auto_strategy_min_hold_seconds",
    "capital_margin_short_option_per_lot",
    "capital_margin_futures_per_lot",
    "capital_margin_futures_rate",
}

_INT_OVERRIDE_KEYS = {
    "eod_exit_positions_max_age_seconds",
    "auto_strategy_max_active_per_underlying",
}

_STRING_OVERRIDE_KEYS = {
    "default_time_zone",
    "eod_exit_time",
    "eod_exit_retry_cutoff_time",
    "position_ownership_unknown_mode",
}

_ALLOWED_OVERRIDE_KEYS = (
    _BOOL_OVERRIDE_KEYS
    | _FLOAT_OVERRIDE_KEYS
    | _INT_OVERRIDE_KEYS
    | _STRING_OVERRIDE_KEYS
)

_LIVE_PROTECTED_OVERRIDE_KEYS = {
    "enable_capital_checks",
    "capital_fail_closed_on_missing_state",
    "capital_fail_closed_on_missing_notional_price",
    "enable_risk_checks",
    "risk_enable_daily_loss",
    "risk_fail_open_on_missing_pnl",
    "risk_fail_closed_on_missing_pnl",
    "risk_include_unrealized_in_daily_loss",
    "risk_max_daily_loss",
    "enable_profit_checks",
    "profit_enable_daily_target",
    "profit_daily_target",
    "profit_block_new_orders_on_target",
    "position_ownership_enabled",
    "position_ownership_unknown_mode",
    "eod_exit_time",
    "eod_exit_retry_cutoff_time",
    "capital_margin_short_option_per_lot",
    "capital_margin_futures_per_lot",
    "capital_margin_futures_rate",
}

_LIVE_BREAK_GLASS_FLAG_ENV = "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS"
_LIVE_BREAK_GLASS_APPROVED_BY_ENV = "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_APPROVED_BY"
_LIVE_BREAK_GLASS_UNTIL_ENV = "LIVE_RUNTIME_OVERRIDE_BREAK_GLASS_UNTIL"


def _parse_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in values.items()
        if value not in (None, "")
    }


def _trade_mode() -> str:
    return str(os.getenv("TRADE_MODE", "PAPER") or "PAPER").strip().upper()


def _parse_break_glass_until(raw_value: object) -> Optional[datetime]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _live_break_glass_status() -> tuple[bool, dict[str, Any]]:
    enabled = _parse_bool(os.getenv(_LIVE_BREAK_GLASS_FLAG_ENV)) is True
    approved_by = str(os.getenv(_LIVE_BREAK_GLASS_APPROVED_BY_ENV) or "").strip()
    until_raw = str(os.getenv(_LIVE_BREAK_GLASS_UNTIL_ENV) or "").strip()
    metadata: dict[str, Any] = {
        "break_glass_enabled": enabled,
        "approved_by": approved_by or None,
        "expires_at_utc": until_raw or None,
    }
    if not enabled:
        return False, metadata
    if not approved_by:
        metadata["reason"] = (
            f"{_LIVE_BREAK_GLASS_FLAG_ENV}=true requires "
            f"{_LIVE_BREAK_GLASS_APPROVED_BY_ENV}"
        )
        return False, metadata
    expires_at = _parse_break_glass_until(until_raw)
    if expires_at is None:
        metadata["reason"] = (
            f"{_LIVE_BREAK_GLASS_FLAG_ENV}=true requires a valid ISO-8601 "
            f"{_LIVE_BREAK_GLASS_UNTIL_ENV}"
        )
        return False, metadata
    if expires_at <= datetime.now(timezone.utc):
        metadata["expires_at_utc"] = expires_at.isoformat().replace("+00:00", "Z")
        metadata["reason"] = "live runtime override break-glass approval has expired"
        return False, metadata
    metadata["expires_at_utc"] = expires_at.isoformat().replace("+00:00", "Z")
    return True, metadata


def _live_override_violations(
    *,
    base_settings: Settings,
    overrides: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    violations: dict[str, dict[str, Any]] = {}
    for key in sorted(_LIVE_PROTECTED_OVERRIDE_KEYS):
        if key not in overrides:
            continue
        override_value = overrides[key]
        base_value = getattr(base_settings, key, None)
        if override_value == base_value:
            continue
        violations[key] = {
            "base": base_value,
            "override": override_value,
        }
    return violations


def _coerce_override(key: str, value: object) -> Optional[Any]:
    if key in _BOOL_OVERRIDE_KEYS:
        return _parse_bool(value)
    if key in _FLOAT_OVERRIDE_KEYS:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if key in _INT_OVERRIDE_KEYS:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if key in _STRING_OVERRIDE_KEYS:
        if value is None:
            return None
        return str(value)
    return None


def _filter_overrides(raw: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in raw.items():
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key not in _ALLOWED_OVERRIDE_KEYS:
            continue
        coerced = _coerce_override(normalized_key, value)
        if coerced is None and normalized_key not in _FLOAT_OVERRIDE_KEYS | _INT_OVERRIDE_KEYS:
            continue
        result[normalized_key] = coerced
    return result


@dataclass(frozen=True)
class _RuntimeOverrideSnapshot:
    global_overrides: dict[str, Any]
    account_overrides: dict[str, dict[str, Any]]
    strategy_overrides: dict[str, dict[str, Any]]
    fetched_at_epoch: float


class RuntimeSettingsProxy:
    def __init__(self, base: Any, overrides: dict[str, Any]) -> None:
        self._base = base
        self._overrides = overrides

    @property
    def runtime_overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


class RuntimeConfigProvider:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[_RuntimeOverrideSnapshot] = None
        self._cache_expires_at = 0.0

    @staticmethod
    def _cloud_run_runtime_marker_present() -> bool:
        k_revision = str(os.getenv("K_REVISION") or "").strip()
        k_configuration = str(os.getenv("K_CONFIGURATION") or "").strip()
        if k_revision or k_configuration:
            return True
        # Docker Desktop may inject K_SERVICE=docker-desktop; treat that as local.
        k_service = str(os.getenv("K_SERVICE") or "").strip().lower()
        if not k_service or k_service == "docker-desktop":
            return False
        # Keep backward compatibility for real Cloud Run-like environments that
        # expose only K_SERVICE.
        return True

    @staticmethod
    def _is_enabled(settings: Settings) -> bool:
        if bool(getattr(settings, "runtime_config_enabled", False)):
            return True
        auto_env = os.getenv("RUNTIME_CONFIG_AUTO_ENABLE")
        if auto_env is not None:
            parsed = _parse_bool(auto_env)
            return bool(parsed)
        return RuntimeConfigProvider._cloud_run_runtime_marker_present()

    @staticmethod
    def _cache_ttl_seconds(settings: Settings) -> float:
        try:
            return max(1.0, float(getattr(settings, "runtime_config_cache_seconds", 15.0)))
        except (TypeError, ValueError):
            return 15.0

    def _load_firestore(self, settings: Settings) -> dict[str, Any]:
        try:
            from google.cloud import firestore  # type: ignore
        except Exception:
            return {}
        collection = str(
            getattr(settings, "runtime_config_firestore_collection", "runtime_config")
            or "runtime_config"
        ).strip()
        document = str(
            getattr(settings, "runtime_config_firestore_document", "global") or "global"
        ).strip()
        if not collection or not document:
            return {}
        try:
            client = firestore.Client(project=settings.gcp_project_id or None)
            doc = client.collection(collection).document(document).get()
            if not doc.exists:
                return {}
            raw = doc.to_dict() or {}
            if isinstance(raw, dict):
                return raw
            return {}
        except Exception as exc:
            logger.warning("RuntimeConfigProvider Firestore read failed: %s", exc)
            return {}

    def _load_secret(self, settings: Settings) -> dict[str, Any]:
        secret_id = str(getattr(settings, "runtime_config_secret", "") or "").strip()
        if not secret_id:
            return {}
        try:
            from google.cloud import secretmanager  # type: ignore
        except Exception:
            return {}
        project_id = str(
            getattr(settings, "runtime_config_secret_project", "") or settings.gcp_project_id
        ).strip()
        if not project_id:
            logger.warning("RuntimeConfigProvider secret configured but no project id found")
            return {}
        if not secret_id.startswith("projects/"):
            secret_name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        else:
            secret_name = secret_id
        try:
            client = secretmanager.SecretManagerServiceClient()
            response = client.access_secret_version(name=secret_name)
            payload = response.payload.data.decode("utf-8")
            raw = json.loads(payload) if payload else {}
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning("RuntimeConfigProvider Secret Manager read failed: %s", exc)
            return {}

    @staticmethod
    def _merge_sources(*sources: Mapping[str, Any]) -> _RuntimeOverrideSnapshot:
        global_overrides: dict[str, Any] = {}
        account_overrides: dict[str, dict[str, Any]] = {}
        strategy_overrides: dict[str, dict[str, Any]] = {}

        for source in sources:
            if not source:
                continue
            settings_map = source.get("settings", source)
            if isinstance(settings_map, Mapping):
                global_overrides.update(_filter_overrides(settings_map))

            account_map = source.get("account_overrides", {})
            if isinstance(account_map, Mapping):
                for acct_key, acct_values in account_map.items():
                    if not isinstance(acct_values, Mapping):
                        continue
                    key = str(acct_key or "").strip()
                    if not key:
                        continue
                    existing = account_overrides.get(key, {})
                    existing.update(_filter_overrides(acct_values))
                    account_overrides[key] = existing

            strategy_map = source.get("strategy_overrides", {})
            if isinstance(strategy_map, Mapping):
                for strategy_key, strategy_values in strategy_map.items():
                    if not isinstance(strategy_values, Mapping):
                        continue
                    key = str(strategy_key or "").strip()
                    if not key:
                        continue
                    existing = strategy_overrides.get(key, {})
                    existing.update(_filter_overrides(strategy_values))
                    strategy_overrides[key] = existing

        return _RuntimeOverrideSnapshot(
            global_overrides=global_overrides,
            account_overrides=account_overrides,
            strategy_overrides=strategy_overrides,
            fetched_at_epoch=time.time(),
        )

    def _refresh_snapshot(self, settings: Settings) -> _RuntimeOverrideSnapshot:
        firestore_payload = self._load_firestore(settings)
        secret_payload = self._load_secret(settings)
        return self._merge_sources(firestore_payload, secret_payload)

    def _get_snapshot(self, settings: Settings) -> _RuntimeOverrideSnapshot:
        if not self._is_enabled(settings):
            return _RuntimeOverrideSnapshot(
                global_overrides={},
                account_overrides={},
                strategy_overrides={},
                fetched_at_epoch=time.time(),
            )
        now = time.time()
        if self._cache is not None and now < self._cache_expires_at:
            return self._cache
        with self._lock:
            now = time.time()
            if self._cache is not None and now < self._cache_expires_at:
                return self._cache
            self._cache = self._refresh_snapshot(settings)
            self._cache_expires_at = now + self._cache_ttl_seconds(settings)
            return self._cache

    def resolve(
        self,
        *,
        base_settings: Any,
        tenant_id: Optional[TenantId] = None,
        broker_account_id: Optional[BrokerAccountId] = None,
        strategy_id: Optional[StrategyId] = None,
    ) -> Any:
        if base_settings is None:
            return base_settings
        if not isinstance(base_settings, Settings):
            # tests often patch get_settings() with a lightweight namespace
            return base_settings

        snapshot = self._get_snapshot(base_settings)
        if (
            not snapshot.global_overrides
            and not snapshot.account_overrides
            and not snapshot.strategy_overrides
        ):
            return base_settings

        merged = dict(snapshot.global_overrides)
        if tenant_id is not None and broker_account_id is not None:
            account_key = f"{tenant_id}:{broker_account_id}"
            merged.update(snapshot.account_overrides.get(account_key, {}))
        if strategy_id is not None:
            merged.update(snapshot.strategy_overrides.get(str(strategy_id), {}))
        if not merged:
            return base_settings
        if _trade_mode() == "LIVE":
            violations = _live_override_violations(
                base_settings=base_settings,
                overrides=merged,
            )
            if violations:
                approved, break_glass_metadata = _live_break_glass_status()
                audit_metadata = _compact_mapping(
                    {
                        "tenant_id": str(tenant_id) if tenant_id is not None else None,
                        "broker_account_id": (
                            str(broker_account_id)
                            if broker_account_id is not None
                            else None
                        ),
                        "strategy_id": (
                            str(strategy_id) if strategy_id is not None else None
                        ),
                        **break_glass_metadata,
                    }
                )
                before = {key: values["base"] for key, values in violations.items()}
                after = {key: values["override"] for key, values in violations.items()}
                if approved:
                    emit_audit_event(
                        actor="runtime-config-provider",
                        action="runtime_override_break_glass_allowed",
                        resource_type="runtime_config",
                        resource_id="live",
                        before=before,
                        after=after,
                        metadata=audit_metadata,
                    )
                else:
                    emit_audit_event(
                        actor="runtime-config-provider",
                        action="runtime_override_rejected",
                        resource_type="runtime_config",
                        resource_id="live",
                        before=before,
                        after=after,
                        metadata=audit_metadata,
                    )
                    blocked_keys = ", ".join(sorted(violations))
                    raise ValueError(
                        "LIVE runtime overrides require break-glass approval for "
                        f"protected keys: {blocked_keys}"
                    )
        return RuntimeSettingsProxy(base_settings, merged)


_provider = RuntimeConfigProvider()


def get_runtime_settings(
    *,
    base_settings: Optional[Any] = None,
    tenant_id: Optional[TenantId] = None,
    broker_account_id: Optional[BrokerAccountId] = None,
    strategy_id: Optional[StrategyId] = None,
) -> Any:
    resolved_base = base_settings if base_settings is not None else get_settings()
    return _provider.resolve(
        base_settings=resolved_base,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
    )


__all__ = ["RuntimeSettingsProxy", "RuntimeConfigProvider", "get_runtime_settings"]
