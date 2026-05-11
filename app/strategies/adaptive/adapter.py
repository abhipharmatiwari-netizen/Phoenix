from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

from app.config.boot_config import StrategyValueResolver

from .dynamic_policy import (
    DynamicPolicyConfig,
    DynamicPolicyEngine,
    PositionPolicyState,
    parse_dynamic_policy_config,
)
from .market_context import MarketContext, MarketContextBuilder
from .regime import Regime, RegimeClassifier, RegimeThresholds

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverrideRule:
    value_type: type[Any]
    minimum: Optional[float] = None
    maximum: Optional[float] = None


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _parse_float(value: Any, *, default: float) -> float:
    try:
        out = float(value)
    except Exception:
        out = float(default)
    if not math.isfinite(out):
        return float(default)
    return float(out)


def _parse_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


class AdaptivePolicyAdapter:
    """
    Shared dynamic-policy runtime adapter for strategy-by-strategy enablement.

    The adapter provides:
    - deterministic market context + regime classification
    - policy overlay via DynamicPolicyEngine
    - strategy-specific override keys with per-key coercion/clamps
    - optional freeze-on-entry behavior for strategy-specific keys
    """

    def __init__(
        self,
        *,
        strategy_name: str,
        env_prefix: str,
        cfg: StrategyValueResolver,
        dynamic_policy_raw: Optional[Mapping[str, Any]],
        base_params: Mapping[str, Any],
        extra_rules: Optional[Mapping[str, OverrideRule]] = None,
        extra_entry_freeze_keys: Optional[Iterable[str]] = None,
        extra_runtime_updatable_keys: Optional[Iterable[str]] = None,
        context_builder_kwargs: Optional[Mapping[str, Any]] = None,
        dynamic_enabled_env_key: Optional[str] = None,
        dynamic_policy_id_env_key: Optional[str] = None,
    ) -> None:
        self.strategy_name = str(strategy_name)
        self.env_prefix = str(env_prefix)
        self._cfg = cfg
        self._extra_rules: Dict[str, OverrideRule] = dict(extra_rules or {})
        self._extra_entry_freeze_keys = set(extra_entry_freeze_keys or [])
        self._extra_runtime_updatable_keys = set(extra_runtime_updatable_keys or [])
        self._base_params: Dict[str, Any] = dict(base_params)
        self._effective_params: Dict[str, Any] = dict(base_params)
        self._frozen_core: Dict[str, Any] = {}
        self._frozen_extra: Dict[str, Any] = {}
        self._current_regime: Regime = Regime.NO_TRADE
        self._last_context: Optional[MarketContext] = None
        self._selected_profile: Dict[str, Any] = {}

        self._policy_cfg: DynamicPolicyConfig = parse_dynamic_policy_config(
            dynamic_policy_raw or {},
            strict=False,
        )
        self._dynamic_enabled = bool(self._policy_cfg.enabled)
        self._dynamic_policy_id = str(self._policy_cfg.policy_id or "").strip()

        if dynamic_enabled_env_key:
            raw_enabled = self._cfg.get(dynamic_enabled_env_key, None)
            if raw_enabled not in (None, ""):
                self._dynamic_enabled = _parse_bool(
                    raw_enabled,
                    default=self._dynamic_enabled,
                )
        if dynamic_policy_id_env_key:
            raw_policy_id = self._cfg.get(dynamic_policy_id_env_key, None)
            if raw_policy_id not in (None, ""):
                self._dynamic_policy_id = str(raw_policy_id).strip()

        if self._dynamic_enabled and not self._dynamic_policy_id:
            logger.warning(
                "[%s] %s dynamic policy enabled without policy_id; disabling.",
                self.env_prefix,
                self.strategy_name,
            )
            self._dynamic_enabled = False

        self._context_builder: Optional[MarketContextBuilder] = None
        self._regime_classifier: Optional[RegimeClassifier] = None
        self._policy_engine: Optional[DynamicPolicyEngine] = None
        if self._dynamic_enabled:
            builder_kwargs = dict(context_builder_kwargs or {})
            self._context_builder = MarketContextBuilder(**builder_kwargs)
            thr = self._policy_cfg.thresholds
            thresholds = RegimeThresholds(
                adx_trend=thr.adx_trend,
                di_spread_trend=thr.di_spread_trend,
                ema_slope_trend_min=thr.ema_slope_trend_min,
                atr_norm_high=thr.atr_norm_high,
                atr_norm_secondary=thr.atr_norm_secondary,
                gap_ratio_spike=thr.gap_ratio_spike,
                atr_norm_low=thr.atr_norm_low,
                adx_normal_low=thr.adx_normal_low,
                adx_normal_high=thr.adx_normal_high,
                chop_adx_max=thr.chop_adx_max,
                chop_di_spread_max=thr.chop_di_spread_max,
                use_chop_index=thr.use_chop_index,
                chop_index_high=thr.chop_index_high,
                hold_bars=thr.hold_bars,
                min_regime_hold_minutes=thr.min_regime_hold_minutes,
            )
            self._regime_classifier = RegimeClassifier(thresholds=thresholds)
            self._policy_engine = DynamicPolicyEngine(config=self._policy_cfg)

    @property
    def enabled(self) -> bool:
        return bool(self._dynamic_enabled)

    @property
    def regime(self) -> Regime:
        return self._current_regime

    @property
    def policy_config(self) -> DynamicPolicyConfig:
        return self._policy_cfg

    @property
    def last_context(self) -> Optional[MarketContext]:
        return self._last_context

    def effective_params(self) -> dict[str, Any]:
        return dict(self._effective_params)

    def selected_profile(self) -> dict[str, Any]:
        return dict(self._selected_profile)

    def set_base_params(self, params: Mapping[str, Any]) -> None:
        self._base_params = dict(params)

    def refresh(
        self,
        *,
        candle: Any,
        indicators: Mapping[str, Any],
        position_open: bool,
    ) -> dict[str, Any]:
        effective = dict(self._base_params)
        if not self._dynamic_enabled:
            self._last_context = None
            self._selected_profile = {}
            self._effective_params = effective
            return dict(self._effective_params)

        if (
            self._context_builder is None
            or self._regime_classifier is None
            or self._policy_engine is None
        ):
            self._last_context = None
            self._selected_profile = {}
            self._effective_params = effective
            return dict(self._effective_params)

        context = self._context_builder.build(
            candle=candle,
            indicators=dict(indicators),
        )
        self._last_context = context
        self._current_regime = self._regime_classifier.update(context)
        effective = self._policy_engine.apply(
            base_params=effective,
            regime=self._current_regime,
            context=context,
            state=PositionPolicyState(
                position_open=bool(position_open),
                entry_frozen_params=dict(self._frozen_core),
                entry_ts=getattr(candle, "start_ts", None),
            ),
        )

        profile = self._policy_engine._profile_for_regime(self._current_regime)
        self._selected_profile = dict(profile)
        extra_overrides = self._sanitize_extra_overrides(profile)
        effective.update(extra_overrides)

        if self._policy_cfg.freeze_on_entry and position_open:
            runtime_updatable = set(self._policy_cfg.allow_runtime_updates_for).union(
                self._extra_runtime_updatable_keys
            )
            for key, value in self._frozen_extra.items():
                if key in runtime_updatable:
                    continue
                effective[key] = value

        self._effective_params = dict(effective)
        return dict(self._effective_params)

    def mark_entry_committed(self) -> None:
        if not self._dynamic_enabled:
            return
        if self._policy_engine is None:
            return
        self._frozen_core = self._policy_engine.snapshot_entry_params(self._effective_params)
        self._frozen_extra = {
            key: self._effective_params[key]
            for key in self._extra_entry_freeze_keys
            if key in self._effective_params
        }

    def mark_position_closed(self) -> None:
        self._frozen_core = {}
        self._frozen_extra = {}

    def reset_runtime(self, *, clear_entry_state: bool = True) -> None:
        if self._context_builder is not None:
            self._context_builder.reset()
        if self._regime_classifier is not None:
            self._regime_classifier.reset()
        self._current_regime = Regime.NO_TRADE
        self._last_context = None
        self._selected_profile = {}
        self._effective_params = dict(self._base_params)
        if clear_entry_state:
            self._frozen_core = {}
            self._frozen_extra = {}

    def get_bool(self, key: str, default: bool = False) -> bool:
        return _parse_bool(self._effective_params.get(key, default), default=default)

    def get_int(self, key: str, default: int = 0, *, minimum: int = 0) -> int:
        out = _parse_int(self._effective_params.get(key, default), default=default)
        return max(int(minimum), int(out))

    def get_float(self, key: str, default: float = 0.0, *, minimum: float = 0.0) -> float:
        out = _parse_float(self._effective_params.get(key, default), default=default)
        return max(float(minimum), float(out))

    def get_optional_float(self, key: str, default: Optional[float] = None) -> Optional[float]:
        raw = self._effective_params.get(key, default)
        if raw is None:
            return None
        out = _parse_float(raw, default=(float(default) if default is not None else 0.0))
        return float(out)

    def _sanitize_extra_overrides(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, rule in self._extra_rules.items():
            if key not in profile:
                continue
            raw = profile.get(key)
            if rule.value_type is bool:
                out[key] = _parse_bool(raw, default=False)
                continue
            if rule.value_type is int:
                value = float(_parse_int(raw, default=0))
            elif rule.value_type is float:
                value = _parse_float(raw, default=0.0)
            else:
                continue
            if rule.minimum is not None:
                value = max(float(rule.minimum), float(value))
            if rule.maximum is not None:
                value = min(float(rule.maximum), float(value))
            out[key] = int(value) if rule.value_type is int else float(value)
        return out


__all__ = [
    "AdaptivePolicyAdapter",
    "OverrideRule",
]
