"""Feature flag registry for stability hardening rollout controls.

Supports versioned flag snapshots with before/after tracking and rollback
to a previous flag version (Architecture S2 source-of-truth matrix).
"""

from __future__ import annotations

import copy
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Guard to ensure the LIVE policy gate override warning is emitted only once,
# not on every call to load_stability_feature_flags().
_LIVE_POLICY_GATE_LOGGED = False


@dataclass(frozen=True)
class StabilityFeatureFlags:
    strategy_bridge_mode: str
    strategy_bridge_timeout_s: float
    strategy_bridge_max_inflight: int
    broker_schema_check_mode: str
    order_place_timeout_safe_mode: bool
    tenant_cooldown_local_clear_only: bool
    order_lifecycle_strict_startup_recovery: bool
    order_snapshot_preserve_terminal: bool
    order_lifecycle_use_broker_snapshot: bool
    option_atm_refresh_reconcile_open_legs: bool
    capital_margin_check_mode: str
    angel_postback_auth_mode: str
    dashboard_fix_recent_trades: bool
    executed_tokens_persist_enabled: bool
    executed_tokens_state_path: str
    executed_tokens_state_flush_interval_s: float
    clock_adoption: str


# ---------------------------------------------------------------------------
# Feature flag version tracking (H2)
# ---------------------------------------------------------------------------

@dataclass
class FlagChangeRecord:
    """Records a single flag change with before/after and metadata."""
    version: int
    changed_at: datetime
    actor: str
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass
class FeatureFlagVersionStore:
    """Tracks versioned flag snapshots with rollback support.

    Each call to ``update()`` increments the version counter and records
    the before/after snapshot so that the operator can roll back to any
    previous version.
    """

    _current: Optional[StabilityFeatureFlags] = field(default=None, repr=False)
    _version: int = 0
    _history: list[FlagChangeRecord] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def load(
        self,
        flags: StabilityFeatureFlags,
        *,
        actor: str = "system",
    ) -> int:
        """Load a new flag set, bumping the version. Returns the new version."""
        with self._lock:
            before = asdict(self._current) if self._current else {}
            after = asdict(flags)
            self._version += 1
            self._current = flags
            record = FlagChangeRecord(
                version=self._version,
                changed_at=datetime.now(timezone.utc),
                actor=actor,
                before=before,
                after=after,
            )
            self._history.append(record)
            logger.info(
                "Feature flags updated to version %d by %s",
                self._version, actor,
            )
            return self._version

    def current(self) -> Optional[StabilityFeatureFlags]:
        with self._lock:
            return self._current

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def history(self, *, limit: int = 50) -> list[FlagChangeRecord]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def rollback(self, target_version: int, *, actor: str = "operator") -> StabilityFeatureFlags:
        """Roll back to a previous flag version. Raises ValueError if not found."""
        with self._lock:
            target_record = None
            for record in self._history:
                if record.version == target_version:
                    target_record = record
                    break
            if target_record is None:
                raise ValueError(
                    f"Feature flag version {target_version} not found in history"
                )
            # Reconstruct flags from the 'after' snapshot of target version
            restored_flags = StabilityFeatureFlags(**target_record.after)
            before = asdict(self._current) if self._current else {}
            self._version += 1
            self._current = restored_flags
            rollback_record = FlagChangeRecord(
                version=self._version,
                changed_at=datetime.now(timezone.utc),
                actor=actor,
                before=before,
                after=target_record.after,
            )
            self._history.append(rollback_record)
            logger.info(
                "Feature flags rolled back to version %d (now version %d) by %s",
                target_version, self._version, actor,
            )
            return restored_flags


