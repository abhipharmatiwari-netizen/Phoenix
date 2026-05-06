"""Execution Hub stream runner (§4 AccountRunner) using legacy single-tenant wiring."""

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone, timedelta, time as dt_time
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


# Allow running as a script (python app/runners/multi_instrument_stream.py)
if __package__ in (None, ""):
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.core.angel_login import angel_login_and_get_tokens
from app.core.bar_persister import BarPersister
from app.core.dashboard_bus import dashboard_bus
from app.core.indicators_engine import GapBackfillRequest, MultiInstrumentIndicatorEngine
from app.core.maintenance import MaintenanceManager, get_trading_session_calendar
from app.core.order_client import AngelOrderClient
from app.core.risk_manager import RiskManager
from app.core.daily_levels import DailyLevelsCache
from app.core.trade_persister import TradePersister
from app.core.executed_tokens_tracker import executed_tokens_tracker
from app.core.feature_flags import load_stability_feature_flags
from app.core.universe_builder import (
    build_instrument_universe,
    normalize_historical_exchange,
)
from app.core.ws_runner import SmartWebSocketRunner
from app.core.strategy_switch import StrategySwitchboard
from app.core.instrument_control import InstrumentController
from app.core.logging_utils import log_event
from app.core.degraded_scope_manager import degraded_scope_manager, DegradedReason
from app.core.position_sync import sync_positions_with_broker
from app.core.signal_metrics import get_labeled_metrics, get_signal_summary_metrics
from app.core.startup_config_validator import validate_startup_config
from app.core.identifiers import StrategyId, TenantId, BrokerAccountId
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
    OrderPurpose,
)
from app.orders.strategy_bridge import place_order_via_bridge
from app.brokers.backoff import BackoffState
from app.strategies.option_strategy import OptionStrategy
from app.strategies.delta_strangle import DeltaStrangleStrategy
from app.strategies.ema20_strategy import Ema20Strategy
from app.strategies.ema20_config import build_ema20_config
from app.strategies.ce_orb import CeOrbStrategy
from app.strategies.ce_pdh_rb import CePdhRangeBreakoutStrategy
from app.strategies.put_momentum_scalper import PutMomentumScalperStrategy
from app.strategies.put_reversion_writer import PutReversionWriterStrategy
from app.strategies.nifty_weekly_credit_spreads import NiftyWeeklyCreditSpreadStrategy
from app.strategies.exclusive_nifty_ce_buy import ExclusiveNiftyCeBuyStrategy
from app.strategies.put_buy_live import PutBuyLiveStrategy
from app.strategies.naming import canonicalize_strategy_name
from app.strategies.validators import validate_strategy_list
from app.strategies.adaptive.market_context import MarketContextBuilder
from app.strategies.adaptive.regime import Regime, RegimeClassifier
from app.strategies.adaptive.strategy_selector import (
    StrategySelector,
    StrategySelectorConfig,
)
from app.config.boot_config import get_boot_config
from app.config.runtime_config import get_runtime_settings
from app.core.operating_mode import resolve_operating_mode_from_runtime
from app.core.p0_operational_guards import P0RuleViolation, assert_legacy_exit_allowed
from app.runners.stream_indicator_plan import (
    build_indicator_engine_plan,
    parse_timeframe_to_seconds as _parse_timeframe_to_seconds,  # noqa: F401 — re-export consumed by tests
)
from app.runners.stream_runtime import (
    run_stream_lifecycle as _run_stream_lifecycle_impl,
    start_health_server as _start_health_server,
)
from app.runners.stream_event_queue import LatestPerKeyDispatcher, StreamEventQueue
from app.runners.stream_strategy_helpers import (
    iter_instruments as _iter_instruments,
    normalize_strategy_entries as _normalize_strategy_entries,
    resolve_delta_qty_config as _resolve_delta_qty_config,
)

try:
    from app.data.postgres import connect_with_retry
except Exception:  # optional Postgres support in some local setups
    connect_with_retry = None

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
_POSTGRES_NAME_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ANGEL_INTERVAL_BY_TIMEFRAME = {
    60: "ONE_MINUTE",
    180: "THREE_MINUTE",
    300: "FIVE_MINUTE",
    600: "TEN_MINUTE",
    900: "FIFTEEN_MINUTE",
    1800: "THIRTY_MINUTE",
    3600: "ONE_HOUR",
}


def _refresh_strategy_instances_after_atm_update(
    strategies: List[Dict[str, Any]],
    new_instrument_meta: Dict[str, Dict[str, Any]],
    *,
    previous_instrument_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    reconcile_open_legs: bool = False,
) -> Dict[str, int]:
    summary = {
        "strategies": 0,
        "open_legs_before": 0,
        "open_legs_after": 0,
        "reconciled": 0,
        "cleared": 0,
        "degraded": 0,
    }
    for strat in strategies:
        inst = strat.get("instance")
        if inst is None:
            continue
        summary["strategies"] += 1
        open_legs_before = len(getattr(inst, "open_legs", {}) or {})
        summary["open_legs_before"] += open_legs_before
        if hasattr(inst, "refresh_atm_labels"):
            inst.refresh_atm_labels(new_instrument_meta)
        if hasattr(inst, "refresh_meta"):
            inst.refresh_meta(new_instrument_meta)
        if hasattr(inst, "open_legs"):
            if reconcile_open_legs and hasattr(inst, "reconcile_open_legs"):
                result = inst.reconcile_open_legs(
                    new_instrument_meta,
                    previous_instrument_meta=previous_instrument_meta,
                )
                summary["reconciled"] += int(result.get("rebound", 0) or 0) + int(
                    result.get("preserved", 0) or 0
                )
            else:
                summary["degraded"] += 1
                if hasattr(inst, "_degraded"):
                    inst._degraded = True
                strategy_name = getattr(inst, "strategy_name", "unknown")
                degraded_scope_manager.enter_degraded(
                    scope_key=f"strategy:{strategy_name}",
                    reason=DegradedReason.ATM_REMAP_FAILED,
                    metadata={"strategy": strategy_name},
                )
                logger.warning(
                    "ATM refresh: strategy %s marked DEGRADED — reconcile_open_legs "
                    "unavailable, open_legs preserved (not cleared). "
                    "New entries blocked until reconciliation.",
                    strategy_name,
                )
            summary["open_legs_after"] += len(getattr(inst, "open_legs", {}) or {})
    return summary

# Allow disabling trading window filter for testing/debugging
_DISABLE_TRADING_WINDOW_FILTER = os.getenv("DISABLE_TRADING_WINDOW_FILTER", "false").lower() == "true"

_daily_levels_state: dict[str, Any] = {"cache": None, "instruments": None}
RUNTIME_EXIT_STRATEGY_ID = StrategyId("__runtime_exit__")


def get_active_instrument_count() -> int:
    """Return the number of instruments currently in the stream universe."""
    instruments = _daily_levels_state.get("instruments")
    if instruments is None:
        return 0
    return len(instruments)


# Refresh the daily levels cache on demand.
def refresh_daily_levels_cache() -> bool:
    cache = _daily_levels_state.get("cache")
    instruments = _daily_levels_state.get("instruments") or []
    if cache is None:
        logger.warning("Daily levels cache not initialized; stream not started yet")
        return False
    try:
        cache.refresh_for_instruments(instruments)
        return True
    except Exception as exc:
        logger.warning("Daily levels manual refresh failed: %s", exc)
        return False


# Interpret a boolean-like environment variable.
def _is_true_env(var_name: str, default: bool = True) -> bool:
    val = os.getenv(var_name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


# Parse an optional integer value safely.
def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# Log a compact summary of per-underlying instrument policy gates.
def _log_instrument_policy_snapshot(instrument_controller: InstrumentController) -> None:
    try:
        snapshot = instrument_controller.snapshot()
    except Exception as exc:
        logger.warning("Failed to capture instrument policy snapshot: %s", exc)
        return
    if not snapshot:
        logger.info("Instrument policy snapshot: no explicit per-underlying overrides.")
        return
    logger.info(
        "Instrument policy snapshot: %d underlyings configured.",
        len(snapshot),
    )
    for underlying in sorted(snapshot.keys()):
        row = snapshot.get(underlying, {})
        enabled = bool(row.get("enabled", True))
        allowed = list(row.get("allowed_strategies", []) or [])
        if not allowed:
            allowed_desc = "ALL (empty allow-list)"
        else:
            allowed_desc = ",".join(str(x) for x in allowed)
        logger.info(
            "Instrument policy | underlying=%s enabled=%s allowed=%s",
            underlying,
            enabled,
            allowed_desc,
        )


# Resolve strategy_id from a strategy instance if available.
def _strategy_id_for_instance(instance: Any) -> Optional[str]:
    try:
        strategy_id = getattr(instance, "_strategy_id", None)
    except Exception:
        return None
    if strategy_id in (None, ""):
        return None
    return str(strategy_id)


# Validate route availability for strategy attachments and optionally drop unrouted ones.
def _filter_strategies_with_missing_routes(
    strategies: List[Dict[str, Any]],
    *,
    routing_table: Any,
    fail_fast: bool,
) -> List[Dict[str, Any]]:
    route_presence: Dict[str, bool] = {}
    filtered: List[Dict[str, Any]] = []
    dropped: List[tuple[str, str, str]] = []

    for row in strategies:
        name = str(row.get("name") or "")
        underlying = str(row.get("underlying") or "")
        instance = row.get("instance")
        strategy_id = _strategy_id_for_instance(instance)
        if not strategy_id:
            filtered.append(row)
            continue

        has_routes = route_presence.get(strategy_id)
        if has_routes is None:
            try:
                routes = routing_table.get_routes_for_strategy(StrategyId(strategy_id))
                has_routes = bool(routes)
            except Exception:
                logger.exception(
                    "Route validation failed for strategy_id=%s; treating as missing route",
                    strategy_id,
                )
                has_routes = False
            route_presence[strategy_id] = bool(has_routes)

        if has_routes:
            filtered.append(row)
            continue

        dropped.append((name, underlying, strategy_id))
        logger.warning(
            "Disabling strategy attachment due to missing hub route | strategy=%s underlying=%s strategy_id=%s",
            name,
            underlying,
            strategy_id,
        )

    if dropped:
        summary = ", ".join(
            f"{name}@{underlying}({strategy_id})"
            for name, underlying, strategy_id in dropped
        )
        if fail_fast:
            raise RuntimeError(
                f"Hub route validation failed; missing routes for: {summary}"
            )
        logger.warning(
            "Hub route validation dropped %d strategy attachment(s): %s",
            len(dropped),
            summary,
        )
    return filtered


_STARTUP_SNAPSHOT_REDACT_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PIN",
    "KEY",
    "JWT",
)
_STARTUP_SNAPSHOT_REDACT_VALUE = "***REDACTED***"
_STARTUP_SNAPSHOT_ALLOWED_ENV_PREFIXES = (
    "AUTO_STRATEGY_",
    "DISABLE_TRADING_WINDOW_FILTER",
    "HUB_ROUTE_VALIDATION_FAIL_FAST",
    "INTRADAY_",
    "TRADE_MODE",
    "ALLOW_LEGACY_EMA20_QTY_IN_LIVE",
)
_REQUIRED_RUNTIME_ATTACHMENT_NAMES = {
    "ema20_strategy",
    "ce_orb",
    "put_momentum_scalper",
}


def _is_startup_snapshot_sensitive_key(key: str) -> bool:
    key_upper = str(key or "").strip().upper()
    return any(marker in key_upper for marker in _STARTUP_SNAPSHOT_REDACT_MARKERS)


def _redact_startup_snapshot_payload(payload: Any, *, key: str = "") -> Any:
    if _is_startup_snapshot_sensitive_key(key):
        return _STARTUP_SNAPSHOT_REDACT_VALUE
    if isinstance(payload, dict):
        return {
            str(child_key): _redact_startup_snapshot_payload(
                child_value,
                key=str(child_key),
            )
            for child_key, child_value in payload.items()
        }
    if isinstance(payload, list):
        return [
            _redact_startup_snapshot_payload(item, key=key)
            for item in payload
        ]
    return payload


def _collect_startup_snapshot_env(env: Mapping[str, Any]) -> Dict[str, Any]:
    collected: Dict[str, Any] = {}
    for raw_key, raw_value in sorted((env or {}).items()):
        key = str(raw_key or "").strip()
        if not key:
            continue
        key_upper = key.upper()
        if not (
            "EMA20" in key_upper
            or "PUT_MOM" in key_upper
            or key_upper.startswith(_STARTUP_SNAPSHOT_ALLOWED_ENV_PREFIXES)
            or _is_startup_snapshot_sensitive_key(key_upper)
        ):
            continue
        collected[key_upper] = raw_value
    return _redact_startup_snapshot_payload(collected)


def _stable_snapshot_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_startup_snapshot_ema_periods(
    ema_periods_by_timeframe: Mapping[Any, Any],
) -> Dict[str, List[int]]:
    normalized: Dict[str, List[int]] = {}
    for raw_tf, raw_periods in (ema_periods_by_timeframe or {}).items():
        try:
            timeframe_seconds = int(raw_tf)
        except (TypeError, ValueError):
            continue
        periods: set[int] = set()
        for raw_period in raw_periods or []:
            try:
                period = int(raw_period)
            except (TypeError, ValueError):
                continue
            if period > 0:
                periods.add(period)
        normalized[str(timeframe_seconds)] = sorted(periods)
    return dict(sorted(normalized.items(), key=lambda item: int(item[0])))


def _build_indicator_plan_snapshot(
    *,
    engine_timeframes: Iterable[int],
    ema_periods_by_timeframe: Mapping[Any, Any],
    adx_period: Any,
) -> Dict[str, Any]:
    normalized_timeframes: List[int] = []
    for raw_timeframe in engine_timeframes or []:
        try:
            timeframe_seconds = int(raw_timeframe)
        except (TypeError, ValueError):
            continue
        if timeframe_seconds > 0:
            normalized_timeframes.append(timeframe_seconds)
    try:
        normalized_adx_period = int(adx_period)
    except (TypeError, ValueError):
        normalized_adx_period = None
    return {
        "engine_timeframes": sorted(set(normalized_timeframes)),
        "ema_periods_by_timeframe": _normalize_startup_snapshot_ema_periods(
            ema_periods_by_timeframe
        ),
        "adx_period": normalized_adx_period,
    }


