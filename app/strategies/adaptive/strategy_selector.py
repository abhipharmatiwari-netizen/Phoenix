from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from app.strategies.validators import validate_strategy_list

from .regime import Regime

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_utc(ts: Optional[datetime]) -> datetime:
    if not isinstance(ts, datetime):
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _regime_from_name(name: Any) -> Optional[Regime]:
    text = str(name or "").strip().upper()
    for regime in Regime:
        if regime.value == text:
            return regime
    return None


def _underlying_aliases(underlying: str) -> tuple[str, ...]:
    value = str(underlying or "").strip().upper()
    if not value:
        return ("",)
    aliases = [value]
    for suffix in ("_IDX", "_FUT"):
        if value.endswith(suffix):
            base = value[: -len(suffix)].strip("_")
            if base:
                aliases.append(base)
    return tuple(dict.fromkeys(aliases))


def _ist_date(dt: datetime) -> date:
    return _to_utc(dt).astimezone(_IST).date()


def _validated_strategy_names(
    values: Sequence[str],
    *,
    context: str,
) -> tuple[str, ...]:
    return tuple(validate_strategy_list([str(value) for value in values], context=context))


@dataclass(frozen=True)
class StrategySelectorConfig:
    enabled: bool = False
    max_active_per_underlying: int = 1
    min_hold_seconds: float = 300.0
    mapping: dict[str, dict[Regime, tuple[str, ...]]] = field(default_factory=dict)
    # Authority rule (EMA20-5): when True, ema20_strategy bypasses selector gating
    # and is governed exclusively by its own dynamic policy. This prevents the selector's
    # NO_TRADE→[] mapping from silently suppressing EMA20 when EMA20's NO_TRADE profile
    # has disable_entries=false. Other strategies are unaffected.
    ema20_is_authoritative: bool = False

    @classmethod
    def from_raw(
        cls,
        raw: Any,
        *,
        enabled_override: Optional[bool] = None,
        max_active_override: Optional[int] = None,
        min_hold_seconds_override: Optional[float] = None,
    ) -> "StrategySelectorConfig":
        payload = raw if isinstance(raw, Mapping) else {}

        enabled = _parse_bool(payload.get("enabled"), default=False)
        if enabled_override is not None:
            enabled = bool(enabled_override)

        max_active = max(
            1,
            _parse_int(
                payload.get("max_active_per_underlying"),
                default=1,
            ),
        )
        if max_active_override is not None:
            max_active = max(1, int(max_active_override))

        min_hold_seconds = max(
            0.0,
            _parse_float(payload.get("min_hold_seconds"), default=300.0),
        )
        if min_hold_seconds_override is not None:
            min_hold_seconds = max(0.0, float(min_hold_seconds_override))

        mapping: dict[str, dict[Regime, tuple[str, ...]]] = {}
        raw_mapping = payload.get("mapping", {})
        if isinstance(raw_mapping, Mapping):
            for raw_underlying, raw_regimes in raw_mapping.items():
                if not isinstance(raw_regimes, Mapping):
                    continue
                underlying = str(raw_underlying or "").strip().upper()
                if not underlying:
                    continue
                regime_map: dict[Regime, tuple[str, ...]] = {}
                for raw_regime_name, raw_strategies in raw_regimes.items():
                    regime = _regime_from_name(raw_regime_name)
                    if regime is None:
                        continue
                    if not isinstance(raw_strategies, (list, tuple, set)):
                        continue
                    canonical = _validated_strategy_names(
                        [str(value) for value in raw_strategies],
                        context=f"strategy_selector.mapping.{underlying}.{regime.value}",
                    )
                    regime_map[regime] = canonical
                mapping[underlying] = regime_map

        ema20_is_authoritative = _parse_bool(
            payload.get("ema20_is_authoritative"), default=False
        )

        return cls(
            enabled=enabled,
            max_active_per_underlying=max_active,
            min_hold_seconds=min_hold_seconds,
            mapping=mapping,
            ema20_is_authoritative=ema20_is_authoritative,
        )


@dataclass(frozen=True)
class StrategySelectionDecision:
    underlying: str
    regime: Regime
    selected_strategies: tuple[str, ...]
    reason: str
    changed: bool
    hold_until: Optional[str]
    updated_at: str


@dataclass
class _SelectionState:
    selected: tuple[str, ...]
    regime: Regime
    reason: str
    switched_at: datetime
    hold_until: Optional[datetime]
    updated_at: datetime