# Module-level singleton
feature_flag_store = FeatureFlagVersionStore()


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def load_stability_feature_flags(
    env: Mapping[str, str] | None = None,
    *,
    log: logging.Logger | None = None,
) -> StabilityFeatureFlags:
    source = env if env is not None else {}
    sink = log or logger

    def _get(name: str) -> str | None:
        if env is None:
            import os

            return os.getenv(name)
        return source.get(name)

    def _choice(name: str, allowed: set[str], default: str) -> str:
        raw = _get(name)
        if raw is None:
            return default
        value = str(raw).strip().lower()
        if value in allowed:
            return value
        sink.warning(
            "Invalid %s=%r; falling back to %r.",
            name,
            raw,
            default,
        )
        return default

    def _float(name: str, default: float, *, minimum: float) -> float:
        raw = _get(name)
        if raw is None:
            return default
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            sink.warning(
                "Invalid %s=%r; falling back to %.3f.",
                name,
                raw,
                default,
            )
            return default
        if parsed < minimum:
            sink.warning(
                "Out-of-range %s=%r; falling back to %.3f.",
                name,
                raw,
                default,
            )
            return default
        return parsed

    def _int(name: str, default: int, *, minimum: int) -> int:
        raw = _get(name)
        if raw is None:
            return default
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            sink.warning(
                "Invalid %s=%r; falling back to %d.",
                name,
                raw,
                default,
            )
            return default
        if parsed < minimum:
            sink.warning(
                "Out-of-range %s=%r; falling back to %d.",
                name,
                raw,
                default,
            )
            return default
        return parsed

    strategy_bridge_mode = _choice(
        "STRATEGY_BRIDGE_MODE",
        {"per_call_loop", "shared_loop"},
        "per_call_loop",
    )
    strategy_bridge_timeout_s = _float(
        "STRATEGY_BRIDGE_TIMEOUT_S",
        2.5,
        minimum=0.1,
    )
    strategy_bridge_max_inflight = _int(
        "STRATEGY_BRIDGE_MAX_INFLIGHT",
        50,
        minimum=1,
    )
    # Default is "off" for deployment stability: AngelOne broker API responses
    # occasionally include undeclared fields or null positions for unknown instruments,
    # and early strict-mode testing surfaced false-positive parse errors on otherwise
    # valid balance and positions payloads. "warn" logs anomalies without blocking;
    # "strict" raises BrokerSchemaError and blocks the sync cycle.
    # LIVE deployments should set BROKER_SCHEMA_CHECK_MODE=strict (enforced via compose).
    # A startup WARNING is emitted when TRADE_MODE=LIVE and this is not strict.
    broker_schema_check_mode = _choice(
        "BROKER_SCHEMA_CHECK_MODE",
        {"off", "warn", "strict"},
        "off",
    )
    order_place_timeout_safe_mode = _parse_bool(
        _get("ORDER_PLACE_TIMEOUT_SAFE_MODE"),
        False,
    )
    tenant_cooldown_local_clear_only = _parse_bool(
        _get("TENANT_COOLDOWN_LOCAL_CLEAR_ONLY"),
        False,
    )
    order_lifecycle_strict_startup_recovery = _parse_bool(
        _get("ORDER_LIFECYCLE_STRICT_STARTUP_RECOVERY"),
        False,
    )
    order_snapshot_preserve_terminal = _parse_bool(
        _get("ORDER_SNAPSHOT_PRESERVE_TERMINAL"),
        False,
    )
    order_lifecycle_use_broker_snapshot = _parse_bool(
        _get("ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT"),
        False,
    )
    option_atm_refresh_reconcile_open_legs = _parse_bool(
        _get("OPTION_ATM_REFRESH_RECONCILE_OPEN_LEGS"),
        False,
    )
    capital_margin_check_mode = _choice(
        "CAPITAL_MARGIN_CHECK_MODE",
        {"off", "shadow", "enforce"},
        "off",
    )
    angel_postback_auth_mode = _choice(
        "ANGEL_POSTBACK_AUTH_MODE",
        {"legacy", "direct_broker", "relay"},
        "legacy",
    )
    dashboard_fix_recent_trades = _parse_bool(
        _get("DASHBOARD_FIX_RECENT_TRADES"),
        False,
    )
    executed_tokens_persist_enabled = _parse_bool(
        _get("EXECUTED_TOKENS_PERSIST_ENABLED"),
        False,
    )
    executed_tokens_state_path = str(
        _get("EXECUTED_TOKENS_STATE_PATH") or "logs/executed_tokens_state.json"
    ).strip() or "logs/executed_tokens_state.json"
    executed_tokens_state_flush_interval_s = _float(
        "EXECUTED_TOKENS_STATE_FLUSH_INTERVAL_S",
        10.0,
        minimum=0.1,
    )
    # ---------------------------------------------------------------------------
    # LIVE policy gates (Architecture S2):  In LIVE mode, enforce hardened
    # defaults for flags that the architecture requires.  Operator overrides
    # via env vars are still respected (they come through _parse_bool / _choice
    # above), but the *default* for LIVE is strict.
    # ---------------------------------------------------------------------------
    import os as _os
    _trade_mode = str(
        (_get("TRADE_MODE") if env is not None else _os.getenv("TRADE_MODE"))
        or "PAPER"
    ).strip().upper()

    if _trade_mode == "LIVE":
        global _LIVE_POLICY_GATE_LOGGED
        _live_overrides: dict[str, Any] = {}

        if not order_lifecycle_strict_startup_recovery:
            order_lifecycle_strict_startup_recovery = True
            _live_overrides["ORDER_LIFECYCLE_STRICT_STARTUP_RECOVERY"] = True
        if not order_snapshot_preserve_terminal:
            order_snapshot_preserve_terminal = True
            _live_overrides["ORDER_SNAPSHOT_PRESERVE_TERMINAL"] = True
        if not order_lifecycle_use_broker_snapshot:
            order_lifecycle_use_broker_snapshot = True
            _live_overrides["ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT"] = True
        if not option_atm_refresh_reconcile_open_legs:
            option_atm_refresh_reconcile_open_legs = True
            _live_overrides["OPTION_ATM_REFRESH_RECONCILE_OPEN_LEGS"] = True
        if capital_margin_check_mode == "off":
            capital_margin_check_mode = "enforce"
            _live_overrides["CAPITAL_MARGIN_CHECK_MODE"] = "enforce"
        if angel_postback_auth_mode == "legacy":
            angel_postback_auth_mode = "direct_broker"
            _live_overrides["ANGEL_POSTBACK_AUTH_MODE"] = "direct_broker"
        if not executed_tokens_persist_enabled:
            executed_tokens_persist_enabled = True
            _live_overrides["EXECUTED_TOKENS_PERSIST_ENABLED"] = True

        if _live_overrides and not _LIVE_POLICY_GATE_LOGGED:
            sink.warning(
                "LIVE mode policy gates enforced hardened defaults for: %s",
                _live_overrides,
            )
            _LIVE_POLICY_GATE_LOGGED = True

    return StabilityFeatureFlags(
        strategy_bridge_mode=strategy_bridge_mode,
        strategy_bridge_timeout_s=strategy_bridge_timeout_s,
        strategy_bridge_max_inflight=strategy_bridge_max_inflight,
        broker_schema_check_mode=broker_schema_check_mode,
        order_place_timeout_safe_mode=order_place_timeout_safe_mode,
        tenant_cooldown_local_clear_only=tenant_cooldown_local_clear_only,
        order_lifecycle_strict_startup_recovery=order_lifecycle_strict_startup_recovery,
        order_snapshot_preserve_terminal=order_snapshot_preserve_terminal,
        order_lifecycle_use_broker_snapshot=order_lifecycle_use_broker_snapshot,
        option_atm_refresh_reconcile_open_legs=option_atm_refresh_reconcile_open_legs,
        capital_margin_check_mode=capital_margin_check_mode,
        angel_postback_auth_mode=angel_postback_auth_mode,
        dashboard_fix_recent_trades=dashboard_fix_recent_trades,
        executed_tokens_persist_enabled=executed_tokens_persist_enabled,
        executed_tokens_state_path=executed_tokens_state_path,
        executed_tokens_state_flush_interval_s=executed_tokens_state_flush_interval_s,
        clock_adoption="dependency_injected_system_default",
    )