def _build_indicator_seed_summary(
    *,
    seed_source: str,
    requested_bars_per_series: int,
    seeded_bars: int,
    seed_bars: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    series_counts: Dict[tuple[str, int], int] = {}
    for row in seed_bars or []:
        label = str(row.get("label") or "").strip().upper()
        timeframe_seconds = _safe_int(row.get("timeframe_seconds"))
        if not label or timeframe_seconds is None:
            continue
        key = (label, int(timeframe_seconds))
        series_counts[key] = int(series_counts.get(key, 0)) + 1
    per_series = [
        {
            "label": label,
            "timeframe_seconds": timeframe_seconds,
            "bars": bars,
        }
        for (label, timeframe_seconds), bars in sorted(series_counts.items())
    ]
    return {
        "seed_source": str(seed_source or "none"),
        "requested_bars_per_series": int(max(0, requested_bars_per_series)),
        "seeded_bars": int(max(0, seeded_bars)),
        "seeded_series_count": len(per_series),
        "per_series": per_series,
    }


def _attached_strategies_by_underlying(
    strategies: Iterable[Mapping[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in strategies or []:
        underlying = str(row.get("underlying") or "").strip().upper()
        name = canonicalize_strategy_name(
            str(row.get("name") or "").strip(),
            source="stream.startup_snapshot",
            warn_alias=False,
        )
        if not underlying or not name:
            continue
        instance = row.get("instance")
        item: Dict[str, Any] = {
            "name": name,
            "class_name": (
                instance.__class__.__name__
                if instance is not None
                else None
            ),
        }
        strategy_id = _strategy_id_for_instance(instance)
        if strategy_id:
            item["strategy_id"] = strategy_id
        grouped.setdefault(underlying, []).append(item)
    for underlying, items in grouped.items():
        grouped[underlying] = sorted(
            items,
            key=lambda item: (
                str(item.get("name") or ""),
                str(item.get("strategy_id") or ""),
                str(item.get("class_name") or ""),
            ),
        )
    return grouped


def _selector_config_snapshot(selector_config: Any) -> Dict[str, Any]:
    if selector_config is None:
        return {}
    mapping: Dict[str, Dict[str, List[str]]] = {}
    raw_mapping = getattr(selector_config, "mapping", {}) or {}
    for underlying, regime_map in raw_mapping.items():
        if not isinstance(regime_map, dict):
            continue
        mapping[str(underlying)] = {
            getattr(regime, "value", str(regime)): list(strategies or [])
            for regime, strategies in regime_map.items()
        }
    return {
        "enabled": bool(getattr(selector_config, "enabled", False)),
        "max_active_per_underlying": int(
            getattr(selector_config, "max_active_per_underlying", 1)
        ),
        "min_hold_seconds": float(
            getattr(selector_config, "min_hold_seconds", 0.0)
        ),
        "ema20_is_authoritative": bool(
            getattr(selector_config, "ema20_is_authoritative", False)
        ),
        "mapping": mapping,
    }


def _log_strategy_bar_skip(
    *,
    logger_obj: logging.Logger,
    bar_label: str,
    underlying_label: str,
    timeframe_seconds: int,
    candle: Any,
    strategy_name: str,
    strategy_underlying: str,
    reason: str,
) -> None:
    log_event(
        logger_obj,
        event_type="STRATEGY_BAR_SKIP",
        message="Skipped strategy bar dispatch before strategy callback.",
        instrument=str(bar_label or ""),
        underlying=str(underlying_label or "").strip().upper(),
        strategy=str(strategy_name or ""),
        strategy_underlying=str(strategy_underlying or "").strip().upper(),
        reason=str(reason or ""),
        timeframe_seconds=int(timeframe_seconds),
        bar_start_ts=_to_utc_iso(getattr(candle, "start_ts", None)),
    )


def _should_dispatch_strategy_bar(
    *,
    bar_label: str,
    underlying_label: str,
    timeframe_seconds: int,
    candle: Any,
    bar_ts: Optional[datetime],
    strategy_row: Mapping[str, Any],
    strategy_switch: StrategySwitchboard,
    instrument_controller: InstrumentController,
    is_strategy_selected: Callable[[str, str], bool],
    is_strict_target_strategy: Callable[[Any], bool],
    strict_cutoff_reached: Callable[[datetime], bool],
) -> bool:
    strategy_name = str(strategy_row.get("name") or "")
    strategy_underlying = str(strategy_row.get("underlying") or "")
    if not strategy_switch.is_enabled(strategy_name):
        _log_strategy_bar_skip(
            logger_obj=logger,
            bar_label=bar_label,
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            strategy_name=strategy_name,
            strategy_underlying=strategy_underlying,
            reason="strategy_switch_disabled",
        )
        return False
    if not instrument_controller.is_strategy_allowed(
        strategy_underlying,
        strategy_name,
    ):
        _log_strategy_bar_skip(
            logger_obj=logger,
            bar_label=bar_label,
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            strategy_name=strategy_name,
            strategy_underlying=strategy_underlying,
            reason="instrument_policy_blocked",
        )
        return False
    if not is_strategy_selected(strategy_underlying, strategy_name):
        _log_strategy_bar_skip(
            logger_obj=logger,
            bar_label=bar_label,
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            strategy_name=strategy_name,
            strategy_underlying=strategy_underlying,
            reason="selector_blocked",
        )
        return False
    if strategy_underlying != underlying_label:
        _log_strategy_bar_skip(
            logger_obj=logger,
            bar_label=bar_label,
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            strategy_name=strategy_name,
            strategy_underlying=strategy_underlying,
            reason="underlying_mismatch",
        )
        return False
    if (
        isinstance(bar_ts, datetime)
        and is_strict_target_strategy(strategy_name)
        and strict_cutoff_reached(bar_ts)
    ):
        _log_strategy_bar_skip(
            logger_obj=logger,
            bar_label=bar_label,
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            strategy_name=strategy_name,
            strategy_underlying=strategy_underlying,
            reason="strict_intraday_cutoff_blocked",
        )
        return False
    return True


def _build_strategy_runtime_startup_snapshot(
    *,
    process_start_ts: datetime,
    trade_mode: str,
    strategy_cfg: Mapping[str, Any],
    env: Mapping[str, Any],
    strategies: Iterable[Mapping[str, Any]],
    instrument_policy_snapshot: Mapping[str, Any],
    strategy_switch_snapshot: Mapping[str, Any],
    selector_config: Any,
    selector_state: Mapping[str, Any],
    strict_intraday_state: Mapping[str, Any],
    selector_staleness_check: Mapping[str, Any] | None = None,
    unroutable_strategies: list[str] | None = None,
    indicator_plan_snapshot: Mapping[str, Any] | None = None,
    indicator_seed_summary: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    env_snapshot = _collect_startup_snapshot_env(env)
    selector_snapshot = _selector_config_snapshot(selector_config)
    config_material = {
        "strategy_cfg": strategy_cfg,
        "env_overrides": env_snapshot,
        "instrument_policies": instrument_policy_snapshot,
        "strategy_switch": strategy_switch_snapshot,
        "selector": selector_snapshot,
        "strict_intraday": strict_intraday_state,
        "indicator_plan": indicator_plan_snapshot or {},
        "indicator_seed_summary": indicator_seed_summary or {},
    }
    return {
        "process_start_ts": _to_utc_iso(process_start_ts),
        "trade_mode": str(trade_mode or "").strip().upper(),
        "config_hash": _stable_snapshot_hash(config_material),
        "attached_strategies_by_underlying": _attached_strategies_by_underlying(
            strategies
        ),
        "instrument_policies": _redact_startup_snapshot_payload(
            dict(instrument_policy_snapshot or {})
        ),
        "strategy_switch": _redact_startup_snapshot_payload(
            dict(strategy_switch_snapshot or {})
        ),
        "selector": _redact_startup_snapshot_payload(selector_snapshot),
        "selector_state": _redact_startup_snapshot_payload(
            dict(selector_state or {})
        ),
        "selector_staleness_check": dict(selector_staleness_check or {"status": "passed"}),
        "unroutable_strategies": sorted(unroutable_strategies or []),
        "strict_intraday": _redact_startup_snapshot_payload(
            dict(strict_intraday_state or {})
        ),
        "indicator_plan": _redact_startup_snapshot_payload(
            dict(indicator_plan_snapshot or {})
        ),
        "indicator_seed_summary": _redact_startup_snapshot_payload(
            dict(indicator_seed_summary or {})
        ),
        "env_overrides": env_snapshot,
    }


def _persist_strategy_runtime_startup_snapshot(
    *,
    snapshot: Mapping[str, Any],
    logs_dir: Path,
) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    process_start_iso = str(snapshot.get("process_start_ts") or "").replace(":", "")
    process_start_iso = process_start_iso.replace("-", "").replace(".", "")
    process_start_iso = process_start_iso.replace("+0000", "Z").replace("+00:00", "Z")
    file_name = (
        f"strategy_runtime_startup_snapshot_{process_start_iso or 'unknown'}.json"
    )
    snapshot_path = logs_dir / file_name
    snapshot_path.write_text(
        json.dumps(snapshot, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return snapshot_path


def _validate_required_strategy_attachments(
    *,
    trade_mode: str,
    strategies: Iterable[Mapping[str, Any]],
    instrument_policy_snapshot: Mapping[str, Any],
    enabled_strategy_names: Iterable[str],
    required_strategy_names: Iterable[str] = _REQUIRED_RUNTIME_ATTACHMENT_NAMES,
) -> List[tuple[str, str]]:
    enabled = set(
        validate_strategy_list(
            [
                str(name).strip()
                for name in (enabled_strategy_names or [])
                if str(name or "").strip()
            ],
            context="stream.attachment_validation.enabled",
        )
    )
    required = set(
        validate_strategy_list(
            [
                str(name).strip()
                for name in (required_strategy_names or [])
                if str(name or "").strip()
            ],
            context="stream.attachment_validation.required",
        )
    )
    target_names = enabled & required
    if not target_names:
        return []

    expected_pairs: set[tuple[str, str]] = set()
    for raw_underlying, raw_cfg in (instrument_policy_snapshot or {}).items():
        cfg = raw_cfg if isinstance(raw_cfg, Mapping) else {}
        if not bool(cfg.get("enabled", True)):
            continue
        allowed = set(
            validate_strategy_list(
                [
                    str(name).strip()
                    for name in (cfg.get("allowed_strategies") or [])
                    if str(name or "").strip()
                ],
                context=f"stream.attachment_validation.allowed.{raw_underlying}",
            )
        )
        if not allowed:
            continue
        underlying = str(raw_underlying or "").strip().upper()
        for strategy_name in sorted(allowed & target_names):
            expected_pairs.add((underlying, strategy_name))

    actual_pairs = {
        (
            str(row.get("underlying") or "").strip().upper(),
            canonicalize_strategy_name(
                str(row.get("name") or "").strip(),
                source="stream.attachment_validation.actual",
                warn_alias=False,
            ),
        )
        for row in strategies or []
        if str(row.get("underlying") or "").strip()
        and str(row.get("name") or "").strip()
    }
    actual_pairs = {
        pair for pair in actual_pairs if pair[0] and pair[1]
    }
    missing = sorted(expected_pairs - actual_pairs)
    if not missing:
        return []

    missing_desc = ", ".join(
        f"{underlying}:{strategy_name}"
        for underlying, strategy_name in missing
    )
    message = (
        "Required runtime strategy attachment(s) missing after startup validation: "
        f"{missing_desc}"
    )
    if str(trade_mode or "").strip().upper() == "LIVE":
        raise RuntimeError(message)
    logger.warning(message)
    return missing


# Load indicator history bars from a prior indicator CSV.
def _load_indicator_seed_bars_from_csv(
    csv_path: Optional[str],
    *,
    allowed_labels: Optional[set[str]] = None,
    allowed_timeframes: Optional[set[int]] = None,
    max_bars_per_series: int = 0,
) -> List[Dict[str, Any]]:
    if not csv_path or not os.path.exists(csv_path):
        return []
    try:
        import pandas as pd
    except Exception as exc:
        logger.warning("Indicator seed skipped; pandas unavailable: %s", exc)
        return []
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        logger.warning("Indicator seed skipped; failed reading %s: %s", csv_path, exc)
        return []

    required_cols = {"ts_start", "label", "timeframe_seconds", "o", "h", "l", "c"}
    missing = required_cols - set(df.columns)
    if missing:
        logger.warning(
            "Indicator seed skipped; missing columns in %s: %s",
            csv_path,
            sorted(missing),
        )
        return []

    df["ts_start"] = pd.to_datetime(df["ts_start"], utc=True, errors="coerce")
    if "ts_end" in df.columns:
        df["ts_end"] = pd.to_datetime(df["ts_end"], utc=True, errors="coerce")
    else:
        df["ts_end"] = pd.NaT
    df["timeframe_seconds"] = pd.to_numeric(df["timeframe_seconds"], errors="coerce")

    df = df.dropna(subset=["timeframe_seconds"])
    df["timeframe_seconds"] = df["timeframe_seconds"].astype(int)
    if allowed_timeframes:
        df = df[df["timeframe_seconds"].isin(allowed_timeframes)]
    if allowed_labels:
        df = df[df["label"].isin(allowed_labels)]
    df = df.dropna(subset=["ts_start", "c", "label"])
    df = df.drop_duplicates(
        subset=["label", "timeframe_seconds", "ts_start"], keep="last"
    )
    df = df.sort_values(["label", "timeframe_seconds", "ts_start"])
    if max_bars_per_series and max_bars_per_series > 0:
        df = df.groupby(["label", "timeframe_seconds"], group_keys=False).tail(
            max_bars_per_series
        )
    df = df.sort_values(["ts_start", "label", "timeframe_seconds"])

    bars: List[Dict[str, Any]] = []
    for row in df.itertuples(index=False):
        ts_start = row.ts_start
        if hasattr(ts_start, "to_pydatetime"):
            ts_start = ts_start.to_pydatetime()
        ts_end = None
        if hasattr(row, "ts_end"):
            ts_end = row.ts_end
            if hasattr(ts_end, "to_pydatetime"):
                ts_end = ts_end.to_pydatetime()
            if pd.isna(ts_end):
                ts_end = None
        if ts_end is None:
            try:
                ts_end = ts_start + timedelta(seconds=int(row.timeframe_seconds))
            except Exception:
                ts_end = ts_start
        try:
            bars.append(
                {
                    "ts_start": ts_start,
                    "ts_end": ts_end,
                    "label": str(row.label),
                    "timeframe_seconds": int(row.timeframe_seconds),
                    "o": float(row.o),
                    "h": float(row.h),
                    "l": float(row.l),
                    "c": float(row.c),
                }
            )
        except Exception:
            continue

    return bars


def _quoted_postgres_table_name(table_name: str) -> str:
    raw = (table_name or "indicator_bars").strip()
    parts = [p for p in raw.split(".") if p]
    if not parts or len(parts) > 2:
        raise ValueError(f"Invalid postgres table name: {raw!r}")
    for part in parts:
        if not _POSTGRES_NAME_PART.match(part):
            raise ValueError(f"Invalid postgres table identifier: {part!r}")
    return ".".join(f'"{part}"' for part in parts)


def _coerce_utc_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    else:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _load_indicator_seed_bars_from_postgres(
    postgres_dsn: Optional[str],
    postgres_table: str,
    *,
    allowed_timeframes: Optional[set[int]] = None,
    allowed_labels: Optional[set[str]] = None,
    start_ts: Optional[datetime] = None,
    end_ts: Optional[datetime] = None,
    max_bars_per_series: int = 0,
) -> List[Dict[str, Any]]:
    if not postgres_dsn:
        return []
    if connect_with_retry is None:
        logger.warning("Indicator seed skipped; Postgres connector unavailable")
        return []
    try:
        table_sql = _quoted_postgres_table_name(postgres_table)
    except ValueError as exc:
        logger.warning(
            "Indicator seed skipped; invalid INDICATOR_POSTGRES_TABLE=%r: %s",
            postgres_table,
            exc,
        )
        return []

    timeframes = sorted(int(tf) for tf in (allowed_timeframes or []))
    if not timeframes:
        return []

    where_clauses: List[str] = ["ts_start IS NOT NULL"]
    params: List[Any] = []

    tf_placeholders = ", ".join(["%s"] * len(timeframes))
    where_clauses.append(f"timeframe_seconds IN ({tf_placeholders})")
    params.extend(timeframes)

    labels: List[str] = []
    if allowed_labels:
        labels = sorted({str(label) for label in allowed_labels if label})
        if not labels:
            return []
        label_placeholders = ", ".join(["%s"] * len(labels))
        where_clauses.append(f"label IN ({label_placeholders})")
        params.extend(labels)

    if start_ts is not None:
        where_clauses.append("ts_start >= %s")
        params.append(start_ts)
    if end_ts is not None:
        where_clauses.append("ts_start < %s")
        params.append(end_ts)

    if start_ts is not None or end_ts is not None:
        query = f"""
            SELECT ts_start, ts_end, label, timeframe_seconds, o, h, l, c
            FROM {table_sql}
            WHERE {" AND ".join(where_clauses)}
            ORDER BY ts_start ASC, label ASC, timeframe_seconds ASC
        """
    else:
        if max_bars_per_series <= 0:
            return []
        params.append(int(max_bars_per_series))
        query = f"""
            SELECT ts_start, ts_end, label, timeframe_seconds, o, h, l, c
            FROM (
                SELECT
                    ts_start,
                    ts_end,
                    label,
                    timeframe_seconds,
                    o,
                    h,
                    l,
                    c,
                    ROW_NUMBER() OVER (
                        PARTITION BY label, timeframe_seconds
                        ORDER BY ts_start DESC
                    ) AS row_num
                FROM {table_sql}
                WHERE {" AND ".join(where_clauses)}
            ) seeded
            WHERE row_num <= %s
            ORDER BY ts_start ASC, label ASC, timeframe_seconds ASC
        """

    conn = None
    try:
        conn = connect_with_retry(postgres_dsn, autocommit=True, max_attempts=3)
    except Exception as exc:
        logger.warning("Indicator seed skipped; failed Postgres connect: %s", exc)
        return []

    rows: List[Any] = []
    try:
        with conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = list(cur.fetchall() or [])
    except Exception as exc:
        logger.warning(
            "Indicator seed skipped; failed reading Postgres table %s: %s",
            postgres_table,
            exc,
        )
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass

    bars: List[Dict[str, Any]] = []
    for row in rows:
        try:
            ts_start = _coerce_utc_timestamp(row[0])
            ts_end = _coerce_utc_timestamp(row[1])
            label = str(row[2])
            timeframe_seconds = int(row[3])
            if not ts_start or timeframe_seconds not in timeframes:
                continue
            if ts_end is None:
                ts_end = ts_start + timedelta(seconds=timeframe_seconds)
            bars.append(
                {
                    "ts_start": ts_start,
                    "ts_end": ts_end,
                    "label": label,
                    "timeframe_seconds": timeframe_seconds,
                    "o": float(row[4]),
                    "h": float(row[5]),
                    "l": float(row[6]),
                    "c": float(row[7]),
                }
            )
        except Exception:
            continue

    return bars


def _load_indicator_gap_bars_from_postgres(
    postgres_dsn: Optional[str],
    postgres_table: str,
    *,
    label: str,
    timeframe_seconds: int,
    start_ts: datetime,
    end_ts: datetime,
) -> List[Dict[str, Any]]:
    return _load_indicator_seed_bars_from_postgres(
        postgres_dsn,
        postgres_table,
        allowed_timeframes={int(timeframe_seconds)},
        allowed_labels={str(label)},
        start_ts=start_ts,
        end_ts=end_ts,
        max_bars_per_series=0,
    )


def _indicator_gap_block_reason(timeframe_seconds: int) -> str:
    return f"indicator_gap_data_stale:{int(timeframe_seconds)}"


def _set_tick_flow_metrics(
    metrics: Any,
    *,
    event_queue: Any,
    tick_dispatcher: Any,
    processing_lag_ms: Optional[float] = None,
) -> None:
    labels = {"queue": "stream"}
    if processing_lag_ms is not None:
        metrics.set_gauge(
            "stream_tick_processing_lag_ms",
            labels=labels,
            value=max(0.0, float(processing_lag_ms)),
        )
    metrics.set_gauge(
        "stream_tick_queue_pending",
        labels=labels,
        value=float(event_queue.pending_count()),
    )
    metrics.set_gauge(
        "stream_tick_coalesced_keys",
        labels=labels,
        value=float(tick_dispatcher.active_key_count()),
    )
    metrics.set_gauge(
        "stream_tick_throttle_active",
        labels=labels,
        value=1.0 if tick_dispatcher.is_throttling() else 0.0,
    )


def _record_tick_throttle_action(metrics: Any, action: str) -> None:
    normalized = str(action or "").strip().lower()
    if normalized not in {"merged", "dropped"}:
        return
    metrics.inc_counter(
        "stream_tick_throttle_total",
        labels={"action": normalized},
    )


def _expected_gap_start_times(request: GapBackfillRequest) -> List[datetime]:
    expected: List[datetime] = []
    cursor = request.gap_start_ts
    while cursor < request.next_bucket_start_ts:
        expected.append(cursor)
        cursor = cursor + timedelta(seconds=request.timeframe_seconds)
    return expected


def _merge_indicator_gap_bars(*sources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[tuple[str, int, datetime], Dict[str, Any]] = {}
    for source in sources:
        for bar in source or []:
            ts_start = bar.get("ts_start")
            if not isinstance(ts_start, datetime):
                continue
            label = str(bar.get("label") or "")
            try:
                timeframe_seconds = int(bar.get("timeframe_seconds"))
            except (TypeError, ValueError):
                continue
            merged[(label, timeframe_seconds, ts_start)] = dict(bar)
    return [
        merged[key]
        for key in sorted(
            merged,
            key=lambda item: (item[2], item[0], item[1]),
        )
    ]


def _indicator_gap_is_covered(
    request: GapBackfillRequest,
    bars: Iterable[Dict[str, Any]],
) -> bool:
    expected = _expected_gap_start_times(request)
    if not expected:
        return False
    actual = {
        bar.get("ts_start")
        for bar in bars or []
        if isinstance(bar.get("ts_start"), datetime)
        and str(bar.get("label") or "") == request.label
        and int(bar.get("timeframe_seconds") or 0) == request.timeframe_seconds
    }
    return all(ts_start in actual for ts_start in expected)


def _timeframe_to_angel_interval(timeframe_seconds: int) -> Optional[str]:
    return _ANGEL_INTERVAL_BY_TIMEFRAME.get(int(timeframe_seconds))


def _parse_angel_candle_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, (int, float)):
        ts = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    return ts.astimezone(timezone.utc)


def _load_indicator_gap_bars_from_broker(
    order_client: Optional[AngelOrderClient],
    instrument_meta: Dict[str, Dict[str, Any]],
    request: GapBackfillRequest,
) -> List[Dict[str, Any]]:
    if order_client is None:
        return []
    interval = _timeframe_to_angel_interval(request.timeframe_seconds)
    if not interval:
        logger.warning(
            "Indicator gap backfill skipped for %s tf=%ss: unsupported timeframe",
            request.label,
            request.timeframe_seconds,
        )
        return []
    meta = instrument_meta.get(request.label) or {}
    if str(meta.get("kind") or "").upper() != "UNDERLYING":
        return []
    token = meta.get("token")
    exchange = normalize_historical_exchange(
        meta.get("exchange") or meta.get("segment") or meta.get("exch_seg")
    )
    if not token or not exchange:
        return []
    try:
        candles = order_client.fetch_historical_candles(
            exchange=exchange,
            symboltoken=str(token),
            interval=interval,
            from_datetime=request.gap_start_ts,
            to_datetime=request.next_bucket_start_ts,
        )
    except Exception as exc:
        logger.warning(
            "Indicator gap broker backfill failed for %s tf=%ss: %s",
            request.label,
            request.timeframe_seconds,
            exc,
        )
        return []

    bars: List[Dict[str, Any]] = []
    for candle in candles:
        if not isinstance(candle, (list, tuple)) or len(candle) < 5:
            continue
        ts_start = _parse_angel_candle_timestamp(candle[0])
        if ts_start is None:
            continue
        if ts_start < request.gap_start_ts or ts_start >= request.next_bucket_start_ts:
            continue
        try:
            o = float(candle[1])
            h = float(candle[2])
            low = float(candle[3])
            c = float(candle[4])
        except (TypeError, ValueError):
            continue
        bars.append(
            {
                "ts_start": ts_start,
                "ts_end": ts_start + timedelta(seconds=request.timeframe_seconds),
                "label": request.label,
                "timeframe_seconds": request.timeframe_seconds,
                "o": o,
                "h": h,
                "l": low,
                "c": c,
            }
        )
    bars.sort(key=lambda bar: bar["ts_start"])
    return bars


def _resolve_indicator_gap_backfill(
    request: GapBackfillRequest,
    *,
    instrument_meta: Dict[str, Dict[str, Any]],
    label_to_underlying: Dict[str, str],
    maintenance: Optional[MaintenanceManager],
    postgres_dsn: Optional[str],
    postgres_table: str,
    order_client: Optional[AngelOrderClient],
    broker_backfill_enabled: bool,
) -> List[Dict[str, Any]]:
    meta = instrument_meta.get(request.label) or {}
    if str(meta.get("kind") or "").upper() != "UNDERLYING":
        return []
    block_reason = _indicator_gap_block_reason(request.timeframe_seconds)
    underlying_label = label_to_underlying.get(request.label, request.label)
    bars: List[Dict[str, Any]] = []
    if postgres_dsn:
        bars = _load_indicator_gap_bars_from_postgres(
            postgres_dsn,
            postgres_table,
            label=request.label,
            timeframe_seconds=request.timeframe_seconds,
            start_ts=request.gap_start_ts,
            end_ts=request.next_bucket_start_ts,
        )
    if (
        not _indicator_gap_is_covered(request, bars)
        and broker_backfill_enabled
        and order_client is not None
    ):
        broker_bars = _load_indicator_gap_bars_from_broker(
            order_client,
            instrument_meta,
            request,
        )
        bars = _merge_indicator_gap_bars(bars, broker_bars)
    if _indicator_gap_is_covered(request, bars):
        if maintenance is not None:
            maintenance.clear_entry_block(
                underlying_label,
                reason=block_reason,
            )
        return bars
    if maintenance is not None:
        maintenance.block_new_entries(
            underlying_label,
            reason=block_reason,
        )
    logger.warning(
        "Indicator gap unresolved for %s tf=%ss gap_start=%s next_bucket=%s; blocking new entries",
        request.label,
        request.timeframe_seconds,
        request.gap_start_ts.isoformat(),
        request.next_bucket_start_ts.isoformat(),
    )
    return []


def _seed_indicators_from_postgres(
    engine: MultiInstrumentIndicatorEngine,
    postgres_dsn: Optional[str],
    postgres_table: str,
    *,
    allowed_labels: Optional[set[str]] = None,
    max_bars_per_series: int = 0,
) -> int:
    bars = _load_indicator_seed_bars_from_postgres(
        postgres_dsn,
        postgres_table,
        allowed_timeframes=set(engine.timeframes),
        allowed_labels=allowed_labels,
        max_bars_per_series=max_bars_per_series,
    )
    if not bars:
        return 0
    return engine.seed_from_closed_bars(bars)


def _replay_indicator_seed_bars(
    *,
    seed_bars: List[Dict[str, Any]],
    timeframes: List[int],
    ema_periods_by_timeframe: Dict[int, List[int]],
    on_bar: Callable[[str, int, Any, Dict[str, Any]], None],
    allowed_labels: Optional[set[str]] = None,
    atr_period: int = 14,
    rsi_period: int = 14,
    adx_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    max_candles: int = 1000,
) -> int:
    if not seed_bars or not timeframes:
        return 0

    label_filter = {str(label) for label in (allowed_labels or set()) if label}
    replay_bars: List[Dict[str, Any]] = []
    for bar in seed_bars:
        label = str(bar.get("label") or "")
        if label_filter and label not in label_filter:
            continue
        replay_bars.append(bar)
    if not replay_bars:
        return 0

    def _effective_end_ts(bar: Dict[str, Any]) -> datetime:
        ts_end = _coerce_utc_timestamp(bar.get("ts_end"))
        if ts_end is not None:
            return ts_end
        ts_start = _coerce_utc_timestamp(bar.get("ts_start"))
        timeframe_seconds = _safe_int(bar.get("timeframe_seconds")) or 0
        if ts_start is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return ts_start + timedelta(seconds=max(0, timeframe_seconds))

    replay_bars.sort(
        key=lambda bar: (
            _effective_end_ts(bar),
            str(bar.get("label") or ""),
            _safe_int(bar.get("timeframe_seconds")) or 0,
        )
    )

    replay_engine = MultiInstrumentIndicatorEngine(
        timeframes=timeframes,
        atr_period=atr_period,
        rsi_period=rsi_period,
        adx_period=adx_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        ema_periods_by_timeframe=ema_periods_by_timeframe,
        max_candles=max_candles,
    )
    replayed = 0
    for bar in replay_bars:
        if replay_engine.seed_from_closed_bars([bar]) <= 0:
            continue
        label = str(bar.get("label") or "")
        timeframe_seconds = _safe_int(bar.get("timeframe_seconds"))
        if not label or timeframe_seconds is None:
            continue
        indicators = replay_engine.get_latest_indicators(label, timeframe_seconds)
        if not indicators:
            continue
        candle = indicators.pop("last_candle", None)
        if candle is None:
            continue
        on_bar(label, timeframe_seconds, candle, indicators)
        replayed += 1
    return replayed


# Seed indicator engine from a prior indicator CSV (warm start).
def _seed_indicators_from_csv(
    engine: MultiInstrumentIndicatorEngine,
    csv_path: Optional[str],
    *,
    allowed_labels: Optional[set[str]] = None,
    max_bars_per_series: int = 0,
) -> int:
    bars = _load_indicator_seed_bars_from_csv(
        csv_path,
        allowed_labels=allowed_labels,
        allowed_timeframes=set(engine.timeframes),
        max_bars_per_series=max_bars_per_series,
    )
    if not bars:
        return 0
    return engine.seed_from_closed_bars(bars)


# Parse HH:MM values into time objects with a safe fallback.
def _parse_hhmm_time(value: Any, *, default: Optional[dt_time] = None) -> Optional[dt_time]:
    if value in (None, ""):
        return default
    try:
        text = str(value).strip()
        hour_s, minute_s = text.split(":", 1)
        return dt_time(hour=int(hour_s), minute=int(minute_s))
    except Exception:
        return default


# Resolve the default environment prefix for an underlying label.
def _default_env_prefix(underlying_label: str) -> str:
    mapping = {
        "NG_FUT": "NG_",
        "NIFTY_IDX": "NIFTY_",
        "BANKNIFTY_IDX": "BANKNIFTY_",
        "FINNIFTY_IDX": "FINNIFTY_",
        "SENSEX_IDX": "SENSEX_",
        "MIDCPNIFTY_IDX": "MIDCPNIFTY_",
    }
    key = str(underlying_label).upper()
    if key in mapping:
        return mapping[key]
    base = key.split("_")[0]
    return f"{base}_"


# Format a timestamp as an IST ISO string.
def _to_ist_iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    try:
        return ts.astimezone(IST).isoformat()
    except Exception:
        return str(ts)


def _to_utc_iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return str(ts)


# Check whether a timestamp is within trading hours for the instrument.
def _is_in_trading_window(meta: dict | None, ts: datetime) -> bool:
    """
    Apply simple exchange-based trading hours filters (IST):
      - MCX/NCO: 09:00–23:30
      - All others (NSE/BSE): 09:15–15:30
    
    Can be disabled via DISABLE_TRADING_WINDOW_FILTER env var for testing.
    """
    if _DISABLE_TRADING_WINDOW_FILTER:
        return True
    if meta is None:
        return True
    return get_trading_session_calendar().is_trading_open(meta, ts)


def _is_index_expected_in_current_session(label: str, ts: datetime) -> bool:
    """
    Return whether an index label is expected to be present in the live universe
    for the current session window.
    """
    normalized = str(label or "").upper()
    if normalized not in INDEX_LABELS:
        return False
    # Index health monitoring is only active during the actual open session.
    return get_trading_session_calendar().is_session_active(
        {"exchange": "NSE", "exchangeType": 1},
        ts,
        include_warmup=False,
    )


def _filter_stream_universe_to_enabled_labels(
    instruments: List[Dict[str, Any]],
    token_labels: Dict[str, str],
    token_list: List[Dict[str, Any]],
    instrument_meta: Dict[str, Dict[str, Any]],
    *,
    instrument_controller: InstrumentController,
    underlying_label_for_label: Callable[[str], str],
) -> tuple[
    List[Dict[str, Any]],
    Dict[str, str],
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    set[str],
]:
    """
    Keep all enabled instruments subscribed, even before their session opens.
    Trading-window checks are enforced later in `on_data` and health monitors.
    """
    allowed_labels = {
        label
        for label in instrument_meta
        if instrument_controller.is_enabled(underlying_label_for_label(label))
    }
    filtered_instruments = [
        ins for ins in instruments if ins.get("label") in allowed_labels
    ]
    filtered_instrument_meta = {
        label: meta for label, meta in instrument_meta.items() if label in allowed_labels
    }
    filtered_token_labels = {
        tok: lbl for tok, lbl in token_labels.items() if lbl in allowed_labels
    }
    filtered_token_list = [
        {
            "exchangeType": entry.get("exchangeType"),
            "tokens": [
                tok
                for tok in entry.get("tokens", [])
                if filtered_token_labels.get(str(tok)) in allowed_labels
            ],
        }
        for entry in token_list
    ]
    filtered_token_list = [
        entry for entry in filtered_token_list if entry.get("tokens")
    ]
    return (
        filtered_instruments,
        filtered_token_labels,
        filtered_token_list,
        filtered_instrument_meta,
        allowed_labels,
    )


def _prune_indicator_state_for_active_labels(
    engine: MultiInstrumentIndicatorEngine,
    active_labels: Iterable[str],
) -> tuple[int, int]:
    active = {str(label) for label in active_labels if label is not None}
    if active:
        return engine.prune_to_active_labels(active)
    cached_labels = len(getattr(engine, "states", {}) or {})
    if cached_labels:
        logger.info(
            "Skipping indicator prune because active label map is empty; retaining %d cached labels",
            cached_labels,
        )
    return 0, 0


def _filter_universe_by_instrument_control(
    *,
    instruments: List[Dict[str, Any]],
    token_labels: Dict[str, str],
    token_list: List[Dict[str, Any]],
    instrument_meta: Dict[str, Dict[str, Any]],
    instrument_controller: InstrumentController,
) -> tuple[
    List[Dict[str, Any]],
    Dict[str, str],
    List[Dict[str, Any]],
    Dict[str, Dict[str, Any]],
    Dict[str, str],
    set[str],
]:
    # Map underlying name -> underlying label (for indices)
    underlying_name_to_label = {
        meta.get("underlying"): label
        for label, meta in instrument_meta.items()
        if meta.get("kind") == "UNDERLYING"
    }

    def _underlying_label_for_label(lbl: str) -> str:
        meta = instrument_meta.get(lbl, {})
        if meta.get("kind") == "UNDERLYING":
            return lbl
        underlying_name = meta.get("underlying")
        return underlying_name_to_label.get(underlying_name, lbl)

    # Filter instruments/token list/meta for disabled underlyings
    allowed_labels = {
        label
        for label, meta in instrument_meta.items()
        if instrument_controller.is_enabled(_underlying_label_for_label(label))
    }
    logger.info("[STREAM:filter] Allowed labels: %d of %d", len(allowed_labels), len(instrument_meta))
    logger.debug("   Allowed: %s", sorted(allowed_labels))

    instruments = [ins for ins in instruments if ins.get("label") in allowed_labels]
    instrument_meta = {k: v for k, v in instrument_meta.items() if k in allowed_labels}
    token_labels = {tok: lbl for tok, lbl in token_labels.items() if lbl in allowed_labels}
    logger.info(
        "[STREAM:post-filter] After filtering: %d tokens, %d instruments",
        len(token_labels),
        len(instruments),
    )

    # Filter token_list structure: [{"exchangeType": ..., "tokens": [...]}, ...]
    # Only include tokens that are in the filtered token_labels
    token_list = [
        {
            "exchangeType": entry.get("exchangeType"),
            "tokens": [
                tok
                for tok in entry.get("tokens", [])
                if token_labels.get(str(tok)) in allowed_labels
            ],
        }
        for entry in token_list
        if entry.get("tokens")  # Keep only exchanges with at least one token
    ]
    # Remove empty exchanges (those with no tokens after filtering)
    token_list = [entry for entry in token_list if entry.get("tokens")]

    logger.info("[STREAM:tokens] Final token_list: %d exchanges", len(token_list))
    for entry in token_list:
        logger.info(
            "   Exchange %s: %d tokens",
            entry.get("exchangeType"),
            len(entry.get("tokens", [])),
        )

    subscribed_tokens: set[tuple[int, str]] = set()
    for entry in token_list or []:
        ex_type = entry.get("exchangeType")
        if ex_type is None:
            continue
        for tok in entry.get("tokens", []):
            subscribed_tokens.add((int(ex_type), str(tok)))

    index_summary = []
    missing_indices = []
    index_check_now_utc = datetime.now(timezone.utc)
    for label in sorted(INDEX_LABELS):
        if not _is_index_expected_in_current_session(label, index_check_now_utc):
            continue
        meta = instrument_meta.get(label)
        if not meta:
            missing_indices.append(label)
            continue
        token = str(meta.get("token"))
        ex_type = int(meta.get("exchangeType", 0) or 0)
        exch = meta.get("exchange")
        index_summary.append(f"{label}(token={token} exchType={ex_type} exch={exch})")
        if (ex_type, token) not in subscribed_tokens:
            logger.warning(
                "Index %s missing from subscription list (token=%s exchType=%s)",
                label,
                token,
                ex_type,
            )
    if index_summary:
        logger.info("Index subscription summary: %s", "; ".join(index_summary))
    if missing_indices:
        logger.warning(
            "Index instruments missing from universe: %s",
            ", ".join(missing_indices),
        )
    index_required_labels = {
        label
        for label in INDEX_LABELS
        if label in instrument_meta
        and _is_index_expected_in_current_session(label, index_check_now_utc)
    }

    label_to_underlying = {
        lbl: _underlying_label_for_label(lbl) for lbl in instrument_meta
    }
    return (
        instruments,
        token_labels,
        token_list,
        instrument_meta,
        label_to_underlying,
        index_required_labels,
    )


def _build_indicator_engine_config(
    strategy_cfg: Dict[str, Any],
) -> tuple[Dict[int, List[int]], List[int]]:
    plan = build_indicator_engine_plan(strategy_cfg)
    return plan.ema_periods_by_timeframe, plan.engine_timeframes


def _run_stream_lifecycle(
    *,
    stop_event: threading.Event,
    refresh_stop: threading.Event,
    start_health_server: bool,
    refresh_atm_loop: Callable[[], None],
    position_sync_loop: Callable[[], None],
    position_sync_interval: int,
    run_socket: Callable[[], None],
    runner: SmartWebSocketRunner,
    persister: BarPersister,
    loop_body: Optional[Callable[[], None]] = None,
    shutdown_callback: Optional[Callable[[], None]] = None,
) -> None:
    _run_stream_lifecycle_impl(
        stop_event=stop_event,
        refresh_stop=refresh_stop,
        start_health_server_enabled=start_health_server,
        refresh_atm_loop=refresh_atm_loop,
        position_sync_loop=position_sync_loop,
        position_sync_interval=position_sync_interval,
        run_socket=run_socket,
        runner=runner,
        persister=persister,
        loop_body=loop_body,
        health_server_factory=_start_health_server,
        shutdown_callback=shutdown_callback,
    )


STRATEGY_REGISTRY = {
    "delta_strangle": DeltaStrangleStrategy,
    "option_strategy": OptionStrategy,
    "ema20_strategy": Ema20Strategy,
    "ce_orb": CeOrbStrategy,
    "ce_pdh_rb": CePdhRangeBreakoutStrategy,
    "put_momentum_scalper": PutMomentumScalperStrategy,
    "put_reversion_writer": PutReversionWriterStrategy,
    "nifty_weekly_credit_spreads": NiftyWeeklyCreditSpreadStrategy,
    "exclusive_nifty_ce_buy": ExclusiveNiftyCeBuyStrategy,
    "put_buy": PutBuyLiveStrategy,
}

INDEX_LABELS = {
    "NIFTY_IDX",
    "BANKNIFTY_IDX",
    "FINNIFTY_IDX",
    "MIDCPNIFTY_IDX",
    "SENSEX_IDX",
}

# Labels that must *always* update maintenance heartbeat, even if their meta.kind
# is not "UNDERLYING" (e.g. indices) or the tick arrives via a derivative leg.
#
# Extend via env var: HEARTBEAT_EXTRA_LABELS="FOO,BAR"
CRITICAL_HEARTBEAT_LABELS = set(INDEX_LABELS) | {"NG_FUT"}

_extra = os.getenv("HEARTBEAT_EXTRA_LABELS", "").strip()
if _extra:
    for _lbl in _extra.split(","):
        _lbl = _lbl.strip().upper()
        if _lbl:
            CRITICAL_HEARTBEAT_LABELS.add(_lbl)


# Monitor index feeds and trigger resubscribe/reconnect on stale ticks.
class IndexFeedMonitor:
    # Initialize the monitor with required labels and thresholds.
    def __init__(
        self,
        *,
        runner: SmartWebSocketRunner,
        instrument_meta: Dict[str, Dict[str, Any]],
        required_labels: set[str],
        warn_seconds: float,
        resubscribe_cooldown_seconds: float = 30.0,
        reconnect_after_seconds: float = 120.0,
    ) -> None:
        self._runner = runner
        self._instrument_meta = instrument_meta
        self._required_labels = required_labels
        self._warn_seconds = float(warn_seconds)
        self._resubscribe_cooldown = float(resubscribe_cooldown_seconds)
        self._reconnect_after = float(reconnect_after_seconds)
        self._start_ts = time.monotonic()
        self._last_check_ts = 0.0
        self._stale_since: Dict[str, float] = {}
        self._last_resubscribe_ts: Dict[str, float] = {}
        self._reconnect_backoff = BackoffState(base_seconds=30.0, max_seconds=300.0)

    # Labels handled by the index monitor stale-feed logic.
    def required_labels(self) -> set[str]:
        return set(self._required_labels)

    # Build a SmartAPI token entry for a required label.
    def _token_entry_for_label(self, label: str) -> Optional[Dict[str, Any]]:
        meta = self._instrument_meta.get(label)
        if not meta:
            return None
        token = meta.get("token")
        ex_type = meta.get("exchangeType")
        if token is None or ex_type is None:
            return None
        return {"exchangeType": int(ex_type), "tokens": [str(token)]}

    # Update instrument metadata used for token lookups.
    def update_instrument_meta(self, instrument_meta: Dict[str, Dict[str, Any]]) -> None:
        self._instrument_meta = instrument_meta

    # Check for stale index feeds and trigger recovery actions.
    def check(self, now: datetime, last_ticks: Dict[str, datetime]) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_check_ts < 5.0:
            return
        self._last_check_ts = now_mono
        for label in sorted(self._required_labels):
            meta = self._instrument_meta.get(label)
            if not meta:
                self._stale_since.pop(label, None)
                self._last_resubscribe_ts.pop(label, None)
                continue
            if not _is_in_trading_window(meta, now):
                self._stale_since.pop(label, None)
                self._last_resubscribe_ts.pop(label, None)
                continue
            last_tick = last_ticks.get(label)
            if last_tick is None:
                stale_seconds = now_mono - self._start_ts
            else:
                stale_seconds = (now - last_tick).total_seconds()
            if stale_seconds <= self._warn_seconds:
                self._stale_since.pop(label, None)
                self._last_resubscribe_ts.pop(label, None)
                continue
            self._stale_since.setdefault(label, now_mono)
            last_resub = self._last_resubscribe_ts.get(label, 0.0)
            if now_mono - last_resub >= self._resubscribe_cooldown:
                entry = self._token_entry_for_label(label)
                if entry:
                    logger.warning(
                        "Index feed stale for %s: %.0fs; resubscribing token=%s exchType=%s",
                        label,
                        stale_seconds,
                        entry["tokens"][0],
                        entry["exchangeType"],
                    )
                    self._runner.subscribe_tokens([entry])
                    self._last_resubscribe_ts[label] = now_mono
            if now_mono - self._stale_since[label] >= self._reconnect_after:
                if self._reconnect_backoff.can_attempt(now_mono):
                    delay = self._reconnect_backoff.register_blocked(
                        "index_feed_stale", now=now_mono
                    )
                    logger.warning(
                        "Index feed still stale after %.0fs; forcing websocket reconnect (next in %.0fs)",
                        stale_seconds,
                        delay,
                    )
                    self._runner.request_reconnect()


# Run the legacy single-tenant multi-instrument stream loop.
def stream_multi_instruments(
    stop_event: threading.Event | None = None,
    *,
    start_health_server: bool = True,
    strategy_switch: StrategySwitchboard | None = None,
    instrument_controller: InstrumentController | None = None,
) -> None:
    """
    Run the multi-instrument stream until ``stop_event`` is set (or a KeyboardInterrupt is raised).

    ``start_health_server`` can be disabled when a different HTTP server (e.g. uvicorn/FastAPI)
    exposes health checks to avoid port clashes.
    """
    stop_event = stop_event or threading.Event()
    boot_cfg = get_boot_config()
    settings = boot_cfg.settings
    process_start_ts = datetime.now(timezone.utc)

    def _runtime_settings():
        return get_runtime_settings(base_settings=settings)
    strategy_cfg = boot_cfg.strategy_env
    vol_thresholds_cfg = (
        strategy_cfg.get("vol_thresholds", {}) if isinstance(strategy_cfg, dict) else {}
    )
    delta_qty_per_leg, delta_qty_per_leg_by_underlying = _resolve_delta_qty_config(
        strategy_cfg
    )
    dashboard_bus.set_mode(boot_cfg.runtime.runner_mode)
    instrument_controller = instrument_controller or InstrumentController()
    instrument_cfg = (
        strategy_cfg.get("instruments", {}) if isinstance(strategy_cfg, dict) else {}
    )
    for key, cfg in instrument_cfg.items():
        if not isinstance(cfg, dict):
            continue
        instrument_controller.set_enabled(key, cfg.get("enabled", True))
        instrument_controller.update_allowed_strategies(
            key, cfg.get("allowed_strategies", [])
        )
    _log_instrument_policy_snapshot(instrument_controller)

    # TODO: legacy single-tenant path; replace with broker adapter + hub routing for multi-tenant mode.
    tokens = angel_login_and_get_tokens()
    jwt_token = tokens["jwtToken"]
    feed_token = tokens["feedToken"]
    client_code = tokens["clientCode"]
    api_key = tokens["API_KEY"]

    auth_header_value = f"Bearer {jwt_token}"

    instruments, token_labels, token_list, instrument_meta = build_instrument_universe(
        jwt_token=jwt_token,
        api_key=api_key,
    )
    logger.info("[STREAM] Universe built: %d instruments, %d exchanges", len(instruments), len(token_list))
    for entry in token_list:
        logger.info(f"   Exchange {entry.get('exchangeType')}: {len(entry.get('tokens', []))} tokens")
    
    _daily_levels_state["instruments"] = instruments
    # Map underlying name -> underlying label (for indices)
    underlying_name_to_label = {
        meta.get("underlying"): label
        for label, meta in instrument_meta.items()
        if meta.get("kind") == "UNDERLYING"
    }

    # Resolve an underlying label for a given instrument label.
    def _underlying_label_for_label(lbl: str) -> str:
        meta = instrument_meta.get(lbl, {})
        if meta.get("kind") == "UNDERLYING":
            return lbl
        underlying_name = meta.get("underlying")
        return underlying_name_to_label.get(underlying_name, lbl)

    total_universe_labels = len(instrument_meta)
    # Filter instruments/token list/meta for disabled underlyings.
    # Keep off-session labels subscribed so seeded indicators stay warm pre-open.
    (
        instruments,
        token_labels,
        token_list,
        instrument_meta,
        allowed_labels,
    ) = _filter_stream_universe_to_enabled_labels(
        instruments,
        token_labels,
        token_list,
        instrument_meta,
        instrument_controller=instrument_controller,
        underlying_label_for_label=_underlying_label_for_label,
    )
    logger.info("[STREAM:filter] Allowed labels: %d of %d", len(allowed_labels), total_universe_labels)
    # logger.debug(f"   Allowed: {sorted(allowed_labels)}")
    
    logger.info("[STREAM:post-filter] After filtering: %d tokens, %d instruments", len(token_labels), len(instruments))
    
    
    logger.info("[STREAM:tokens] Final token_list: %d exchanges", len(token_list))
    for entry in token_list:
        logger.info(f"   Exchange {entry.get('exchangeType')}: {len(entry.get('tokens', []))} tokens")
    
    dashboard_bus.set_instrument_meta(instrument_meta)
    instrument_state = {
        "instruments": instruments,
        "token_labels": token_labels,
        "token_list": token_list,
        "instrument_meta": instrument_meta,
        "label_to_underlying": {
            lbl: _underlying_label_for_label(lbl) for lbl in instrument_meta
        },
    }
    token_labels_ref = {"map": token_labels}
    token_label_self_heal_enabled = _is_true_env(
        "STREAM_SELF_HEAL_TOKEN_LABELS", default=False
    )
    sync_executed_token_labels = _is_true_env(
        "STREAM_SYNC_EXECUTED_TOKEN_LABELS",
        default=token_label_self_heal_enabled,
    )
    drop_unmapped_ticks = _is_true_env(
        "STREAM_DROP_UNMAPPED_TICKS",
        default=token_label_self_heal_enabled,
    )
    try:
        unmapped_tick_log_interval_seconds = max(
            1.0,
            float(os.getenv("STREAM_UNMAPPED_TICK_LOG_INTERVAL_SECONDS", "60")),
        )
    except (TypeError, ValueError):
        unmapped_tick_log_interval_seconds = 60.0
    unmapped_tick_last_log_mono: Dict[str, float] = {}
    logger.info(
        "Token label guards: self_heal=%s sync_executed=%s drop_unmapped=%s log_interval=%.0fs",
        token_label_self_heal_enabled,
        sync_executed_token_labels,
        drop_unmapped_ticks,
        unmapped_tick_log_interval_seconds,
    )

    def _merge_executed_token_labels(
        base_token_labels: Dict[str, str],
        base_token_list: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        if not sync_executed_token_labels:
            return dict(base_token_labels)
        tracked = executed_tokens_tracker.get_token_label_map()
        if not tracked:
            return dict(base_token_labels)

        subscribed_tokens: set[str] = set()
        for entry in base_token_list or []:
            for tok in entry.get("tokens", []) or []:
                subscribed_tokens.add(str(tok))

        merged = dict(base_token_labels)
        added = 0
        for tok, lbl in tracked.items():
            token = str(tok)
            if token not in subscribed_tokens:
                continue
            label = str(lbl)
            if merged.get(token) == label:
                continue
            merged[token] = label
            added += 1
        if added:
            logger.info(
                "Token label sync: merged %d executed token mapping(s) into active label map",
                added,
            )
        return merged

    def _resolve_label_from_runtime_state(token: str, exch_type: Any) -> Optional[str]:
        exchange_type = _safe_int(exch_type)

        for candidate_label, meta in list(instrument_state["instrument_meta"].items()):
            if str(meta.get("token") or "") != token:
                continue
            meta_exch_type = _safe_int(meta.get("exchangeType"))
            if (
                exchange_type is None
                or meta_exch_type is None
                or meta_exch_type == exchange_type
            ):
                return candidate_label

        tracked = executed_tokens_tracker.get_token_label_map()
        tracked_label = tracked.get(token)
        if tracked_label:
            return str(tracked_label)
        return None

    subscribed_tokens: set[tuple[int, str]] = set()
    for entry in token_list or []:
        ex_type = entry.get("exchangeType")
        if ex_type is None:
            continue
        for tok in entry.get("tokens", []):
            subscribed_tokens.add((int(ex_type), str(tok)))

    index_summary = []
    missing_indices = []
    index_check_now_utc = datetime.now(timezone.utc)
    for label in sorted(INDEX_LABELS):
        if not _is_index_expected_in_current_session(label, index_check_now_utc):
            continue
        if not instrument_controller.is_enabled(label):
            continue
        meta = instrument_meta.get(label)
        if not meta:
            missing_indices.append(label)
            continue
        token = str(meta.get("token"))
        ex_type = int(meta.get("exchangeType", 0) or 0)
        exch = meta.get("exchange")
        index_summary.append(
            f"{label}(token={token} exchType={ex_type} exch={exch})"
        )
        if (ex_type, token) not in subscribed_tokens:
            logger.warning(
                "Index %s missing from subscription list (token=%s exchType=%s)",
                label,
                token,
                ex_type,
            )
    if index_summary:
        logger.info("Index subscription summary: %s", "; ".join(index_summary))
    if missing_indices:
        logger.warning(
            "Index instruments missing from universe: %s",
            ", ".join(missing_indices),
        )
    index_required_labels = {
        label
        for label in INDEX_LABELS
        if label in instrument_meta
        and instrument_controller.is_enabled(label)
        and _is_index_expected_in_current_session(label, index_check_now_utc)
    }

    indicator_plan = build_indicator_engine_plan(strategy_cfg)
    ema_periods_by_tf = indicator_plan.ema_periods_by_timeframe
    engine_timeframes = indicator_plan.engine_timeframes
    engine_adx_period = indicator_plan.adx_period

    def _is_date_folder(value: str) -> bool:
        parts = (value or "").split("-")
        return (
            len(parts) == 3
            and all(p.isdigit() for p in parts)
            and len(parts[0]) == 4
            and len(parts[1]) == 2
            and len(parts[2]) == 2
        )

    def _daily_logs_dir() -> Path:
        log_root = Path(__file__).resolve().parents[2] / "logs"
        tz_name = settings.default_time_zone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        day = datetime.now(tz).date().isoformat()
        date_dir = log_root / day
        date_dir.mkdir(parents=True, exist_ok=True)
        return date_dir

    def _coerce_daily_path(path_str: Optional[str]) -> Optional[str]:
        if not path_str:
            return None
        p = Path(path_str)
        log_root = Path(__file__).resolve().parents[2] / "logs"
        date_dir = _daily_logs_dir()
        if p.is_absolute():
            try:
                rel = p.relative_to(log_root)
                if rel.parts and _is_date_folder(rel.parts[0]):
                    return str(p)
                return str(date_dir / p.name)
            except Exception:
                return str(p)
        parts = p.parts
        if not parts or parts[0] == ".":
            return str(date_dir / p.name)
        if parts[0].lower() == "logs":
            if len(parts) > 1 and _is_date_folder(parts[1]):
                return str(p)
            return str(date_dir / p.name)
        return str(p)

    indicator_csv_enabled = _is_true_env("INDICATOR_CSV_ENABLED", default=True)
    raw_indicator_csv_path = os.getenv("INDICATOR_CSV_PATH", "indicator_bars.csv")
    csv_path = raw_indicator_csv_path if indicator_csv_enabled and raw_indicator_csv_path else None
    sqlite_path = os.getenv("INDICATOR_SQLITE_PATH")
    indicator_postgres_enabled = _is_true_env(
        "INDICATOR_POSTGRES_ENABLED", default=False
    )
    indicator_postgres_dsn = os.getenv("INDICATOR_POSTGRES_DSN")
    indicator_postgres_table = os.getenv("INDICATOR_POSTGRES_TABLE", "indicator_bars")
    if indicator_postgres_enabled and not indicator_postgres_dsn:
        try:
            from app.data.postgres import get_control_plane_dsn

            indicator_postgres_dsn = get_control_plane_dsn(settings)
        except Exception as exc:
            logger.warning(
                "INDICATOR_POSTGRES_ENABLED=true but no INDICATOR_POSTGRES_DSN/control-plane DSN available: %s",
                exc,
            )
            indicator_postgres_dsn = None
    if not indicator_postgres_enabled:
        indicator_postgres_dsn = None
    bq_project = os.getenv("INDICATOR_BQ_PROJECT_ID", settings.gcp_project_id or "")
    bq_dataset = os.getenv("INDICATOR_BQ_DATASET", settings.bq_dataset_id or "")
    bq_table = os.getenv("INDICATOR_BQ_TABLE", settings.indicator_bq_table)
    trade_csv = _coerce_daily_path(os.getenv("TRADE_CSV_PATH", "trades.csv"))
    trade_sqlite = _coerce_daily_path(os.getenv("TRADE_SQLITE_PATH"))
    trade_bq_project = os.getenv("TRADE_BQ_PROJECT_ID", settings.gcp_project_id or "")
    trade_bq_dataset = os.getenv("TRADE_BQ_DATASET", settings.bq_dataset_id or "")
    trade_bq_table = os.getenv("TRADE_BQ_TABLE", settings.bigquery_trades_table)

    persister = BarPersister(
        csv_path=csv_path,
        sqlite_path=sqlite_path,
        bq_project=bq_project,
        bq_dataset=bq_dataset,
        bq_table=bq_table,
        postgres_dsn=indicator_postgres_dsn,
        postgres_table=indicator_postgres_table,
    )
    trade_persister = TradePersister(
        csv_path=trade_csv,
        sqlite_path=trade_sqlite,
        bq_project=trade_bq_project,
        bq_dataset=trade_bq_dataset,
        bq_table=trade_bq_table,
    )

    # Tick-by-tick market data is useful for deep diagnostics, but it drowns
    # operational warnings in the live Docker profile. Keep it opt-in.
    log_ticks = os.getenv("LOG_TICKS", "0").lower() not in {"0", "false", "no"}
    try:
        tick_log_every = (
            max(1, int(os.getenv("TICK_LOG_EVERY", "1"))) if log_ticks else 0
        )
    except ValueError:
        tick_log_every = 1 if log_ticks else 0
    tick_counter = {"count": 0}  # mutable for closure

    trade_mode = os.getenv("TRADE_MODE", "PAPER").upper()
    if trade_mode == "SHADOW":
        logger.info(
            "TRADE_MODE=SHADOW active: execution routed to virtual shadow adapter where configured."
        )
    dashboard_bus.set_trade_mode(trade_mode)

    # Resolve operating mode for authority-path enforcement in exit paths.
    _stream_operating_mode = resolve_operating_mode_from_runtime(
        settings=settings,
        runtime_cfg=boot_cfg.runtime,
    )
    _stream_operating_mode_str = (
        _stream_operating_mode.mode.value
        if _stream_operating_mode.mode is not None
        else "UNKNOWN"
    )
    runtime_tenant_id = (
        str(getattr(settings, "hub_default_tenant_id", "") or "").strip() or None
    )
    position_sync_broker_account_id = (
        str(getattr(settings, "hub_default_broker_account_id", "") or "").strip()
        or None
    )
    order_client = AngelOrderClient(
        jwt_token=jwt_token,
        api_key=api_key,
        broker_account_id=position_sync_broker_account_id,
        client_local_ip=tokens.get("client_local_ip"),
        client_public_ip=tokens.get("client_public_ip"),
        mac_address=tokens.get("mac_address"),
    )
    daily_levels_cache = DailyLevelsCache(order_client)
    _daily_levels_state["cache"] = daily_levels_cache
    try:
        daily_levels_cache.refresh_for_instruments(instruments)
    except Exception as exc:
        logger.warning("Daily levels refresh failed: %s", exc)
    feed_warn_seconds = int(
        os.getenv(
            "FEED_STALE_WARN_SECONDS",
            "120",  # Reduced from 300 (5 min) to 120 (2 min) for faster detection
        )
    )
    # Reconnect after 180s (3 min) instead of 900s (15 min)
    feed_reconnect_seconds = int(
        os.getenv("FEED_STALE_RECONNECT_SECONDS", "180")
    )
    feed_log_throttle_seconds = int(
        os.getenv("FEED_STALE_LOG_THROTTLE_SECONDS", "60")
    )
    maintenance = MaintenanceManager(
        kill_switch_env=os.getenv("MAINTENANCE_KILL_SWITCH_ENV", "GLOBAL_KILL"),
        heartbeat_timeout_seconds=feed_warn_seconds,
        feed_stale_log_throttle_seconds=feed_log_throttle_seconds,
        eod_square_off_time=os.getenv("EOD_SQUARE_OFF_TIME", "15:20"),
        eod_square_off_start_time=os.getenv("EOD_SQUARE_OFF_START_TIME", "15:15"),
        eod_square_off_end_time=os.getenv("EOD_SQUARE_OFF_END_TIME"),
    )
    risk_cfg = (
        (strategy_cfg or {}).get("risk", {}) if isinstance(strategy_cfg, dict) else {}
    )
    strategies_cfg_raw = (
        strategy_cfg.get("strategies", []) if isinstance(strategy_cfg, dict) else []
    )
    normalized_strategy_entries = _normalize_strategy_entries(strategies_cfg_raw)
    validate_startup_config(
        strategy_cfg=(strategy_cfg if isinstance(strategy_cfg, dict) else {}),
        trade_mode=trade_mode,
        disable_trading_window_filter=_DISABLE_TRADING_WINDOW_FILTER,
        known_strategy_names=STRATEGY_REGISTRY.keys(),
        env=boot_cfg.env,
    )
    initial_flags: Dict[str, bool] = {}
    for entry in normalized_strategy_entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        canonical = canonicalize_strategy_name(
            str(entry.get("name") or "").strip(),
            source="stream.initial_flags",
        )
        if not canonical:
            continue
        initial_flags[canonical] = bool(entry.get("enabled", True))
    strategy_switch = strategy_switch or StrategySwitchboard(initial_flags)
    for name, enabled in initial_flags.items():
        strategy_switch.set_enabled(name, enabled)
    max_spreads_env = int(
        os.getenv(
            "RISK_MAX_OPEN_SPREADS_PER_UNDERLYING",
            str(risk_cfg.get("max_open_spreads_per_underlying", "1")),
        )
    )
    day_loss_limit = float(os.getenv("RISK_DAY_LOSS_LIMIT", "0"))
    max_daily_loss = float(
        os.getenv("RISK_MAX_DAILY_LOSS", str(risk_cfg.get("max_daily_loss", "0")))
    )
    max_intraday_drawdown = float(
        os.getenv(
            "RISK_MAX_INTRADAY_DRAWDOWN",
            str(risk_cfg.get("max_intraday_drawdown", "0")),
        )
    )
    kill_switch_square_off = os.getenv(
        "RISK_KILL_SWITCH_SQUARE_OFF_OPEN_POSITIONS",
        str(risk_cfg.get("kill_switch_square_off_open_positions", "true")),
    ).lower() not in {"0", "false", "no"}
    risk_limits_cfg = (
        risk_cfg.get("risk_limits", {}) if isinstance(risk_cfg, dict) else {}
    )
    risk_manager = RiskManager(
        instrument_meta=instrument_meta,
        order_client=order_client,
        max_open_spreads_per_underlying=max_spreads_env,
        per_underlying_max_open_spreads={
            str(k).upper(): int(v)
            for k, v in ((risk_cfg.get("per_underlying", {}) or {}).items())
            if v is not None
        },
        day_loss_limit=day_loss_limit,
        max_daily_loss=max_daily_loss,
        max_intraday_drawdown=max_intraday_drawdown,
        kill_switch_square_off_open_positions=kill_switch_square_off,
        risk_limits=risk_limits_cfg,
        state_path=os.getenv("RISK_STATE_PATH"),
        trade_persister=trade_persister,
        tenant_id=runtime_tenant_id,
        broker_account_id=position_sync_broker_account_id,
    )
    risk_manager.current_trade_mode = trade_mode.upper()
    risk_manager.set_operating_mode(_stream_operating_mode_str)
    risk_manager.attach_maintenance(maintenance)
    dashboard_bus.bootstrap_positions(risk_manager.open_positions)
    dashboard_bus.set_realized_pnl(risk_manager.realized_pnl)
    engine = MultiInstrumentIndicatorEngine(
        timeframes=engine_timeframes,
        atr_period=14,
        rsi_period=14,
        adx_period=engine_adx_period,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        ema_periods_by_timeframe=ema_periods_by_tf,
        max_candles=1000,
        daily_levels_cache=daily_levels_cache,
    )
    gap_backfill_enabled = _is_true_env(
        "INDICATOR_GAP_BACKFILL_ENABLED",
        default=True,
    )
    gap_backfill_broker_enabled = _is_true_env(
        "INDICATOR_GAP_BACKFILL_BROKER_ENABLED",
        default=True,
    )
    if gap_backfill_enabled:
        def _gap_backfill(request):
            return _resolve_indicator_gap_backfill(
                request,
                instrument_meta=instrument_state["instrument_meta"],
                label_to_underlying=instrument_state["label_to_underlying"],
                maintenance=maintenance,
                postgres_dsn=indicator_postgres_dsn,
                postgres_table=indicator_postgres_table,
                order_client=order_client,
                broker_backfill_enabled=gap_backfill_broker_enabled,
            )

        engine.on_gap_backfill = _gap_backfill
    logger.info(
        "Engine footprint: instruments=%d timeframes=%s max_candles_per_tf=%d adx_period=%d",
        len(instrument_meta),
        engine.timeframes,
        engine.max_candles,
        engine.adx_period,
    )
    try:
        seed_bars_env = int(os.getenv("INDICATOR_SEED_BARS", "0"))
    except ValueError:
        seed_bars_env = 0
    seeded_history_bars: List[Dict[str, Any]] = []
    seeded_history_source = "none"
    seeded_history_count = 0
    seed_bars = 0
    if seed_bars_env > 0:
        seed_bars = min(seed_bars_env, engine.max_candles)
        allowed_seed_labels = set(instrument_meta.keys())
        seed_csv_path = os.getenv("INDICATOR_SEED_CSV_PATH") or csv_path
        seeded = 0

        # Postgres is the primary seed source when INDICATOR_POSTGRES_ENABLED=true.
        # CSV is the fallback — used only when Postgres is unavailable or returns 0 bars.
        if indicator_postgres_dsn:
            seeded_history_bars = _load_indicator_seed_bars_from_postgres(
                indicator_postgres_dsn,
                indicator_postgres_table,
                allowed_timeframes=set(engine.timeframes),
                allowed_labels=allowed_seed_labels,
                max_bars_per_series=seed_bars,
            )
            seeded = (
                engine.seed_from_closed_bars(seeded_history_bars)
                if seeded_history_bars
                else 0
            )
            if seeded:
                seeded_history_source = "postgres"
                seeded_history_count = int(seeded)
                logger.info(
                    "startup.indicator_seed_complete: source=postgres bars=%d table=%s max_per_series=%d",
                    seeded,
                    indicator_postgres_table,
                    seed_bars,
                )
            else:
                logger.info(
                    "startup.indicator_seed: postgres table %s returned 0 bars; falling back to CSV",
                    indicator_postgres_table,
                )

        if seeded == 0 and seed_csv_path:
            seeded_history_bars = _load_indicator_seed_bars_from_csv(
                seed_csv_path,
                allowed_labels=allowed_seed_labels,
                allowed_timeframes=set(engine.timeframes),
                max_bars_per_series=seed_bars,
            )
            seeded = (
                engine.seed_from_closed_bars(seeded_history_bars)
                if seeded_history_bars
                else 0
            )
            if seeded:
                seeded_history_source = "csv"
                seeded_history_count = int(seeded)
                logger.info(
                    "startup.indicator_seed_complete: source=csv bars=%d path=%s max_per_series=%d",
                    seeded,
                    seed_csv_path,
                    seed_bars,
                )
            else:
                logger.info(
                    "startup.indicator_seed: no bars seeded from CSV %s", seed_csv_path
                )

        if seeded == 0 and not seed_csv_path and not indicator_postgres_dsn:
            logger.info(
                "startup.indicator_seed: skipped — no INDICATOR_SEED_CSV_PATH/INDICATOR_CSV_PATH "
                "or INDICATOR_POSTGRES_DSN configured"
            )
    # Tell the tracker about any restored positions,
    # then extend the token_list so their tokens are always streamed.
    executed_tokens_tracker.bootstrap_from_positions(
        risk_manager.open_positions,
        instrument_meta,
    )
    token_list = executed_tokens_tracker.extend_token_list(token_list)
    instrument_state["token_list"] = token_list
    if sync_executed_token_labels:
        merged_startup_labels = _merge_executed_token_labels(
            token_labels_ref["map"],
            token_list,
        )
        token_labels_ref["map"] = merged_startup_labels
        instrument_state["token_labels"] = dict(merged_startup_labels)
    if trade_mode == "LIVE" and os.getenv("SKIP_RMS_CHECK", "0").lower() not in {
        "1",
        "true",
    }:
        try:
            rms = order_client.get_rms()
            logger.info("RMS summary: %s", rms.get("data", {}))
        except Exception as exc:
            logger.warning("RMS check failed: %s", exc)

    strategies: List[Dict[str, Any]] = []
    index_monitor: Optional[IndexFeedMonitor] = None

    def _instantiate_strategy(name: str, underlying_label: str, params: Dict[str, Any]):
        canonical_name = canonicalize_strategy_name(
            name,
            source="stream.instantiate",
        )
        if canonical_name not in STRATEGY_REGISTRY:
            logger.warning("Unknown strategy name: %s", name)
            return None
        env_prefix = str(params.get("env_prefix") or _default_env_prefix(underlying_label))
        underlying_param = str(params.get("underlying_label") or underlying_label)
        ce_prefix = params.get("ce_label_prefix") or f"{env_prefix}ATM_CE"
        pe_prefix = params.get("pe_label_prefix") or f"{env_prefix}ATM_PE"
        product_type = params.get("product_type", "INTRADAY")
        resolver = boot_cfg.strategy_resolver(env_prefix=env_prefix, params=params)
        try:
            if canonical_name == "option_strategy":
                return OptionStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    ce_label_prefix=ce_prefix,
                    pe_label_prefix=pe_prefix,
                    vol_thresholds=vol_thresholds_cfg,
                    config_resolver=resolver,
                )
            if canonical_name == "delta_strangle":
                delta_qty_override = params.get("delta_qty_per_leg")
                per_underlying_override = params.get(
                    "delta_qty_per_leg_by_underlying", {}
                )
                per_underlying = dict(delta_qty_per_leg_by_underlying)
                for key, val in (per_underlying_override or {}).items():
                    try:
                        parsed = int(val)
                        if parsed > 0:
                            per_underlying[str(key).upper()] = parsed
                    except (TypeError, ValueError):
                        logger.warning("Invalid delta qty override %s=%s", key, val)
                return DeltaStrangleStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    ce_label_prefix=ce_prefix,
                    pe_label_prefix=pe_prefix,
                    delta_qty_per_leg=int(
                        delta_qty_override if delta_qty_override else delta_qty_per_leg
                    ),
                    delta_qty_per_leg_by_underlying=per_underlying,
                    sl_pct=float(params.get("sl_pct", 0.30)),
                    tp_pct=float(params.get("tp_pct", 0.30)),
                    config_resolver=resolver,
                )
            if canonical_name == "ema20_strategy":
                ema_cfg = build_ema20_config(
                    underlying_label=underlying_param,
                    raw=(strategy_cfg if isinstance(strategy_cfg, dict) else {}),
                    params=params,
                    strict=False,
                )
                ce_override = params.get("ce_label_prefix") or ce_prefix
                return Ema20Strategy(
                    instrument_meta=instrument_meta,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    product_type=product_type,
                    signal_timeframe=ema_cfg.signal_timeframe,
                    ema_period=ema_cfg.ema_period,
                    min_atr=ema_cfg.min_atr,
                    require_rsi_falling=ema_cfg.require_rsi_falling,
                    use_adx_filter=ema_cfg.use_adx_filter,
                    adx_period=ema_cfg.adx_period,
                    min_adx=ema_cfg.min_adx,
                    require_bearish_di=ema_cfg.require_bearish_di,
                    min_di_spread=ema_cfg.min_di_spread,
                    ce_label_prefix=ce_override,
                    sl_pct=ema_cfg.sl_pct,
                    tp_pct=ema_cfg.tp_pct,
                    trail_buffer_pct=ema_cfg.trail_buffer_pct,
                    trail_trigger_pct=ema_cfg.trail_trigger_pct,
                    first_entry_time=ema_cfg.first_entry_time_hhmm(),
                    square_off_time=ema_cfg.square_off_time_hhmm(),
                    dynamic_policy=ema_cfg.dynamic_policy.to_public_dict(),
                    # PHX#182/#183/#184: profit-booking enhancements.
                    tp1_pct=ema_cfg.tp1_pct,
                    tp1_qty_pct=ema_cfg.tp1_qty_pct,
                    giveback_pct=ema_cfg.giveback_pct,
                    giveback_arm_pct=ema_cfg.giveback_arm_pct,
                    decay_tighten_minutes_before_eod=ema_cfg.decay_tighten_minutes_before_eod,
                    decay_tp_multiplier=ema_cfg.decay_tp_multiplier,
                    decay_trail_buffer_multiplier=ema_cfg.decay_trail_buffer_multiplier,
                    config_resolver=resolver,
                    risk_manager=risk_manager,
                )
            if canonical_name == "ce_orb":
                return CeOrbStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "ce_pdh_rb":
                return CePdhRangeBreakoutStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "exclusive_nifty_ce_buy":
                return ExclusiveNiftyCeBuyStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "put_momentum_scalper":
                return PutMomentumScalperStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "put_reversion_writer":
                return PutReversionWriterStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "put_buy":
                return PutBuyLiveStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
            if canonical_name == "nifty_weekly_credit_spreads":
                return NiftyWeeklyCreditSpreadStrategy(
                    instrument_meta=instrument_meta,
                    order_client=order_client,
                    risk_manager=risk_manager,
                    env_prefix=env_prefix,
                    underlying_label=underlying_param,
                    params=params,
                    config_resolver=resolver,
                )
        except Exception as exc:
            logger.exception(
                "Failed to init strategy %s for %s: %s", name, underlying_label, exc
            )
            return None
        return None

    if not normalized_strategy_entries:
        normalized_strategy_entries = [
            {"name": "option_strategy", "instruments": ["NG_FUT"], "enabled": True},
            {
                "name": "delta_strangle",
                "instruments": ["NIFTY_IDX", "BANKNIFTY_IDX"],
                "enabled": True,
            },
            {
                "name": "option_strategy",
                "instruments": [
                    "NIFTY_IDX",
                    "BANKNIFTY_IDX",
                    "FINNIFTY_IDX",
                    "SENSEX_IDX",
                    "MIDCPNIFTY_IDX",
                ],
                "enabled": True,
            },
        ]

    for entry in normalized_strategy_entries:
        raw_name = str(entry.get("name") or "").strip()
        name = canonicalize_strategy_name(
            raw_name,
            source="stream.strategy_entry",
        )
        if not name:
            continue
        if not strategy_switch.is_enabled(name):
            continue
        base_params = entry.get("params", {}) or {}
        for inst_entry in _iter_instruments(entry.get("instruments", [])):
            inst_label = str(inst_entry.get("label"))
            if not instrument_controller.is_enabled(inst_label):
                logger.info(
                    "Skipping strategy %s for %s: instrument disabled by policy",
                    name,
                    inst_label,
                )
                continue
            if not instrument_controller.is_strategy_allowed(inst_label, name):
                logger.info(
                    "Skipping strategy %s for %s: not in allowed_strategies policy",
                    name,
                    inst_label,
                )
                continue
            params = dict(base_params)
            inst_specific = {k: v for k, v in inst_entry.items() if k != "label"}
            params.update(inst_specific)
            instance = _instantiate_strategy(name, inst_label, params)
            if instance is None:
                continue
            strategies.append(
                {
                    "name": name,
                    "instance": instance,
                    "underlying": inst_label,
                }
            )
            logger.info(
                "Attached strategy %s to %s (env_prefix=%s)",
                name,
                inst_label,
                params.get("env_prefix") or _default_env_prefix(inst_label),
            )

    if _runtime_settings().use_hub_router:
        # §78: In LIVE mode, unroutable (enabled but not routed) strategies must
        # abort stream startup rather than silently discard signals.
        # HUB_ROUTE_VALIDATION_FAIL_FAST can override this in either direction.
        _live_fail_fast_default = (trade_mode == "LIVE")
        fail_fast_on_missing_routes = _is_true_env(
            "HUB_ROUTE_VALIDATION_FAIL_FAST", default=_live_fail_fast_default
        )
        try:
            from app.hub.runtime import get_hub_runtime

            runtime = get_hub_runtime()
            strategies = _filter_strategies_with_missing_routes(
                strategies,
                routing_table=runtime.routing_table,
                fail_fast=fail_fast_on_missing_routes,
            )
        except Exception:
            if fail_fast_on_missing_routes:
                raise
            logger.exception(
                "Hub route validation failed unexpectedly; continuing without startup route pruning."
            )

    strict_intraday_default_targets = [
        "put_momentum_scalper",
        "put_reversion_writer",
        "put_buy",
        "nifty_weekly_credit_spreads",
        "option_strategy",
        "delta_strangle",
    ]
    strict_intraday_enabled = _is_true_env(
        "INTRADAY_STRICT_ENFORCEMENT_ENABLED", default=False
    )
    strict_target_strategies_raw = os.getenv(
        "INTRADAY_STRICT_TARGET_STRATEGIES",
        ",".join(strict_intraday_default_targets),
    )
    strict_target_names = set(
        validate_strategy_list(
            [
                p.strip()
                for p in str(strict_target_strategies_raw or "").split(",")
                if p.strip()
            ],
            context="INTRADAY_STRICT_TARGET_STRATEGIES",
        )
    )
    strict_require_hub_routing = _is_true_env(
        "INTRADAY_REQUIRE_HUB_ROUTING_FOR_TARGET_STRATEGIES",
        default=False,
    )
    strict_eod_retry_enabled = _is_true_env(
        "INTRADAY_EOD_RETRY_ENABLED",
        default=False,
    )
    strict_eod_telemetry_enabled = _is_true_env(
        "INTRADAY_EOD_TELEMETRY_ENABLED",
        default=True,
    )
    strict_eod_enforcement_time = (
        maintenance.eod_end_time
        or _parse_hhmm_time(os.getenv("EOD_SQUARE_OFF_TIME", "15:20"), default=dt_time(15, 20))
        or dt_time(15, 20)
    )
    strict_eod_retry_cutoff_time = _parse_hhmm_time(
        os.getenv("INTRADAY_EOD_RETRY_CUTOFF_TIME", "15:30"),
        default=dt_time(15, 30),
    )
    try:
        strict_eod_retry_interval_seconds = max(
            1.0,
            float(os.getenv("INTRADAY_EOD_RETRY_INTERVAL_SECONDS", "5")),
        )
    except (TypeError, ValueError):
        strict_eod_retry_interval_seconds = 5.0

    if strict_intraday_enabled:
        logger.info(
            "Strict intraday enforcement enabled | targets=%s enforce_time=%s retry=%s retry_cutoff=%s require_hub_routing=%s",
            sorted(strict_target_names),
            strict_eod_enforcement_time.strftime("%H:%M"),
            strict_eod_retry_enabled,
            strict_eod_retry_cutoff_time.strftime("%H:%M")
            if strict_eod_retry_cutoff_time
            else "-",
            strict_require_hub_routing,
        )
    if strict_intraday_enabled and strict_require_hub_routing and strict_target_names:
        if not _runtime_settings().use_hub_router:
            dropped: list[str] = []
            filtered_strategies: List[Dict[str, Any]] = []
            for row in strategies:
                canonical_name = canonicalize_strategy_name(
                    str(row.get("name") or ""),
                    source="stream.intraday_strict_drop",
                    warn_alias=False,
                )
                if canonical_name in strict_target_names:
                    dropped.append(
                        f"{canonical_name}@{row.get('underlying')}"
                    )
                    continue
                filtered_strategies.append(row)
            if dropped:
                logger.error(
                    "Strict intraday fail-closed: dropped target strategy attachments because use_hub_router=false: %s",
                    ", ".join(dropped),
                )
            strategies = filtered_strategies
        else:
            try:
                from app.hub.runtime import get_hub_runtime

                runtime = get_hub_runtime()
                route_presence: Dict[str, bool] = {}
                dropped: list[str] = []
                filtered_strategies: List[Dict[str, Any]] = []
                for row in strategies:
                    canonical_name = canonicalize_strategy_name(
                        str(row.get("name") or ""),
                        source="stream.intraday_strict_route_check",
                        warn_alias=False,
                    )
                    if canonical_name not in strict_target_names:
                        filtered_strategies.append(row)
                        continue
                    strategy_id_raw = _strategy_id_for_instance(row.get("instance"))
                    if not strategy_id_raw:
                        dropped.append(
                            f"{canonical_name}@{row.get('underlying')}#missing_strategy_id"
                        )
                        continue
                    has_routes = route_presence.get(strategy_id_raw)
                    if has_routes is None:
                        routes = runtime.routing_table.get_routes_for_strategy(
                            StrategyId(strategy_id_raw)
                        )
                        has_routes = bool(routes)
                        route_presence[strategy_id_raw] = has_routes
                    if has_routes:
                        filtered_strategies.append(row)
                    else:
                        dropped.append(
                            f"{canonical_name}@{row.get('underlying')}#{strategy_id_raw}"
                        )
                if dropped:
                    logger.error(
                        "Strict intraday fail-closed: dropped target strategy attachments with missing hub routes: %s",
                        ", ".join(dropped),
                    )
                strategies = filtered_strategies
            except Exception:
                dropped: list[str] = []
                filtered_strategies = []
                for row in strategies:
                    canonical_name = canonicalize_strategy_name(
                        str(row.get("name") or ""),
                        source="stream.intraday_strict_route_check_error",
                        warn_alias=False,
                    )
                    if canonical_name in strict_target_names:
                        dropped.append(f"{canonical_name}@{row.get('underlying')}")
                        continue
                    filtered_strategies.append(row)
                if dropped:
                    logger.exception(
                        "Strict intraday fail-closed: route validation error; dropped target strategy attachments: %s",
                        ", ".join(dropped),
                    )
                strategies = filtered_strategies

    runtime_settings = _runtime_settings()
    selection_cfg_raw = (
        strategy_cfg.get("strategy_selection", {})
        if isinstance(strategy_cfg, dict)
        else {}
    )
    max_active_override_raw = getattr(
        runtime_settings,
        "auto_strategy_max_active_per_underlying",
        1,
    )
    min_hold_override_raw = getattr(
        runtime_settings,
        "auto_strategy_min_hold_seconds",
        300.0,
    )
    try:
        max_active_override = max(1, int(max_active_override_raw))
    except (TypeError, ValueError):
        max_active_override = 1
    try:
        min_hold_override = max(0.0, float(min_hold_override_raw))
    except (TypeError, ValueError):
        min_hold_override = 300.0
    selector_config = StrategySelectorConfig.from_raw(
        selection_cfg_raw,
        enabled_override=bool(
            getattr(runtime_settings, "auto_strategy_select_enabled", False)
        ),
        max_active_override=max_active_override,
        min_hold_seconds_override=min_hold_override,
    )
    strategy_selector: Optional[StrategySelector] = None
    selector_context_builders: Dict[str, MarketContextBuilder] = {}
    selector_eval_timeframes: Dict[str, set[int]] = {}
    selector_regime_classifiers: Dict[str, RegimeClassifier] = {}

    def _selector_strategy_timeframes(instance: Any) -> set[int]:
        out: set[int] = set()
        for attr_name in (
            "signal_timeframe",
            "timeframe_seconds",
            "timeframe_5m",
            "timeframe_15m",
        ):
            raw = getattr(instance, attr_name, None)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                out.add(value)
        return out

    def _selector_ema_period_for_underlying(underlying_label: str) -> int:
        target = str(underlying_label or "").strip().upper()
        if not target:
            return 20
        fallback_periods: set[int] = set()
        for row in strategies:
            row_underlying = str(row.get("underlying") or "").strip().upper()
            if row_underlying != target:
                continue
            instance = row.get("instance")
            try:
                period = int(getattr(instance, "ema_period", None))
            except (TypeError, ValueError):
                continue
            if period <= 0:
                continue
            name = canonicalize_strategy_name(
                str(row.get("name") or ""),
                source=f"stream.selector_ema_period.{target}",
                warn_alias=False,
            )
            if name == "ema20_strategy":
                return period
            fallback_periods.add(period)
        if fallback_periods:
            if len(fallback_periods) > 1:
                logger.warning(
                    "Strategy selector found multiple EMA periods for %s (%s); using %d",
                    target,
                    sorted(fallback_periods),
                    min(fallback_periods),
                )
            return min(fallback_periods)
        return 20

    def _selector_eval_timeframes_for_underlying(
        underlying_label: str, *, ema_period: int
    ) -> set[int]:
        target = str(underlying_label or "").strip().upper()
        attached_timeframes: set[int] = set()
        for row in strategies:
            row_underlying = str(row.get("underlying") or "").strip().upper()
            if row_underlying != target:
                continue
            attached_timeframes.update(_selector_strategy_timeframes(row.get("instance")))

        ema_supported_timeframes = {
            int(tf_sec)
            for tf_sec, periods in (ema_periods_by_tf or {}).items()
            if ema_period in {int(period) for period in (periods or [])}
        }
        eval_timeframes = attached_timeframes & ema_supported_timeframes
        if eval_timeframes:
            return eval_timeframes
        if ema_supported_timeframes:
            return ema_supported_timeframes
        if attached_timeframes:
            return attached_timeframes
        return {300}

    if selector_config.enabled:
        strategy_selector = StrategySelector(selector_config)
        logger.info(
            "Auto strategy selector enabled | max_active_per_underlying=%d min_hold_seconds=%.1f mapped_underlyings=%s",
            selector_config.max_active_per_underlying,
            selector_config.min_hold_seconds,
            sorted(selector_config.mapping.keys()),
        )

    # Pre-initialized; filled by the unroutable check block after strategy switch is ready.
    unroutable_strategies: list[str] = []

    def _strategy_candidates_for_underlying(underlying_label: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        target = str(underlying_label or "").strip().upper()
        _exclude = bool(
            trade_mode == "LIVE"
            and str(os.getenv("AUTO_STRATEGY_SELECT_ENABLED", "false") or "false")
            .strip().lower() in {"1", "true", "yes", "on"}
            and unroutable_strategies
        )
        _unroutable = frozenset(unroutable_strategies) if _exclude else frozenset()
        for row in strategies:
            row_underlying = str(row.get("underlying") or "").strip().upper()
            if row_underlying != target:
                continue
            name = canonicalize_strategy_name(
                str(row.get("name") or ""),
                source=f"stream.strategy_candidates.{target}",
                warn_alias=False,
            )
            if not name or name in seen:
                continue
            seen.add(name)
            if not instrument_controller.is_strategy_allowed(target, name):
                continue
            if _exclude and name in _unroutable:
                continue
            out.append(name)
        return out

    def _publish_strategy_selection(underlying_label: str) -> None:
        if strategy_selector is None:
            return
        snapshot = strategy_selector.snapshot().get(
            str(underlying_label or "").strip().upper()
        )
        if not snapshot:
            return
        dashboard_bus.upsert_strategy_selection(
            snapshot.get("underlying") or str(underlying_label or "").strip().upper(),
            regime=str(snapshot.get("regime") or "NO_TRADE"),
            selected_strategies=list(snapshot.get("selected_strategies") or []),
            reason=str(snapshot.get("reason") or ""),
            hold_until=(
                str(snapshot.get("hold_until"))
                if snapshot.get("hold_until")
                else None
            ),
            updated_at=(
                str(snapshot.get("updated_at"))
                if snapshot.get("updated_at")
                else None
            ),
        )

    def _update_strategy_selection_for_bar(
        *,
        underlying_label: str,
        timeframe_seconds: int,
        candle: Any,
        indicators: dict,
        publish_state: bool = True,
        emit_log: bool = True,
    ) -> None:
        if strategy_selector is None:
            return
        key = str(underlying_label or "").strip().upper()
        if not key:
            return
        builder = selector_context_builders.setdefault(key, MarketContextBuilder())
        context = builder.build(
            candle=candle,
            indicators=indicators if isinstance(indicators, dict) else {},
        )
        eval_timeframes = selector_eval_timeframes.get(key)
        if eval_timeframes and int(timeframe_seconds) not in eval_timeframes:
            return
        classifier = selector_regime_classifiers.setdefault(key, RegimeClassifier())
        regime = classifier.update(context)
        decision = strategy_selector.select(
            underlying=key,
            regime=regime,
            allowed_strategies=_strategy_candidates_for_underlying(key),
            now=(
                getattr(candle, "end_ts", None)
                or getattr(candle, "start_ts", None)
                or datetime.now(timezone.utc)
            ),
        )
        if publish_state:
            _publish_strategy_selection(key)
        if emit_log and decision.changed:
            logger.info(
                "Strategy selector update | underlying=%s regime=%s selected=%s reason=%s hold_until=%s",
                key,
                decision.regime.value,
                list(decision.selected_strategies),
                decision.reason,
                decision.hold_until or "-",
            )

    def _is_strategy_selected(underlying_label: str, strategy_name: str) -> bool:
        if strategy_selector is None:
            return True
        # EMA20-5: EMA20 dynamic policy is authoritative for EMA20 entry decisions.
        # When ema20_is_authoritative=True, bypass selector gating for ema20_strategy
        # so regime-based suppression (e.g. NO_TRADE->[]) cannot override EMA20's own
        # per-regime disable_entries flag. Other strategies are unaffected.
        if selector_config.ema20_is_authoritative and canonicalize_strategy_name(
            str(strategy_name or ""), source="selector_bypass"
        ) == "ema20_strategy":
            return True
        return strategy_selector.is_strategy_active(
            underlying=str(underlying_label or "").strip().upper(),
            strategy_name=str(strategy_name or ""),
        )

    if strategy_selector is not None:
        for key in sorted(
            {
                str(row.get("underlying") or "").strip().upper()
                for row in strategies
                if str(row.get("underlying") or "").strip()
            }
        ):
            selector_ema_period = _selector_ema_period_for_underlying(key)
            selector_context_builders[key] = MarketContextBuilder(
                ema_period=selector_ema_period
            )
            selector_eval_timeframes[key] = _selector_eval_timeframes_for_underlying(
                key,
                ema_period=selector_ema_period,
            )
            logger.info(
                "Strategy selector context | underlying=%s ema_period=%d eval_timeframes=%s",
                key,
                selector_ema_period,
                sorted(selector_eval_timeframes[key]),
            )
            strategy_selector.select(
                underlying=key,
                regime=Regime.NO_TRADE,
                allowed_strategies=_strategy_candidates_for_underlying(key),
            )
            _publish_strategy_selection(key)
        selector_seed_labels = set(selector_context_builders.keys())
        if seeded_history_bars and selector_seed_labels:
            replayed = _replay_indicator_seed_bars(
                seed_bars=seeded_history_bars,
                timeframes=list(engine.timeframes),
                ema_periods_by_timeframe=ema_periods_by_tf,
                allowed_labels=selector_seed_labels,
                atr_period=engine.atr_period,
                rsi_period=engine.rsi_period,
                adx_period=engine.adx_period,
                macd_fast=engine.macd_fast,
                macd_slow=engine.macd_slow,
                macd_signal=engine.macd_signal,
                max_candles=engine.max_candles,
                on_bar=lambda label, timeframe_seconds, candle, indicators: _update_strategy_selection_for_bar(
                    underlying_label=label,
                    timeframe_seconds=timeframe_seconds,
                    candle=candle,
                    indicators=indicators,
                    publish_state=False,
                    emit_log=False,
                ),
            )
            if replayed:
                logger.info(
                    "Strategy selector warmup replayed %d seeded bars for underlyings=%s",
                    replayed,
                    sorted(selector_seed_labels),
                )
                for key in sorted(selector_seed_labels):
                    _publish_strategy_selection(key)
                    snapshot = strategy_selector.snapshot().get(key)
                    if not snapshot:
                        continue
                    logger.info(
                        "Strategy selector warmup snapshot | underlying=%s regime=%s selected=%s reason=%s",
                        key,
                        snapshot.get("regime") or "NO_TRADE",
                        list(snapshot.get("selected_strategies") or []),
                        snapshot.get("reason") or "",
                    )

    instrument_policy_snapshot = instrument_controller.snapshot()
    strategy_switch_snapshot = strategy_switch.snapshot()

    # --- Issue #33: detect attached strategies that still lack hub routes. ---
    try:
        from app.hub.routing_table import get_global_routing_table
        _rt = get_global_routing_table()
        routed_ids = _rt.routed_strategy_ids()
        _auto_select_enabled = str(
            os.getenv("AUTO_STRATEGY_SELECT_ENABLED", "false") or "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        _attached_strategy_names = {
            canonicalize_strategy_name(
                str(row.get("name") or ""),
                source="stream.unroutable_check.attached",
                warn_alias=False,
            )
            for row in strategies
            if str(row.get("name") or "").strip()
        }
        for _sname in sorted(name for name in _attached_strategy_names if name):
            if _sname not in routed_ids:
                unroutable_strategies.append(_sname)
                logger.warning(
                    "strategy.unroutable name=%s - attached at runtime but has no routing entry; "
                    "signals from this strategy will be dropped by the router",
                    _sname,
                )
        if unroutable_strategies and trade_mode == "LIVE":
            log_level = logging.ERROR if _auto_select_enabled else logging.WARNING
            logger.log(
                log_level,
                "strategy.unroutable_selector_excluded count=%d names=%s - "
                "unroutable strategies %s from selector evaluation in LIVE mode; "
                "add routing entries to strategy_configs/HUB_ROUTES_JSON or disable the runtime attachment",
                len(unroutable_strategies),
                sorted(unroutable_strategies),
                "excluded" if _auto_select_enabled else "present but not auto-excluded",
            )
    except Exception as _exc:
        logger.warning("Unroutable strategy check failed: %s", _exc)
    strict_intraday_state = {
        "enabled": bool(strict_intraday_enabled),
        "target_strategies": sorted(strict_target_names),
        "require_hub_routing": bool(strict_require_hub_routing),
        "retry_enabled": bool(strict_eod_retry_enabled),
        "telemetry_enabled": bool(strict_eod_telemetry_enabled),
        "enforcement_time": (
            strict_eod_enforcement_time.strftime("%H:%M")
            if strict_eod_enforcement_time
            else None
        ),
        "retry_cutoff_time": (
            strict_eod_retry_cutoff_time.strftime("%H:%M")
            if strict_eod_retry_cutoff_time
            else None
        ),
        "retry_interval_seconds": float(strict_eod_retry_interval_seconds),
    }
    indicator_plan_snapshot = _build_indicator_plan_snapshot(
        engine_timeframes=engine_timeframes,
        ema_periods_by_timeframe=ema_periods_by_tf,
        adx_period=engine_adx_period,
    )
    indicator_seed_summary = _build_indicator_seed_summary(
        seed_source=seeded_history_source,
        requested_bars_per_series=seed_bars,
        seeded_bars=seeded_history_count,
        seed_bars=seeded_history_bars,
    )
    startup_snapshot = _build_strategy_runtime_startup_snapshot(
        process_start_ts=process_start_ts,
        trade_mode=trade_mode,
        strategy_cfg=(strategy_cfg if isinstance(strategy_cfg, dict) else {}),
        env=boot_cfg.env,
        strategies=strategies,
        instrument_policy_snapshot=instrument_policy_snapshot,
        strategy_switch_snapshot=strategy_switch_snapshot,
        selector_config=selector_config,
        selector_state=(
            strategy_selector.snapshot()
            if strategy_selector is not None
            else {}
        ),
        selector_staleness_check=(
            strategy_selector.staleness_summary()
            if strategy_selector is not None
            else {"status": "no_selector"}
        ),
        unroutable_strategies=unroutable_strategies,
        strict_intraday_state=strict_intraday_state,
        indicator_plan_snapshot=indicator_plan_snapshot,
        indicator_seed_summary=indicator_seed_summary,
    )
    startup_snapshot_path = _persist_strategy_runtime_startup_snapshot(
        snapshot=startup_snapshot,
        logs_dir=_daily_logs_dir(),
    )
    logger.info(
        "Strategy runtime startup snapshot written | path=%s config_hash=%s attached_underlyings=%s",
        startup_snapshot_path,
        startup_snapshot.get("config_hash") or "",
        sorted(
            startup_snapshot.get("attached_strategies_by_underlying", {}).keys()
        ),
    )
    _validate_required_strategy_attachments(
        trade_mode=trade_mode,
        strategies=strategies,
        instrument_policy_snapshot=instrument_policy_snapshot,
        enabled_strategy_names=[
            name for name, enabled in initial_flags.items() if bool(enabled)
        ],
    )

    strategy_id_by_name: Dict[str, StrategyId] = {}
    for row in strategies:
        name = str(row.get("name") or "").strip()
        sid_raw = _strategy_id_for_instance(row.get("instance"))
        if not name or not sid_raw:
            continue
        strategy_id_by_name[name] = StrategyId(sid_raw)

    runtime_exit_hub_routing_enabled = bool(
        _runtime_settings().use_hub_router
        and _is_true_env("HUB_ROUTE_RUNTIME_EXITS", default=True)
    )
    runtime_exit_default_tenant = str(
        getattr(settings, "hub_default_tenant_id", "") or ""
    ).strip()
    runtime_exit_default_broker = str(
        getattr(settings, "hub_default_broker_account_id", "") or ""
    ).strip()
    if runtime_exit_hub_routing_enabled and (
        not runtime_exit_default_tenant or not runtime_exit_default_broker
    ):
        logger.warning(
            "HUB_ROUTE_RUNTIME_EXITS enabled but hub_default_tenant_id/hub_default_broker_account_id missing; "
            "falling back to direct broker safety exits."
        )
        runtime_exit_hub_routing_enabled = False

    if runtime_exit_hub_routing_enabled:
        logger.info(
            "Runtime exit routing mode: hub (HUB_ROUTE_RUNTIME_EXITS=true, tenant=%s broker_account=%s)",
            runtime_exit_default_tenant,
            runtime_exit_default_broker,
        )
        default_tenant_id = TenantId(runtime_exit_default_tenant)
        default_broker_account_id = BrokerAccountId(runtime_exit_default_broker)

        def _submit_runtime_exit_via_hub(
            *,
            label: str,
            strategy_name: Optional[str],
            exchange: str,
            symboltoken: str,
            tradingsymbol: str,
            side: str,
            quantity: int,
            trade_mode: str,
            tag: Optional[str],
            reason: Optional[str],
        ) -> bool:
            if str(trade_mode or "").upper() not in {"LIVE", "SHADOW"}:
                return False
            side_norm = str(side or "").upper()
            if side_norm not in {"BUY", "SELL"}:
                return False
            if int(quantity or 0) <= 0:
                return False

            strategy_name_raw = str(strategy_name or "").strip()
            strategy_name_key = (
                canonicalize_strategy_name(
                    strategy_name_raw,
                    source="runtime_exit_strategy_name",
                )
                if strategy_name_raw
                else ""
            )
            resolved_id = (
                strategy_id_by_name.get(strategy_name_key)
                or strategy_id_by_name.get(strategy_name_raw)
            )
            # §74: In LIVE hub-authoritative mode the synthetic __runtime_exit__
            # fallback is forbidden — it breaks OwnershipKey canonicalization and
            # the exit audit trail.  Refuse the exit rather than poison the DB
            # with an unattributable ownership record.
            if resolved_id is None:
                if str(trade_mode or "").upper() == "LIVE":
                    logger.error(
                        "runtime_exit.strategy_unresolvable: LIVE mode requires a real "
                        "strategy_id for hub exits but strategy_name=%r is not in the "
                        "strategy registry. Exit for label=%s REFUSED to prevent "
                        "__runtime_exit__ contamination in OwnershipKeys. "
                        "Ensure the caller provides the owning strategy name.",
                        strategy_name_raw or None,
                        label,
                    )
                    return False
                # Non-LIVE (SHADOW, PAPER): allow synthetic fallback for testing.
                resolved_id = RUNTIME_EXIT_STRATEGY_ID
            strategy_id = resolved_id
            submit_tag = str(tag or reason or "RUNTIME_EXIT").strip()
            order_req = OrderRequest(
                symbol=str(tradingsymbol or label or "").strip() or str(label),
                quantity=int(quantity),
                side=OrderSide(side_norm),
                order_type=OrderType.MARKET,
                product_type=ProductType.INTRADAY,
                time_in_force=TimeInForce.DAY,
                limit_price=None,
                stop_price=None,
                tag=submit_tag or "RUNTIME_EXIT",
                purpose=OrderPurpose.EXIT,
                exchange=str(exchange or "").strip() or None,
                symbol_token=str(symboltoken or "").strip() or None,
            )
            try:
                response = place_order_via_bridge(
                    strategy_id=strategy_id,
                    order_req=order_req,
                    tenant_id=default_tenant_id,
                    broker_account_id=default_broker_account_id,
                )
            except Exception as exc:
                logger.warning(
                    "Hub runtime exit failed for %s (strategy=%s): %s",
                    label,
                    strategy_name_raw or "unknown",
                    exc,
                )
                return False
            status = str(getattr(response, "status", "") or "").upper()
            if status in {"FAILED", "REJECTED"}:
                logger.warning(
                    "Hub runtime exit rejected for %s (strategy=%s): status=%s",
                    label,
                    strategy_name_raw or str(strategy_id),
                    status or "UNKNOWN",
                )
                return False
            return True

        risk_manager.set_hub_exit_submitter(_submit_runtime_exit_via_hub)
    else:
        # In hub-authoritative LIVE mode the direct broker fallback path is
        # structurally forbidden (Issue #54 / Architecture §LIVE-exit-routing).
        # HUB_ROUTE_RUNTIME_EXITS must be true and hub identity vars must be set.
        if trade_mode == "LIVE" and _runtime_settings().use_hub_router:
            raise P0RuleViolation(
                54,
                "LIVE hub-authoritative mode requires HUB_ROUTE_RUNTIME_EXITS=true "
                "and valid HUB_DEFAULT_TENANT_ID / HUB_DEFAULT_BROKER_ACCOUNT_ID. "
                f"Current: HUB_ROUTE_RUNTIME_EXITS={os.getenv('HUB_ROUTE_RUNTIME_EXITS', 'true')!r} "
                f"tenant={runtime_exit_default_tenant!r} "
                f"account={runtime_exit_default_broker!r}. "
                "Direct broker fallback is structurally forbidden in this mode.",
            )
        logger.info(
            "Runtime exit routing mode: direct broker fallback (HUB_ROUTE_RUNTIME_EXITS=%s, use_hub_router=%s)",
            str(os.getenv("HUB_ROUTE_RUNTIME_EXITS", "true")),
            bool(_runtime_settings().use_hub_router),
        )
        risk_manager.set_hub_exit_submitter(None)

    signal_metrics = get_signal_summary_metrics()
    try:
        signal_summary_interval_seconds = max(
            5.0,
            float(os.getenv("SIGNAL_SUMMARY_INTERVAL_SECONDS", "60")),
        )
    except (TypeError, ValueError):
        signal_summary_interval_seconds = 60.0
    tick_metrics = get_labeled_metrics()
    try:
        stream_event_queue_max_pending = max(
            0,
            int(os.getenv("STREAM_EVENT_QUEUE_MAX_PENDING", "0")),
        )
    except (TypeError, ValueError):
        stream_event_queue_max_pending = 0
    event_queue = StreamEventQueue(
        name="stream-event-queue",
        max_pending=stream_event_queue_max_pending,
    )
    event_queue.start()
    try:
        tick_coalesce_threshold = max(
            0,
            int(os.getenv("STREAM_TICK_COALESCE_QUEUE_THRESHOLD", "200")),
        )
    except (TypeError, ValueError):
        tick_coalesce_threshold = 200
    try:
        tick_max_coalesced_keys = max(
            0,
            int(os.getenv("STREAM_TICK_MAX_COALESCED_KEYS", "512")),
        )
    except (TypeError, ValueError):
        tick_max_coalesced_keys = 512
    tick_dispatcher = LatestPerKeyDispatcher(
        event_queue,
        overload_threshold=tick_coalesce_threshold,
        max_keys=tick_max_coalesced_keys,
    )

    # Handle a closed bar: persist, update dashboard, and drive strategies.
    def on_bar(label, timeframe_seconds, candle, indicators):
        logger.info(
            "BAR CLOSED | %s | tf=%ss | start_ist=%s close=%.3f | ATR=%s RSI=%s MACD=%s SIG=%s HIST=%s EMA20=%s",
            label,
            timeframe_seconds,
            _to_ist_iso(candle.start_ts),
            candle.c,
            indicators.get("atr"),
            indicators.get("rsi"),
            indicators.get("macd"),
            indicators.get("macd_signal"),
            indicators.get("macd_hist"),
            indicators.get("ema_20"),
        )
        dashboard_bus.record_bar(label, timeframe_seconds, candle, indicators)
        persister.persist_bar(label, timeframe_seconds, candle, indicators)

        # Only drive strategies from underlying instruments
        meta = instrument_state["instrument_meta"].get(label)
        if not meta or meta.get("kind") != "UNDERLYING":
            return

        underlying_label = instrument_state["label_to_underlying"].get(label, label)
        maintenance.clear_entry_block(
            underlying_label,
            reason=_indicator_gap_block_reason(timeframe_seconds),
        )
        if not instrument_controller.is_enabled(underlying_label):
            return
        _update_strategy_selection_for_bar(
            underlying_label=underlying_label,
            timeframe_seconds=timeframe_seconds,
            candle=candle,
            indicators=indicators,
        )
        bar_ts = (
            getattr(candle, "end_ts", None)
            or getattr(candle, "start_ts", None)
            or datetime.now(timezone.utc)
        )
        if isinstance(bar_ts, datetime) and bar_ts.tzinfo is None:
            bar_ts = bar_ts.replace(tzinfo=timezone.utc)
        if isinstance(bar_ts, datetime):
            _enforce_strict_intraday_squareoff(bar_ts, source="bar")
        for strat in strategies:
            if not _should_dispatch_strategy_bar(
                bar_label=label,
                underlying_label=underlying_label,
                timeframe_seconds=timeframe_seconds,
                candle=candle,
                bar_ts=bar_ts if isinstance(bar_ts, datetime) else None,
                strategy_row=strat,
                strategy_switch=strategy_switch,
                instrument_controller=instrument_controller,
                is_strategy_selected=_is_strategy_selected,
                is_strict_target_strategy=_is_strict_target_strategy,
                strict_cutoff_reached=_strict_cutoff_reached,
            ):
                continue
            signal_metrics.mark_signal_checked()
            strat["instance"].on_bar(label, timeframe_seconds, candle, indicators)

    engine.on_bar = on_bar

    last_prices = {}
    profit_sweep_state = {"last_check_ts": 0.0, "swept_date": None}

    # Resolve the daily profit sweep target from settings.
    def _profit_sweep_target() -> Optional[float]:
        runtime_settings = _runtime_settings()
        if runtime_settings.use_hub_router:
            return None
        if not runtime_settings.enable_profit_checks:
            return None
        if not runtime_settings.profit_enable_daily_target:
            return None
        target = (
            runtime_settings.profit_daily_target_paper
            if trade_mode in {"PAPER", "SHADOW"} and runtime_settings.profit_daily_target_paper is not None
            else runtime_settings.profit_daily_target
        )
        if target is None:
            return None
        try:
            target_val = float(target)
        except (TypeError, ValueError):
            return None
        if target_val <= 0:
            return None
        return target_val

    # Trigger a profit sweep if the target is reached.
    def _maybe_profit_sweep(ts: datetime) -> None:
        if trade_mode == "LIVE":
            try:
                assert_legacy_exit_allowed(
                    operating_mode=_stream_operating_mode_str,
                    is_break_glass=False,
                    exit_source="profit_sweep",
                    scope_key="stream:profit_sweep",
                )
            except P0RuleViolation:
                return
        target = _profit_sweep_target()
        if target is None:
            return
        now_mono = time.monotonic()
        if now_mono - profit_sweep_state["last_check_ts"] < 5.0:
            return
        profit_sweep_state["last_check_ts"] = now_mono
        try:
            risk_manager._reset_daily_if_new_day(ts)
        except Exception:
            pass
        today = ts.astimezone(IST).date()
        if profit_sweep_state["swept_date"] == today:
            return
        current_pnl = float(getattr(risk_manager, "daily_realized_pnl", 0.0) or 0.0)
        if current_pnl < target:
            return
        logger.warning(
            "Profit sweep triggered: realized_pnl=%.2f target=%.2f",
            current_pnl,
            target,
        )
        for strat in strategies:
            inst = strat.get("instance")
            if inst is not None and hasattr(inst, "force_exit_all"):
                try:
                    inst.force_exit_all(reason="PROFIT_SWEEP", submit_orders=True)
                except Exception as exc:
                    logger.warning("Profit sweep exit failed for %s: %s", strat, exc)
        if risk_manager.open_positions:
            risk_manager.square_off_all(
                trade_mode=trade_mode,
                ltp_lookup=last_prices,
                tag="PROFIT_SWEEP",
            )
        if not risk_manager.open_positions:
            for strat in strategies:
                inst = strat.get("instance")
                if inst is not None and hasattr(inst, "force_exit_all"):
                    try:
                        inst.force_exit_all(
                            reason="PROFIT_SWEEP", submit_orders=False
                        )
                    except Exception as exc:
                        logger.warning(
                            "Profit sweep state reset failed for %s: %s", strat, exc
                        )
        profit_sweep_state["swept_date"] = today

    strict_intraday_state: Dict[str, Any] = {
        "date": None,
        "last_attempt_mono": 0.0,
        "cutoff_warning_logged": False,
    }

    def _is_strict_target_strategy(name: Any) -> bool:
        if not strict_intraday_enabled:
            return False
        canonical = canonicalize_strategy_name(
            str(name or ""),
            source="stream.intraday_strict_is_target",
            warn_alias=False,
        )
        return canonical in strict_target_names

    def _strict_cutoff_reached(ts: datetime) -> bool:
        return ts.astimezone(IST).time() >= strict_eod_enforcement_time

    def _strict_retry_window_open(ts: datetime) -> bool:
        if strict_eod_retry_cutoff_time is None:
            return True
        return ts.astimezone(IST).time() < strict_eod_retry_cutoff_time

    def _strict_open_positions_snapshot() -> List[tuple[str, Dict[str, Any]]]:
        snapshot = list(risk_manager.open_positions.items())
        out: List[tuple[str, Dict[str, Any]]] = []
        for label, entry in snapshot:
            strategy_name = canonicalize_strategy_name(
                str(entry.get("strategy_name") or ""),
                source="stream.intraday_strict_open_positions",
                warn_alias=False,
            )
            if strategy_name not in strict_target_names:
                continue
            qty = _safe_int(entry.get("qty"))
            if qty is None or int(qty) <= 0:
                continue
            out.append((label, entry))
        return out

    def _enforce_strict_intraday_squareoff(ts: datetime, *, source: str) -> None:
        if not strict_intraday_enabled or not strict_target_names:
            return
        if trade_mode == "LIVE":
            try:
                assert_legacy_exit_allowed(
                    operating_mode=_stream_operating_mode_str,
                    is_break_glass=False,
                    exit_source="strict_intraday_squareoff",
                    scope_key="stream:strict_intraday",
                )
            except P0RuleViolation:
                return
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now_ist = ts.astimezone(IST)
        today = now_ist.date()
        if strict_intraday_state["date"] != today:
            strict_intraday_state["date"] = today
            strict_intraday_state["last_attempt_mono"] = 0.0
            strict_intraday_state["cutoff_warning_logged"] = False
        if not _strict_cutoff_reached(ts):
            return

        retry_window_open = _strict_retry_window_open(ts)
        now_mono = time.monotonic()
        if strict_eod_retry_enabled:
            if not retry_window_open:
                if (
                    strict_eod_telemetry_enabled
                    and not strict_intraday_state["cutoff_warning_logged"]
                ):
                    residual = _strict_open_positions_snapshot()
                    if residual:
                        logger.error(
                            "Strict intraday retry cutoff reached with residual target positions: %s",
                            ", ".join(f"{label}:{entry.get('strategy_name')}" for label, entry in residual),
                        )
                    strict_intraday_state["cutoff_warning_logged"] = True
                return
            if (
                now_mono - float(strict_intraday_state["last_attempt_mono"])
                < strict_eod_retry_interval_seconds
            ):
                return
        else:
            if float(strict_intraday_state["last_attempt_mono"]) > 0.0:
                return
        strict_intraday_state["last_attempt_mono"] = now_mono

        forced_strategy_calls = 0
        for strat in strategies:
            if not _is_strict_target_strategy(strat.get("name")):
                continue
            inst = strat.get("instance")
            if inst is None or not hasattr(inst, "force_exit_all"):
                continue
            try:
                inst.force_exit_all(
                    reason="STRICT_INTRADAY_EOD", submit_orders=True
                )
                forced_strategy_calls += 1
            except Exception as exc:
                logger.warning(
                    "Strict intraday force_exit_all failed for strategy=%s underlying=%s: %s",
                    strat.get("name"),
                    strat.get("underlying"),
                    exc,
                )

        closed_labels: set[str] = set()
        try:
            closed_labels = risk_manager.square_off_strategies(
                strategy_names=strict_target_names,
                trade_mode=trade_mode,
                ltp_lookup=last_prices,
                tag="STRICT_INTRADAY_EOD",
                reason="STRICT_INTRADAY_EOD",
            )
        except Exception as exc:
            logger.warning("Strict intraday risk-manager square-off failed: %s", exc)

        residual = _strict_open_positions_snapshot()
        if strict_eod_telemetry_enabled:
            logger.info(
                "[INTRADAY_STRICT] cycle | source=%s ts_ist=%s targets=%s forced_strategy_calls=%d closed_labels=%d residual_positions=%d retry_enabled=%s retry_window_open=%s",
                source,
                now_ist.isoformat(),
                sorted(strict_target_names),
                forced_strategy_calls,
                len(closed_labels),
                len(residual),
                strict_eod_retry_enabled,
                retry_window_open,
            )
        if (
            residual
            and not strict_eod_retry_enabled
            and not strict_intraday_state["cutoff_warning_logged"]
        ):
            logger.warning(
                "Strict intraday one-shot enforcement left residual target positions: %s",
                ", ".join(f"{label}:{entry.get('strategy_name')}" for label, entry in residual),
            )
            strict_intraday_state["cutoff_warning_logged"] = True

    try:
        base_position_sync_interval = int(
            os.getenv("POSITION_SYNC_INTERVAL_SECONDS", "300")
        )
    except (TypeError, ValueError):
        base_position_sync_interval = 300
    base_position_sync_interval = max(5, base_position_sync_interval)
    if trade_mode == "SHADOW":
        logger.info(
            "Disabling legacy broker position sync loop for TRADE_MODE=SHADOW (execution remains virtual)."
        )
        base_position_sync_interval = 0
    try:
        position_sync_max_interval = int(
            os.getenv(
                "POSITION_SYNC_MAX_INTERVAL_SECONDS",
                str(base_position_sync_interval * 4),
            )
        )
    except (TypeError, ValueError):
        position_sync_max_interval = base_position_sync_interval * 4
    position_sync_max_interval = max(
        base_position_sync_interval, position_sync_max_interval
    )
    try:
        position_sync_blocked_min_interval = float(
            os.getenv(
                "POSITION_SYNC_BLOCKED_MIN_INTERVAL_SECONDS",
                os.getenv(
                    "ANGEL_FETCH_RATE_LIMIT_FLOOR_SECONDS",
                    str(base_position_sync_interval),
                ),
            )
        )
    except (TypeError, ValueError):
        position_sync_blocked_min_interval = float(base_position_sync_interval)
    position_sync_blocked_min_interval = max(
        float(base_position_sync_interval),
        position_sync_blocked_min_interval,
    )
    try:
        ws_error_log_throttle_seconds = max(
            1.0, float(os.getenv("WS_APP_ERROR_LOG_THROTTLE_SECONDS", "15"))
        )
    except (TypeError, ValueError):
        ws_error_log_throttle_seconds = 15.0
    try:
        route_error_log_throttle_seconds = max(
            1.0, float(os.getenv("ROUTE_ERROR_LOG_THROTTLE_SECONDS", "30"))
        )
    except (TypeError, ValueError):
        route_error_log_throttle_seconds = 30.0
    ws_error_state: Dict[str, Any] = {
        "last_msg": None,
        "last_ts": 0.0,
        "suppressed": 0,
    }
    route_error_state: Dict[str, Any] = {
        "last_msg": None,
        "last_ts": 0.0,
        "suppressed": 0,
    }

    def _log_ws_error_throttled(message: str) -> None:
        now_mono = time.monotonic()
        if (
            ws_error_state["last_msg"] == message
            and (now_mono - float(ws_error_state["last_ts"]))
            < ws_error_log_throttle_seconds
        ):
            ws_error_state["suppressed"] = int(ws_error_state["suppressed"]) + 1
            return
        suppressed = int(ws_error_state["suppressed"])
        suffix = f" (suppressed {suppressed} repeats)" if suppressed else ""
        logger.error("%s%s", message, suffix)
        ws_error_state["suppressed"] = 0
        ws_error_state["last_msg"] = message
        ws_error_state["last_ts"] = now_mono

    def _log_route_error_throttled(message: str) -> None:
        now_mono = time.monotonic()
        if (
            route_error_state["last_msg"] == message
            and (now_mono - float(route_error_state["last_ts"]))
            < route_error_log_throttle_seconds
        ):
            route_error_state["suppressed"] = int(route_error_state["suppressed"]) + 1
            return
        suppressed = int(route_error_state["suppressed"])
        suffix = f" (suppressed {suppressed} repeats)" if suppressed else ""
        logger.error("%s%s", message, suffix)
        route_error_state["suppressed"] = 0
        route_error_state["last_msg"] = message
        route_error_state["last_ts"] = now_mono

    def _copy_tick_message(message: dict) -> dict:
        payload = dict(message or {})
        ohlc = payload.get("ohlc")
        if isinstance(ohlc, dict):
            payload["ohlc"] = dict(ohlc)
        return payload

    # Process tick data, update indicators, and run strategies.
    def _process_tick_message(message: dict, *, enqueued_mono: Optional[float] = None):
        if enqueued_mono is not None:
            _set_tick_flow_metrics(
                tick_metrics,
                event_queue=event_queue,
                tick_dispatcher=tick_dispatcher,
                processing_lag_ms=(time.monotonic() - enqueued_mono) * 1000.0,
            )
        token = str(message["token"])
        exch_type = message.get("exchange_type")
        label = token_labels_ref["map"].get(token)
        if label is None:
            resolved_label: Optional[str] = None
            if token_label_self_heal_enabled:
                resolved_label = _resolve_label_from_runtime_state(token, exch_type)
            if resolved_label:
                label = resolved_label
                token_labels_ref["map"][token] = resolved_label
                instrument_state["token_labels"][token] = resolved_label
            elif drop_unmapped_ticks:
                now_mono = time.monotonic()
                last_log_mono = float(unmapped_tick_last_log_mono.get(token, 0.0))
                if now_mono - last_log_mono >= unmapped_tick_log_interval_seconds:
                    logger.warning(
                        "Dropping unmapped tick token=%s exchType=%s; no active label mapping",
                        token,
                        exch_type,
                    )
                    unmapped_tick_last_log_mono[token] = now_mono
                return
            else:
                label = token
        if label is None:
            label = token
        ltp = message["last_traded_price"] / 100.0
        ohlc = message.get("ohlc", {})
        open_price = ohlc.get("open")
        high_price = ohlc.get("high")
        low_price = ohlc.get("low")
        close_price = ohlc.get("close")
        sub_mode = message.get("subscription_mode_val")

        # Log every tick with extensive detail
        # logger.debug(
        #     f"📡 [WS_DATA] Token={token} | Label={label} | LTP={ltp:.3f} | "
        #     f"Mode={sub_mode} | OHLC={open_price}/{high_price}/{low_price}/{close_price}"
        # )

        if tick_log_every:
            if tick_counter["count"] % tick_log_every == 0:
                logger.debug(
                    "%s | exchType=%s | token=%s | LTP=%.3f | O=%s H=%s L=%s C=%s | Mode=%s",
                    f"{label:15s}",
                    str(exch_type),
                    token,
                    ltp,
                    str(open_price),
                    str(high_price),
                    str(low_price),
                    str(close_price),
                    str(sub_mode),
                )
            tick_counter["count"] += 1

        ts = datetime.now(timezone.utc)
        meta = instrument_state["instrument_meta"].get(label)
        
        # Log trading window check
        in_window = _is_in_trading_window(meta, ts)
        if not in_window:
            # logger.debug(f"⏰ [WINDOW] {label} outside trading window (meta={meta})")
            return
        
        # logger.debug(f"✅ [WINDOW] {label} in trading window, processing")
        
        last_prices[label] = ltp
        executed_tokens_tracker.on_tick(label, ltp)

        # Log dashboard bus update
        # logger.debug(f"📊 [DASHBOARD] Recording tick for {label}: {ltp}")
        dashboard_bus.record_tick(label, ltp, ts)

        # RISK-5.4: Feed India VIX tick into circuit breaker for volatility protection.
        if label == "INDIAVIX_IDX":
            try:
                from app.hub.runtime import get_hub_runtime
                get_hub_runtime().update_volatility(ltp)
            except Exception:
                pass  # Hub runtime may not be ready during early startup
            return  # Data-only feed — no strategy dispatch needed

        try:
            risk_manager.evaluate_account_loss(
                now=ts,
                source=f"tick:{label}",
                force=False,
            )
        except Exception as exc:
            logger.warning(
                "[RISK] Account loss evaluation failed for %s: %s",
                label,
                exc,
            )
        
        underlying_label = instrument_state["label_to_underlying"].get(label, label)

        # Heartbeat tracking:
        # - Always update for true underlyings.
        # - Also update for *critical* labels like indices and NG_FUT.
        # - If a tick arrives for a derivative leg (options), refresh the critical underlying label
        #   to avoid false stale warnings when only leg ticks arrive.
        if (meta and meta.get("kind") == "UNDERLYING") or (label.upper() in CRITICAL_HEARTBEAT_LABELS):
            # logger.debug(f"💓 [HEARTBEAT] Updating heartbeat for {label}")
            maintenance.update_tick(label, ts)
        if underlying_label != label and underlying_label.upper() in CRITICAL_HEARTBEAT_LABELS:
            maintenance.update_tick(underlying_label, ts)
        
        enabled = instrument_controller.is_enabled(underlying_label)
        # logger.debug(f"🎚️  [CONTROL] {underlying_label} enabled={enabled}")
        if not enabled:
            return

        _enforce_strict_intraday_squareoff(ts, source="tick")

        if maintenance.should_square_off(ts):
            _eod_exit_allowed = True
            if trade_mode == "LIVE":
                try:
                    assert_legacy_exit_allowed(
                        operating_mode=_stream_operating_mode_str,
                        is_break_glass=False,
                        exit_source="eod_square_off",
                        scope_key="stream:eod_square_off",
                    )
                except P0RuleViolation:
                    _eod_exit_allowed = False
            if _eod_exit_allowed:
                processed = maintenance.eod_processed_underlyings(ts)
                closed_underlyings = risk_manager.square_off_eod(
                    trade_mode=trade_mode,
                    ltp_lookup=last_prices,
                    exclude_underlyings={"NG"},
                    processed_underlyings=processed,
                    tag="EOD_SQUARE_OFF",
                )
                if closed_underlyings:
                    maintenance.mark_eod_processed(closed_underlyings, ts)
                    for strat in strategies:
                        inst = strat.get("instance")
                        if inst is not None and hasattr(inst, "force_exit_all"):
                            try:
                                inst.force_exit_all(
                                    reason="EOD_SQUARE_OFF", submit_orders=False
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Strategy state reset failed after EOD square-off: %s",
                                    exc,
                                )
        elif maintenance.should_log_eod_warning(ts):
            remaining = [
                f"{lbl}({entry.get('underlying')})"
                for lbl, entry in risk_manager.open_positions.items()
                if str(entry.get("underlying") or "").upper() not in {"NG"}
            ]
            if remaining:
                logger.warning(
                    "EOD window passed but positions remain open: %s",
                    ", ".join(remaining),
                )
            maintenance.mark_eod_warning_logged(ts)
        monitor_labels = index_monitor.required_labels() if index_monitor else set()
        maintenance.check_heartbeat(
            label_filter=lambda stale_label: (
                instrument_state["instrument_meta"].get(stale_label) is not None
                and _is_in_trading_window(
                    instrument_state["instrument_meta"].get(stale_label), ts
                )
            ),
            suppress_labels=monitor_labels,
        )
        if index_monitor is not None:
            index_monitor.check(ts, maintenance.last_tick_by_label)
        try:
            # logger.debug(f"📈 [ENGINE] Updating indicator engine for {label}: {ltp}")
            engine.update_from_tick(label, ltp, timestamp=ts)
            
            for strat in strategies:
                if not strategy_switch.is_enabled(strat["name"]):
                    continue
                if not instrument_controller.is_strategy_allowed(
                    strat["underlying"], strat["name"]
                ):
                    continue
                if not _is_strategy_selected(strat["underlying"], strat["name"]):
                    continue
                if strat.get("underlying") != underlying_label:
                    continue
                if _is_strict_target_strategy(strat.get("name")) and _strict_cutoff_reached(ts):
                    continue
                signal_metrics.mark_signal_checked()
                # logger.debug(f"📋 [STRATEGY] {strat['name']} on_tick({label}, {ltp})")
                strat["instance"].on_tick(label, ltp)
            try:
                _maybe_profit_sweep(ts)
            except Exception as exc:
                logger.warning("Profit sweep check failed: %s", exc)
            signal_metrics.maybe_log_summary(
                logger,
                signal_summary_interval_seconds,
            )
        except Exception as exc:
            msg = str(exc)
            if "no active routes for strategy_id=" in msg:
                _log_route_error_throttled(
                    f"Route error on_data for {label}: {msg}"
                )
            else:
                logger.exception("[ERROR] on_data processing failed for %s: %s", label, exc)

    def on_data(wsapp, message: dict):
        try:
            payload = _copy_tick_message(message)
            enqueued_mono = time.monotonic()
            token_key = "%s:%s" % (
                str(payload.get("exchange_type") or ""),
                str(payload.get("token") or ""),
            )
            action = tick_dispatcher.submit(
                token_key,
                lambda payload=payload, enqueued_mono=enqueued_mono: _process_tick_message(
                    payload,
                    enqueued_mono=enqueued_mono,
                ),
                description="tick",
            )
            _record_tick_throttle_action(tick_metrics, action)
            _set_tick_flow_metrics(
                tick_metrics,
                event_queue=event_queue,
                tick_dispatcher=tick_dispatcher,
            )
        except Exception as exc:
            logger.exception("Failed to enqueue tick event: %s", exc)

    # Log control messages from the websocket.
    def on_control(wsapp, msg: dict):
        logger.info("CONTROL: %s", msg)

    # Log websocket errors.
    def on_error(wsapp, error):
        _log_ws_error_throttled(f"WebSocket error: {error}")

    # Log websocket close events.
    def on_close(wsapp):
        logger.info("WebSocket closed")

    logger.info("[WEBSOCKET] Initializing runner with token_list...")
    total_tokens = sum(len(entry.get("tokens", [])) for entry in token_list)
    logger.info(f"   Exchanges: {len(token_list)}, Total tokens: {total_tokens}")
    for entry in token_list:
        logger.info(f"   Exchange {entry.get('exchangeType')}: {len(entry.get('tokens', []))} tokens")
    
    runner = SmartWebSocketRunner(
        auth_header_value,
        api_key,
        client_code,
        feed_token,
        token_list,
    )
    if index_required_labels:
        index_monitor = IndexFeedMonitor(
            runner=runner,
            instrument_meta=instrument_meta,
            required_labels=index_required_labels,
            warn_seconds=feed_warn_seconds,
            resubscribe_cooldown_seconds=30.0,
            reconnect_after_seconds=feed_reconnect_seconds,  # Use configurable threshold
        )

    runner.set_handlers(
        on_data=on_data,
        on_control=on_control,
        on_error=on_error,
        on_close=on_close,
    )

    def _apply_token_label_refresh(next_token_labels: Dict[str, str]) -> None:
        if token_labels_ref["map"] != next_token_labels:
            token_labels_ref["map"] = dict(next_token_labels)
            instrument_state["token_labels"] = dict(next_token_labels)
        _prune_indicator_state_from_token_map()

    def _apply_atm_refresh_changes(
        *,
        next_instruments: List[Dict[str, Any]],
        next_token_labels: Dict[str, str],
        next_token_list: List[Dict[str, Any]],
        next_instrument_meta: Dict[str, Dict[str, Any]],
        next_label_to_underlying: Dict[str, str],
    ) -> None:
        logger.info("ATM refresh detected changes, resubscribing...")
        old_label_map = dict(token_labels_ref["map"])
        old_token_list = instrument_state["token_list"]
        old_instrument_meta = dict(instrument_state["instrument_meta"])
        merged_labels = dict(old_label_map)
        merged_labels.update(next_token_labels)
        token_labels_ref["map"] = merged_labels

        instrument_state["instruments"] = next_instruments
        instrument_state["token_labels"] = dict(merged_labels)
        instrument_state["token_list"] = next_token_list
        instrument_state["instrument_meta"] = next_instrument_meta
        instrument_state["label_to_underlying"] = dict(next_label_to_underlying)
        dashboard_bus.set_instrument_meta(next_instrument_meta)
        if index_monitor is not None:
            index_monitor.update_instrument_meta(next_instrument_meta)
        refresh_summary = _refresh_strategy_instances_after_atm_update(
            strategies,
            next_instrument_meta,
            previous_instrument_meta=old_instrument_meta,
            reconcile_open_legs=(
                stability_flags.option_atm_refresh_reconcile_open_legs
            ),
        )
        if refresh_summary["open_legs_before"] > 0:
            log_event(
                logger,
                event_type="ATM_REFRESH_STRATEGY_STATE",
                message="Refreshed strategy ATM labels and option leg state.",
                level=logging.INFO,
                mode=stability_flags.option_atm_refresh_reconcile_open_legs,
                open_legs_before=refresh_summary["open_legs_before"],
                open_legs_after=refresh_summary["open_legs_after"],
                reconciled=refresh_summary["reconciled"],
                cleared=refresh_summary["cleared"],
            )
        risk_manager.instrument_meta = next_instrument_meta
        removed_payload, unsub_ok = runner.swap_subscriptions(
            next_token_list,
            old_token_list=old_token_list,
        )
        if unsub_ok and removed_payload:
            stale_tokens = {
                str(tok)
                for entry in removed_payload
                for tok in entry.get("tokens", [])
            }
            for tok in stale_tokens:
                token_labels_ref["map"].pop(str(tok), None)
                instrument_state["token_labels"].pop(str(tok), None)
        _prune_indicator_state_from_token_map()

    refresh_interval = int(os.getenv("ATM_REFRESH_INTERVAL_SECONDS", "300"))
    refresh_stop = stop_event
    stability_flags = load_stability_feature_flags()

    def _prune_indicator_state_from_token_map() -> None:
        active_labels = set(token_labels_ref["map"].values())
        _prune_indicator_state_for_active_labels(engine, active_labels)

    # Periodically refresh ATM labels and resubscribe tokens.
    def refresh_atm_loop():
        nonlocal jwt_token
        while not refresh_stop.is_set():
            # Wait with a timeout so shutdown does not have to wait for a full interval
            if refresh_stop.wait(refresh_interval):
                break
            try:
                (
                    new_instruments,
                    new_token_labels,
                    new_token_list,
                    new_instrument_meta,
                ) = build_instrument_universe(
                    jwt_token=jwt_token,
                    api_key=api_key,
                )
                new_underlying_name_to_label = {
                    meta.get("underlying"): label
                    for label, meta in new_instrument_meta.items()
                    if meta.get("kind") == "UNDERLYING"
                }

                def _new_underlying_label_for_label(lbl: str) -> str:
                    meta = new_instrument_meta.get(lbl, {})
                    if meta.get("kind") == "UNDERLYING":
                        return lbl
                    underlying_name = meta.get("underlying")
                    return new_underlying_name_to_label.get(underlying_name, lbl)

                (
                    new_instruments,
                    new_token_labels,
                    new_token_list,
                    new_instrument_meta,
                    _,
                ) = _filter_stream_universe_to_enabled_labels(
                    new_instruments,
                    new_token_labels,
                    new_token_list,
                    new_instrument_meta,
                    instrument_controller=instrument_controller,
                    underlying_label_for_label=_new_underlying_label_for_label,
                )
                # Enforce "always stream executed tokens" on top of the fresh universe
                new_token_list = executed_tokens_tracker.extend_token_list(
                    new_token_list
                )
                next_token_labels = _merge_executed_token_labels(
                    new_token_labels,
                    new_token_list,
                )
                current_labels = set(token_labels_ref["map"].keys())
                next_labels = set(next_token_labels.keys())
                if next_labels == current_labels:
                    event_queue.submit_and_wait(
                        lambda next_token_labels=dict(next_token_labels): _apply_token_label_refresh(
                            next_token_labels
                        ),
                        description="atm_refresh_labels",
                        timeout=max(30.0, float(refresh_interval)),
                    )
                    continue
                
                # CHECK: Only resubscribe if WebSocket is healthy
                # This prevents the race condition where resubscribe forces
                # a reconnect that breaks the data stream
                if runner.state.status != "connected":
                    logger.warning(
                        "ATM refresh detected changes but WebSocket is not healthy (status=%s). "
                        "Deferring resubscribe to next cycle...",
                        runner.state.status
                    )
                    continue
                
                event_queue.submit_and_wait(
                    lambda new_instruments=list(new_instruments),
                    next_token_labels=dict(next_token_labels),
                    new_token_list=list(new_token_list),
                    new_instrument_meta=dict(new_instrument_meta),
                    next_label_to_underlying={
                        lbl: _new_underlying_label_for_label(lbl)
                        for lbl in new_instrument_meta
                    }: _apply_atm_refresh_changes(
                        next_instruments=new_instruments,
                        next_token_labels=next_token_labels,
                        next_token_list=new_token_list,
                        next_instrument_meta=new_instrument_meta,
                        next_label_to_underlying=next_label_to_underlying,
                    ),
                    description="atm_refresh_apply",
                    timeout=max(30.0, float(refresh_interval)),
                )
            except Exception as exc:
                err_str = str(exc)
                is_auth_error = (
                    "AG8001" in err_str
                    or "invalid token" in err_str.lower()
                    or "unauthorized" in err_str.lower()
                    or "token expired" in err_str.lower()
                )
                if is_auth_error:
                    logger.warning(
                        "ATM refresh: JWT token appears expired (%s); re-logging in for fresh token",
                        err_str[:120],
                    )
                    try:
                        fresh = angel_login_and_get_tokens()
                        jwt_token = fresh["jwtToken"]
                        logger.info("ATM refresh: re-login successful; will retry on next cycle")
                    except Exception as login_exc:
                        logger.error("ATM refresh: re-login failed: %s", login_exc)
                else:
                    logger.warning("ATM refresh failed: %s", exc)

    # Periodically sync broker positions into the risk manager.
    def _lookup_recent_entry_submission(
        *,
        broker_account_id: str,
        label: str,
        symbol: str,
        side: str,
    ):
        try:
            from app.hub.runtime import get_hub_runtime

            runtime = get_hub_runtime()
            lifecycle = getattr(runtime, "order_lifecycle", None)
            if lifecycle is None:
                return None
            return lifecycle.find_recent_entry_submission(
                broker_account_id=broker_account_id,
                label=label,
                symbol=symbol,
                side=side,
            )
        except Exception:
            return None

    def position_sync_loop():
        if base_position_sync_interval <= 0:
            return
        sync_interval_seconds = float(base_position_sync_interval)
        while not stop_event.is_set():
            if stop_event.wait(sync_interval_seconds):
                break
            try:
                try:
                    if risk_manager.current_trade_mode.upper() == "LIVE":
                        risk_manager._reconcile_pending_orders()  # type: ignore[attr-defined]
                except Exception:
                    pass
                # The primary notification path is the position_flat_registry singleton,
                # which strategies subscribe to directly at init time and is immune to
                # routing_table API changes. The legacy routing-table callback below is
                # kept for backward compatibility with any future strategies that do not
                # subscribe to the registry.
                def _make_pos_sync_flat_callback():
                    def _cb(label: str, reason: str) -> None:
                        # position_flat_registry.notify() is already called from
                        # position_sync.py directly; this legacy path catches any
                        # strategy that subscribed to routing_table but not registry.
                        try:
                            from app.hub.runtime import get_hub_runtime
                            rt = get_hub_runtime()
                            if rt is None:
                                return
                            rt_table = getattr(rt, "routing_table", None)
                            if rt_table is None:
                                return
                            for _strategy in getattr(rt_table, "get_all_active_strategies", lambda: [])():
                                # Skip strategies already covered by registry
                                from app.core.position_flat_registry import position_flat_registry as _reg
                                if _strategy in _reg._subscribers:
                                    continue
                                _fn = getattr(_strategy, "on_position_flat_by_sync", None)
                                if callable(_fn):
                                    try:
                                        _fn(label=label, reason=reason)
                                    except Exception as _e:
                                        logger.warning(
                                            "strategy.on_position_flat_by_sync (legacy) failed: %s", _e
                                        )
                        except Exception as _outer:
                            logger.warning("_pos_sync_flat_callback error: %s", _outer)
                    return _cb

                res = sync_positions_with_broker(
                    order_client=order_client,
                    risk_manager=risk_manager,
                    instrument_meta=instrument_state["instrument_meta"],
                    ltp_lookup=last_prices,
                    broker_account_id=position_sync_broker_account_id,
                    entry_submission_lookup=_lookup_recent_entry_submission,
                    strategy_flat_callback=_make_pos_sync_flat_callback(),
                )
                try:
                    risk_manager.evaluate_account_loss(
                        source="position_sync",
                        force=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "[RISK] Account loss evaluation failed after position sync: %s",
                        exc,
                    )
                if res.added or res.closed or res.updated:
                    logger.info("[POS_SYNC] %s", res.summary())
                if res.blocked:
                    retry_after = float(res.retry_after_seconds or 0.0)
                    next_interval = min(
                        float(position_sync_max_interval),
                        max(
                            float(base_position_sync_interval),
                            sync_interval_seconds * 2.0,
                            retry_after,
                            float(position_sync_blocked_min_interval),
                        ),
                    )
                    if next_interval > sync_interval_seconds:
                        logger.warning(
                            "[POS_SYNC] Backing off sync interval to %.1fs (reason=%s retry_after=%s cached=%s)",
                            next_interval,
                            res.blocked_reason or "blocked",
                            f"{retry_after:.1f}s" if retry_after > 0 else "-",
                            res.used_cached_snapshot,
                        )
                    sync_interval_seconds = next_interval
                elif res.errors:
                    next_interval = min(
                        float(position_sync_max_interval),
                        max(
                            float(base_position_sync_interval),
                            sync_interval_seconds * 1.5,
                        ),
                    )
                    if next_interval > sync_interval_seconds:
                        logger.warning(
                            "[POS_SYNC] Increasing sync interval to %.1fs after fetch errors=%d",
                            next_interval,
                            len(res.errors),
                        )
                    sync_interval_seconds = next_interval
                elif sync_interval_seconds != float(base_position_sync_interval):
                    logger.info(
                        "[POS_SYNC] Sync recovered; restoring interval to %ss",
                        base_position_sync_interval,
                    )
                    sync_interval_seconds = float(base_position_sync_interval)
            except Exception as exc:
                logger.warning("Position sync failed: %s", exc)

    # Run the websocket client in a background thread.
    def run_socket():
        try:
            runner.start()
        except Exception as exc:
            logger.exception("WebSocket runner stopped with error: %s", exc)
        finally:
            stop_event.set()

    def lifecycle_loop_body() -> None:
        _set_tick_flow_metrics(
            tick_metrics,
            event_queue=event_queue,
            tick_dispatcher=tick_dispatcher,
        )
        signal_metrics.maybe_log_summary(
            logger,
            signal_summary_interval_seconds,
        )

    _run_stream_lifecycle(
        stop_event=stop_event,
        refresh_stop=refresh_stop,
        start_health_server=start_health_server,
        refresh_atm_loop=refresh_atm_loop,
        position_sync_loop=position_sync_loop,
        position_sync_interval=base_position_sync_interval,
        run_socket=run_socket,
        runner=runner,
        persister=persister,
        loop_body=lifecycle_loop_body,
        shutdown_callback=event_queue.close,
    )


if __name__ == "__main__":
    stream_multi_instruments()