class StrategySelector:
    def __init__(self, config: StrategySelectorConfig) -> None:
        self.config = config
        self._state: dict[str, _SelectionState] = {}
        self._stale_invalidated: list[str] = []

    def select(
        self,
        *,
        underlying: str,
        regime: Regime,
        allowed_strategies: Sequence[str],
        now: Optional[datetime] = None,
    ) -> StrategySelectionDecision:
        now_utc = _to_utc(now)
        underlying_key = str(underlying or "").strip().upper()
        allowed = _validated_strategy_names(
            [str(name) for name in (allowed_strategies or [])],
            context=f"strategy_selector.allowed.{underlying_key}",
        )
        desired, desired_reason = self._desired_selection(
            underlying=underlying_key,
            regime=regime,
            allowed=allowed,
        )

        state = self._state.get(underlying_key)
        if state is not None and _ist_date(state.switched_at) < _ist_date(now_utc):
            logger.info(
                "selector.stale_state_detected underlying=%s switched_at=%s today_ist=%s; "
                "invalidating prior-day selection",
                underlying_key,
                state.switched_at.isoformat(),
                _ist_date(now_utc).isoformat(),
            )
            del self._state[underlying_key]
            self._stale_invalidated.append(underlying_key)
            state = None

        if state is None:
            new_state = self._set_state(
                underlying=underlying_key,
                selected=desired,
                regime=regime,
                reason=desired_reason,
                now=now_utc,
            )
            return self._decision(underlying_key, new_state, changed=True)

        force_switch = (
            regime == Regime.NO_TRADE
            or not self._selection_is_allowed(state.selected, allowed)
        )
        hold_active = bool(state.hold_until and now_utc < state.hold_until)

        if (
            not force_switch
            and hold_active
            and desired != state.selected
        ):
            state.regime = regime
            state.reason = f"hold_active({state.hold_until.isoformat()})"
            state.updated_at = now_utc
            return self._decision(underlying_key, state, changed=False)

        changed = (state.selected != desired) or (state.regime != regime)
        new_state = self._set_state(
            underlying=underlying_key,
            selected=desired,
            regime=regime,
            reason=desired_reason,
            now=now_utc,
        )
        return self._decision(underlying_key, new_state, changed=changed)

    def is_strategy_active(self, *, underlying: str, strategy_name: str) -> bool:
        underlying_key = str(underlying or "").strip().upper()
        state = self._state.get(underlying_key)
        if state is None:
            return False
        canonical = _validated_strategy_names(
            [str(strategy_name or "")],
            context=f"strategy_selector.active.{underlying_key}",
        )
        return canonical[0] in set(state.selected)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for underlying, state in self._state.items():
            out[underlying] = {
                "underlying": underlying,
                "regime": state.regime.value,
                "selected_strategies": list(state.selected),
                "reason": state.reason,
                "hold_until": state.hold_until.isoformat() if state.hold_until else None,
                "updated_at": state.updated_at.isoformat(),
                "switched_at": state.switched_at.isoformat(),
            }
        return out

    def staleness_summary(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Return a staleness check summary for inclusion in the startup snapshot."""
        now_utc = _to_utc(now)
        today_ist = _ist_date(now_utc)
        stale = [
            k for k, v in self._state.items()
            if _ist_date(v.switched_at) < today_ist
        ]
        if stale:
            return {
                "status": f"stale_count={len(stale)}",
                "stale_underlyings": stale,
            }
        return {"status": "passed"}

    def _set_state(
        self,
        *,
        underlying: str,
        selected: tuple[str, ...],
        regime: Regime,
        reason: str,
        now: datetime,
    ) -> _SelectionState:
        hold_until = None
        if self.config.min_hold_seconds > 0:
            hold_until = now + timedelta(seconds=float(self.config.min_hold_seconds))
        state = _SelectionState(
            selected=selected,
            regime=regime,
            reason=reason,
            switched_at=now,
            hold_until=hold_until,
            updated_at=now,
        )
        self._state[underlying] = state
        return state

    def _decision(
        self,
        underlying: str,
        state: _SelectionState,
        *,
        changed: bool,
    ) -> StrategySelectionDecision:
        return StrategySelectionDecision(
            underlying=underlying,
            regime=state.regime,
            selected_strategies=state.selected,
            reason=state.reason,
            changed=changed,
            hold_until=state.hold_until.isoformat() if state.hold_until else None,
            updated_at=state.updated_at.isoformat(),
        )

    def _selection_is_allowed(
        self,
        selected: tuple[str, ...],
        allowed: tuple[str, ...],
    ) -> bool:
        if not selected:
            return True
        allowed_set = set(allowed)
        return all(strategy in allowed_set for strategy in selected)

    def _desired_selection(
        self,
        *,
        underlying: str,
        regime: Regime,
        allowed: tuple[str, ...],
    ) -> tuple[tuple[str, ...], str]:
        mapped = self._mapped_selection(underlying=underlying, regime=regime)
        if regime == Regime.NO_TRADE:
            # An explicit non-empty NO_TRADE mapping is honoured so that strategies
            # whose own dynamic policy governs entry decisions (e.g. exclusive_nifty_ce_buy)
            # still receive bar dispatch. An absent or empty mapping clears the selection.
            if mapped:
                selection = tuple(s for s in mapped if s in set(allowed))
                if selection:
                    return selection[: self.config.max_active_per_underlying], "mapping"
            return (), "no_trade_regime"

        if mapped is not None:
            selection = tuple(
                strategy
                for strategy in mapped
                if strategy in set(allowed)
            )
            if selection:
                return selection[: self.config.max_active_per_underlying], "mapping"
            return (), "mapping_empty_after_allowed_filter"

        if not allowed:
            return (), "allowed_empty"
        return (
            tuple(allowed[: self.config.max_active_per_underlying]),
            "allowed_fallback",
        )

    def _mapped_selection(
        self,
        *,
        underlying: str,
        regime: Regime,
    ) -> Optional[tuple[str, ...]]:
        for alias in _underlying_aliases(underlying):
            regime_map = self.config.mapping.get(alias)
            if not regime_map:
                continue
            selected = regime_map.get(regime)
            if selected is not None:
                return selected
            if regime in {Regime.TRENDING_UP, Regime.TRENDING_DOWN}:
                selected = regime_map.get(Regime.TRENDING)
                if selected is not None:
                    return selected
        return None


__all__ = [
    "StrategySelector",
    "StrategySelectorConfig",
    "StrategySelectionDecision",
]