def log_stability_feature_flags(
    flags: StabilityFeatureFlags,
    *,
    log: logging.Logger | None = None,
) -> None:
    sink = log or logger
    sink.info(
        "Stability feature flags active: %s",
        {
            "STRATEGY_BRIDGE_MODE": flags.strategy_bridge_mode,
            "STRATEGY_BRIDGE_TIMEOUT_S": flags.strategy_bridge_timeout_s,
            "STRATEGY_BRIDGE_MAX_INFLIGHT": flags.strategy_bridge_max_inflight,
            "BROKER_SCHEMA_CHECK_MODE": flags.broker_schema_check_mode,
            "ORDER_PLACE_TIMEOUT_SAFE_MODE": flags.order_place_timeout_safe_mode,
            "TENANT_COOLDOWN_LOCAL_CLEAR_ONLY": flags.tenant_cooldown_local_clear_only,
            "ORDER_LIFECYCLE_STRICT_STARTUP_RECOVERY": flags.order_lifecycle_strict_startup_recovery,
            "ORDER_SNAPSHOT_PRESERVE_TERMINAL": flags.order_snapshot_preserve_terminal,
            "ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT": flags.order_lifecycle_use_broker_snapshot,
            "OPTION_ATM_REFRESH_RECONCILE_OPEN_LEGS": flags.option_atm_refresh_reconcile_open_legs,
            "CAPITAL_MARGIN_CHECK_MODE": flags.capital_margin_check_mode,
            "ANGEL_POSTBACK_AUTH_MODE": flags.angel_postback_auth_mode,
            "DASHBOARD_FIX_RECENT_TRADES": flags.dashboard_fix_recent_trades,
            "EXECUTED_TOKENS_PERSIST_ENABLED": flags.executed_tokens_persist_enabled,
            "EXECUTED_TOKENS_STATE_PATH": flags.executed_tokens_state_path,
            "EXECUTED_TOKENS_STATE_FLUSH_INTERVAL_S": flags.executed_tokens_state_flush_interval_s,
            "CLOCK_ADOPTION": flags.clock_adoption,
        },
    )


__all__ = [
    "FeatureFlagVersionStore",
    "FlagChangeRecord",
    "StabilityFeatureFlags",
    "feature_flag_store",
    "load_stability_feature_flags",
    "log_stability_feature_flags",
]
