from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from .market_context import MarketContext
from .regime import Regime, RegimeThresholds

logger = logging.getLogger(__name__)


ALLOWED_OVERRIDE_KEYS = {
    "sl_pct",
    "tp_pct",
    "trail_trigger_pct",
    "trail_buffer_pct",
    "ema_period",
    "use_adx_filter",
    "min_adx",
    "min_di_spread",
    "require_rsi_falling",
    "first_entry_delay_minutes",
    "disable_entries",
    "qty_mult",
    "min_atr",
    "sl_atr",
    "tp_atr",
    "rsi_min",
    "rsi_max",
}

ENTRY_CRITICAL_KEYS = {
    "ema_period",
    "sl_pct",
    "tp_pct",
    "qty_mult",
    "use_adx_filter",
    "min_adx",
    "min_di_spread",
    "require_rsi_falling",
    "min_atr",
    "disable_entries",
    "first_entry_delay_minutes",
}

_KEY_SPEC: dict[str, tuple[type[Any], Optional[float], Optional[float]]] = {
    "sl_pct": (float, 0.05, 0.90),
    "tp_pct": (float, 0.05, 0.95),
    "trail_trigger_pct": (float, 0.0, 1.0),
    "trail_buffer_pct": (float, 0.0, 1.0),
    "ema_period": (int, 2.0, 200.0),
    "use_adx_filter": (bool, None, None),
    "min_adx": (float, 0.0, 100.0),
    "min_di_spread": (float, 0.0, 100.0),
    "require_rsi_falling": (bool, None, None),
    "first_entry_delay_minutes": (int, 0.0, 180.0),
    "disable_entries": (bool, None, None),
    "qty_mult": (float, 0.1, 2.0),
    "min_atr": (float, 0.0, None),
    "sl_atr": (float, 0.1, 20.0),
    "tp_atr": (float, 0.1, 20.0),
    "rsi_min": (float, 0.0, 100.0),
    "rsi_max": (float, 0.0, 100.0),
}


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def deep_merge_dict(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge_dict(out[key], value)
        else:
            out[key] = value
    return out


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_policy_hash(policy_payload: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(policy_payload).encode("utf-8")).hexdigest()
    return digest[:8]


@dataclass(frozen=True)
class DynamicPolicyConfig:
    enabled: bool = False
    policy_id: str = ""
    freeze_on_entry: bool = True
    allow_runtime_updates_for: tuple[str, ...] = ("trail_trigger_pct", "trail_buffer_pct")
    update_policy_only_when_flat: bool = False
    hold_bars: int = 3
    min_regime_hold_minutes: int = 0
    thresholds: RegimeThresholds = field(default_factory=RegimeThresholds)
    profiles: dict[Regime, dict[str, Any]] = field(default_factory=dict)
    explicit_profiles: frozenset[Regime] = field(default_factory=frozenset)
    policy_hash: str = ""

    def is_runtime_updatable(self, key: str) -> bool:
        return key in set(self.allow_runtime_updates_for)

    def to_public_dict(self) -> dict[str, Any]:
        profiles = {reg.value: dict(values) for reg, values in self.profiles.items()}
        return {
            "enabled": self.enabled,
            "policy_id": self.policy_id,
            "policy_hash": self.policy_hash,
            "freeze_on_entry": self.freeze_on_entry,
            "allow_runtime_updates_for": list(self.allow_runtime_updates_for),
            "update_policy_only_when_flat": self.update_policy_only_when_flat,
            "hold_bars": self.hold_bars,
            "min_regime_hold_minutes": self.min_regime_hold_minutes,
            "thresholds": {
                "adx_trend": self.thresholds.adx_trend,
                "di_spread_trend": self.thresholds.di_spread_trend,
                "ema_slope_trend_min": self.thresholds.ema_slope_trend_min,
                "atr_norm_high": self.thresholds.atr_norm_high,
                "atr_norm_secondary": self.thresholds.atr_norm_secondary,
                "gap_ratio_spike": self.thresholds.gap_ratio_spike,
                "atr_norm_low": self.thresholds.atr_norm_low,
                "adx_normal_low": self.thresholds.adx_normal_low,
                "adx_normal_high": self.thresholds.adx_normal_high,
                "chop_adx_max": self.thresholds.chop_adx_max,
                "chop_di_spread_max": self.thresholds.chop_di_spread_max,
                "use_chop_index": self.thresholds.use_chop_index,
                "chop_index_high": self.thresholds.chop_index_high,
            },
            "profiles": profiles,
        }


@dataclass(frozen=True)
class PositionPolicyState:
    position_open: bool = False
    entry_frozen_params: Optional[Mapping[str, Any]] = None
    entry_ts: Optional[datetime] = None


def _to_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(out):
        return float(default)
    return out


def _coerce_number(value: Any, want_int: bool) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    if want_int:
        return float(int(out))
    return out


def _regime_from_name(name: str) -> Optional[Regime]:
    txt = str(name or "").strip().upper()
    for reg in Regime:
        if reg.value == txt:
            return reg
    return None


def _default_profiles() -> dict[Regime, dict[str, Any]]:
    return {
        Regime.TRENDING: {},
        Regime.TRENDING_UP: {},
        Regime.TRENDING_DOWN: {},
        Regime.NORMAL: {},
        Regime.CHOPPY: {},
        Regime.HIGH_VOL: {},
        Regime.NO_TRADE: {"disable_entries": True},
    }


def parse_dynamic_policy_config(
    raw_policy: Any,
    *,
    strict: bool = False,
) -> DynamicPolicyConfig:
    payload = raw_policy if isinstance(raw_policy, dict) else {}
    enabled = _to_bool(payload.get("enabled"), default=False)
    policy_id = str(payload.get("policy_id", "") or "").strip()
    if enabled and not policy_id and strict:
        raise ValueError("dynamic_policy.policy_id is required when dynamic_policy.enabled=true")

    freeze_on_entry = _to_bool(payload.get("freeze_on_entry"), default=True)
    allow_runtime_updates_for_raw = payload.get("allow_runtime_updates_for")
    runtime_updates: tuple[str, ...]
    if isinstance(allow_runtime_updates_for_raw, (list, tuple, set)):
        runtime_updates = tuple(str(x).strip() for x in allow_runtime_updates_for_raw if str(x).strip())
    else:
        runtime_updates = ("trail_trigger_pct", "trail_buffer_pct")
    update_policy_only_when_flat = _to_bool(payload.get("update_policy_only_when_flat"), default=False)

    hold_bars = max(1, _to_int(payload.get("hold_bars", 3) or 3, default=3))
    min_regime_hold_minutes = max(
        0,
        _to_int(payload.get("min_regime_hold_minutes", 0) or 0, default=0),
    )

    thr_raw = payload.get("thresholds", {})
    if not isinstance(thr_raw, dict):
        thr_raw = {}
    thresholds = RegimeThresholds(
        adx_trend=_to_float(thr_raw.get("adx_trend", 25.0), default=25.0),
        di_spread_trend=_to_float(thr_raw.get("di_spread_trend", 5.0), default=5.0),
        ema_slope_trend_min=_to_float(
            thr_raw.get("ema_slope_trend_min", 0.0),
            default=0.0,
        ),
        atr_norm_high=_to_float(thr_raw.get("atr_norm_high", 1.6), default=1.6),
        atr_norm_secondary=_to_float(
            thr_raw.get("atr_norm_secondary", 1.3),
            default=1.3,
        ),
        gap_ratio_spike=_to_float(
            thr_raw.get("gap_ratio_spike", 1.0),
            default=1.0,
        ),
        atr_norm_low=_to_float(thr_raw.get("atr_norm_low", 0.8), default=0.8),
        adx_normal_low=_to_float(
            thr_raw.get("adx_normal_low", 18.0),
            default=18.0,
        ),
        adx_normal_high=_to_float(
            thr_raw.get("adx_normal_high", 25.0),
            default=25.0,
        ),
        chop_adx_max=_to_float(thr_raw.get("chop_adx_max", 17.0), default=17.0),
        chop_di_spread_max=_to_float(
            thr_raw.get("chop_di_spread_max", 3.0),
            default=3.0,
        ),
        use_chop_index=_to_bool(thr_raw.get("use_chop_index", False), default=False),
        chop_index_high=_to_float(
            thr_raw.get("chop_index_high", 61.8),
            default=61.8,
        ),
        hold_bars=hold_bars,
        min_regime_hold_minutes=min_regime_hold_minutes,
    )

    profiles = _default_profiles()
    explicit_profiles: set[Regime] = set()
    raw_profiles = payload.get("profiles", {})
    if isinstance(raw_profiles, dict):
        for name, values in raw_profiles.items():
            regime = _regime_from_name(str(name))
            if regime is None or not isinstance(values, dict):
                continue
            explicit_profiles.add(regime)
            profiles[regime] = dict(values)

    policy_dict = {
        "enabled": enabled,
        "policy_id": policy_id,
        "freeze_on_entry": freeze_on_entry,
        "allow_runtime_updates_for": list(runtime_updates),
        "update_policy_only_when_flat": update_policy_only_when_flat,
        "hold_bars": hold_bars,
        "min_regime_hold_minutes": min_regime_hold_minutes,
        "thresholds": {
            "adx_trend": thresholds.adx_trend,
            "di_spread_trend": thresholds.di_spread_trend,
            "ema_slope_trend_min": thresholds.ema_slope_trend_min,
            "atr_norm_high": thresholds.atr_norm_high,
            "atr_norm_secondary": thresholds.atr_norm_secondary,
            "gap_ratio_spike": thresholds.gap_ratio_spike,
            "atr_norm_low": thresholds.atr_norm_low,
            "adx_normal_low": thresholds.adx_normal_low,
            "adx_normal_high": thresholds.adx_normal_high,
            "chop_adx_max": thresholds.chop_adx_max,
            "chop_di_spread_max": thresholds.chop_di_spread_max,
            "use_chop_index": thresholds.use_chop_index,
            "chop_index_high": thresholds.chop_index_high,
        },
        "profiles": {reg.value: values for reg, values in profiles.items()},
        "explicit_profiles": sorted(reg.value for reg in explicit_profiles),
    }
    return DynamicPolicyConfig(
        enabled=enabled,
        policy_id=policy_id,
        freeze_on_entry=freeze_on_entry,
        allow_runtime_updates_for=runtime_updates,
        update_policy_only_when_flat=update_policy_only_when_flat,
        hold_bars=hold_bars,
        min_regime_hold_minutes=min_regime_hold_minutes,
        thresholds=thresholds,
        profiles=profiles,
        explicit_profiles=frozenset(explicit_profiles),
        policy_hash=build_policy_hash(policy_dict),
    )


class DynamicPolicyEngine:
    def __init__(
        self,
        *,
        config: DynamicPolicyConfig,
        now_mono: Optional[Callable[[], float]] = None,
        warning_interval_seconds: float = 60.0,
    ) -> None:
        self.config = config
        self._now_mono = now_mono or time.monotonic
        self._warning_interval_seconds = max(1.0, float(warning_interval_seconds))
        self._last_warning_mono: dict[str, float] = {}
        self._unknown_key_counts: Counter[str] = Counter()
        self._clamp_counts: Counter[str] = Counter()
        self._apply_counts: Counter[str] = Counter()
        self._fallback_counts: Counter[str] = Counter()

    def unknown_key_count(self, key: str) -> int:
        return int(self._unknown_key_counts.get(key, 0))

    def unknown_key_counts(self) -> dict[str, int]:
        return dict(self._unknown_key_counts)

    def clamp_count(self, key: str) -> int:
        return int(self._clamp_counts.get(key, 0))

    def clamp_counts(self) -> dict[str, int]:
        return dict(self._clamp_counts)

    def policy_apply_count(self, regime: Regime) -> int:
        return int(self._apply_counts.get(regime.value, 0))

    def fallback_count(self, reason: str) -> int:
        return int(self._fallback_counts.get(reason, 0))

    def fallback_counts(self) -> dict[str, int]:
        return dict(self._fallback_counts)

    def snapshot_entry_params(self, effective_params: Mapping[str, Any]) -> dict[str, Any]:
        return {key: effective_params[key] for key in ENTRY_CRITICAL_KEYS if key in effective_params}

    def apply(
        self,
        *,
        base_params: Mapping[str, Any],
        regime: Regime,
        context: Optional[MarketContext],
        state: Optional[PositionPolicyState] = None,
    ) -> dict[str, Any]:
        effective: dict[str, Any] = dict(base_params)
        self._apply_counts[regime.value] += 1

        if not self.config.enabled:
            return effective
        if context is None:
            self._fallback_counts["missing_context"] += 1
            return effective
        if not context.validate():
            self._fallback_counts["invalid_context"] += 1
            return effective

        if state and state.position_open and self.config.update_policy_only_when_flat:
            if state.entry_frozen_params:
                for key, value in state.entry_frozen_params.items():
                    if key in effective:
                        effective[key] = value
            return effective

        profile = self._profile_for_regime(regime)
        sanitized = self._sanitize_overrides(profile)
        effective.update(sanitized)
        self._apply_computed_overrides(effective=effective, context=context)

        if self.config.freeze_on_entry and state and state.position_open:
            frozen = dict(state.entry_frozen_params or {})
            for key in ENTRY_CRITICAL_KEYS:
                if key in frozen and key not in set(self.config.allow_runtime_updates_for):
                    effective[key] = frozen[key]
        return effective

    def _profile_for_regime(self, regime: Regime) -> Mapping[str, Any]:
        if regime in self.config.explicit_profiles:
            return self.config.profiles.get(regime) or {}
        profile = self.config.profiles.get(regime)
        if profile:
            return profile
        if regime in {Regime.TRENDING_UP, Regime.TRENDING_DOWN}:
            return self.config.profiles.get(Regime.TRENDING) or {}
        return profile or {}

    def _apply_computed_overrides(
        self,
        *,
        effective: dict[str, Any],
        context: MarketContext,
    ) -> None:
        if "qty_mult" not in effective:
            return
        base_qty_mult = _coerce_number(effective.get("qty_mult"), want_int=False)
        if base_qty_mult is None:
            return
        atr_norm = context.atr_norm
        if atr_norm is None or not math.isfinite(atr_norm) or atr_norm <= 0.0:
            return
        # Deterministic monotonic down-scaling with volatility.
        scale = 1.0 / max(0.5, atr_norm)
        adjusted = float(base_qty_mult) * scale
        clamped = clamp(adjusted, 0.25, 1.5)
        if clamped != adjusted:
            self._clamp_counts["qty_mult"] += 1
        effective["qty_mult"] = clamped

    def _sanitize_overrides(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, raw_value in profile.items():
            if key not in ALLOWED_OVERRIDE_KEYS:
                self._unknown_key_counts[str(key)] += 1
                self._warn_limited(
                    key=f"unknown:{key}",
                    message="Dynamic policy ignored unknown key: %s",
                    arg=key,
                )
                continue

            spec = _KEY_SPEC.get(key)
            if spec is None:
                continue
            expected_type, minimum, maximum = spec

            if expected_type is bool:
                out[key] = _to_bool(raw_value, default=False)
                continue
            if expected_type is int:
                numeric = _coerce_number(raw_value, want_int=True)
                if numeric is None:
                    continue
                value = int(numeric)
                if minimum is not None and value < int(minimum):
                    value = int(minimum)
                    self._clamp_counts[key] += 1
                if maximum is not None and value > int(maximum):
                    value = int(maximum)
                    self._clamp_counts[key] += 1
                out[key] = value
                continue
            if expected_type is float:
                numeric = _coerce_number(raw_value, want_int=False)
                if numeric is None:
                    continue
                value = float(numeric)
                clamped = value
                if minimum is not None:
                    clamped = max(float(minimum), clamped)
                if maximum is not None:
                    clamped = min(float(maximum), clamped)
                if clamped != value:
                    self._clamp_counts[key] += 1
                out[key] = clamped
        return out

    def _warn_limited(self, *, key: str, message: str, arg: Any) -> None:
        now = self._now_mono()
        last = self._last_warning_mono.get(key, 0.0)
        if now - last < self._warning_interval_seconds:
            return
        self._last_warning_mono[key] = now
        logger.warning(message, arg)


__all__ = [
    "ALLOWED_OVERRIDE_KEYS",
    "ENTRY_CRITICAL_KEYS",
    "DynamicPolicyConfig",
    "PositionPolicyState",
    "DynamicPolicyEngine",
    "build_policy_hash",
    "clamp",
    "deep_merge_dict",
    "parse_dynamic_policy_config",
]
