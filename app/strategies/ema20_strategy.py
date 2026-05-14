from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from app.strategies.base import BaseStrategy
from app.core.position_flat_registry import position_flat_registry
from app.core.signal_metrics import get_labeled_metrics
from app.core.underlyings import canonical_underlying_key, underlying_lookup_candidates
from app.config.boot_config import StrategyValueResolver
from app.core.lot_size import lot_size_for_symbol_optional
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import EMA20_STRATEGY_ID
from app.strategies.naming import canonicalize_strategy_name
from app.strategies.restart_state import is_recovery_position_marker
from app.strategies.adaptive.dynamic_policy import (
    DynamicPolicyConfig,
    DynamicPolicyEngine,
    PositionPolicyState,
    parse_dynamic_policy_config,
)
from app.strategies.adaptive.market_context import MarketContextBuilder
from app.strategies.adaptive.regime import Regime, RegimeClassifier, RegimeThresholds
from app.config.settings import get_settings
from app.brokers.base import (
    OrderRequest,
    OrderPurpose,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# Lazy runtime accessor for route/state lookups (patched in tests).
def _get_hub_runtime() -> Any:
    from app.hub.runtime import get_hub_runtime

    return get_hub_runtime()


# EMA-based trend strategy for call shorts.
class Ema20Strategy(BaseStrategy):
    """
    Simple EMA trend strategy:
    - Reacts to the configured underlying's bar close (default 5m).
    - When close < EMA(period) and ATR (if configured) is sufficient, short nearest CE.
    - Routes all orders through strategy_bridge -> OrderRouter.
    """

    # Initialize strategy configuration and state.
    def __init__(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        *,
        env_prefix: str,
        underlying_label: str,
        product_type: str = "INTRADAY",
        signal_timeframe: int = 300,
        ema_period: int = 20,
        min_atr: Optional[float] = None,
        require_rsi_falling: Optional[bool] = None,
        use_adx_filter: bool = False,
        adx_period: int = 14,
        min_adx: float = 18.0,
        require_bearish_di: bool = True,
        min_di_spread: float = 0.0,
        ce_label_prefix: Optional[str] = None,
        sl_pct: float = 0.30,
        tp_pct: float = 0.30,
        trail_buffer_pct: float = 0.0,
        trail_trigger_pct: Optional[float] = None,
        first_entry_time: Optional[str] = None,
        square_off_time: Optional[str] = None,
        dynamic_policy: Optional[Dict[str, Any]] = None,
        # PHX#182: partial booking at first target. tp1_pct=None disables TP1.
        tp1_pct: Optional[float] = None,
        tp1_qty_pct: float = 0.0,
        # PHX#183: give-back guardrail. giveback_pct=None disables.
        giveback_pct: Optional[float] = None,
        giveback_arm_pct: Optional[float] = None,
        # PHX#184: time-decay accelerator. minutes=None disables.
        decay_tighten_minutes_before_eod: Optional[int] = None,
        decay_tp_multiplier: float = 1.0,
        decay_trail_buffer_multiplier: float = 1.0,
        config_resolver: Optional[StrategyValueResolver] = None,
        risk_manager: Optional[Any] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.env_prefix = env_prefix
        self.underlying_label = underlying_label
        self._underlying_key = canonical_underlying_key(
            self.underlying_label or self.env_prefix
        )
        self.product_type = product_type
        self.signal_timeframe = signal_timeframe
        self.ema_period = ema_period
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params={},
            env=dict(os.environ),
        )
        self.ce_label_prefix = ce_label_prefix or f"{self.env_prefix}ATM_CE"
        self.sl_pct = float(sl_pct)
        self.tp_pct = float(tp_pct)
        self.trail_buffer_pct = float(trail_buffer_pct or 0.0)
        self.trail_trigger_pct = (
            float(trail_trigger_pct)
            if trail_trigger_pct is not None
            else None
        )
        # PHX#182: TP1 partial booking. tp1_pct=None or tp1_qty_pct<=0 disables.
        self.tp1_pct: Optional[float] = (
            float(tp1_pct) if tp1_pct is not None and float(tp1_pct) > 0 else None
        )
        self.tp1_qty_pct: float = max(0.0, min(1.0, float(tp1_qty_pct or 0.0)))
        # PHX#183: give-back guardrail.
        self.giveback_pct: Optional[float] = (
            float(giveback_pct)
            if giveback_pct is not None and float(giveback_pct) > 0
            else None
        )
        self.giveback_arm_pct: Optional[float] = (
            float(giveback_arm_pct)
            if giveback_arm_pct is not None and float(giveback_arm_pct) > 0
            else None
        )
        # PHX#184: time-decay accelerator.
        self.decay_tighten_minutes_before_eod: Optional[int] = (
            int(decay_tighten_minutes_before_eod)
            if decay_tighten_minutes_before_eod is not None
            and int(decay_tighten_minutes_before_eod) > 0
            else None
        )
        self.decay_tp_multiplier: float = max(
            0.0, float(decay_tp_multiplier) if decay_tp_multiplier is not None else 1.0
        )
        self.decay_trail_buffer_multiplier: float = max(
            0.0,
            float(decay_trail_buffer_multiplier)
            if decay_trail_buffer_multiplier is not None
            else 1.0,
        )
        default_require_rsi = (
            bool(require_rsi_falling) if require_rsi_falling is not None else True
        )
        self.require_rsi_falling = self._cfg.get_bool(
            "EMA20_REQUIRE_RSI_FALLING",
            default=default_require_rsi,
        )
        self.use_adx_filter = self._cfg.get_bool(
            "EMA20_USE_ADX_FILTER",
            default=bool(use_adx_filter),
        )
        self.adx_period = max(
            2,
            int(self._cfg.get_int("EMA20_ADX_PERIOD", int(adx_period))),
        )
        self.min_adx = max(
            0.0,
            float(self._cfg.get_float("EMA20_MIN_ADX", float(min_adx))),
        )
        self.require_bearish_di = self._cfg.get_bool(
            "EMA20_REQUIRE_BEARISH_DI",
            default=bool(require_bearish_di),
        )
        self.min_di_spread = max(
            0.0,
            float(self._cfg.get_float("EMA20_MIN_DI_SPREAD", float(min_di_spread))),
        )

        self._env: Any = lambda key, default=None: str(  # noqa: E731
            self._cfg.get(key, default if default is not None else "") or ""
        )
        self.min_atr = self._parse_float(
            self._env("EMA20_MIN_ATR", min_atr if min_atr is not None else None)
        )
        self.first_entry_time = self._parse_time(
            self._env(
                "EMA20_FIRST_ENTRY_TIME",
                first_entry_time if first_entry_time is not None else None,
            )
        )
        self.square_off_time = self._parse_time(
            self._env(
                "EMA20_SQUARE_OFF_TIME",
                square_off_time if square_off_time is not None else None,
            )
        )
        self._dynamic_policy_cfg: DynamicPolicyConfig = parse_dynamic_policy_config(
            dynamic_policy or {},
            strict=False,
        )
        self._dynamic_policy_id = self._dynamic_policy_cfg.policy_id or None
        self._dynamic_enabled = bool(self._dynamic_policy_cfg.enabled)
        dynamic_enabled_override = self._env_raw("EMA20_DYNAMIC_POLICY_ENABLED")
        if dynamic_enabled_override is not None:
            self._dynamic_enabled = self._parse_bool_env(
                dynamic_enabled_override,
                default=self._dynamic_enabled,
            )
        dynamic_policy_id_override = self._env_raw("EMA20_DYNAMIC_POLICY_ID")
        if dynamic_policy_id_override is not None and str(dynamic_policy_id_override).strip():
            self._dynamic_policy_id = str(dynamic_policy_id_override).strip()
        if self._dynamic_enabled and not self._dynamic_policy_id:
            logger.warning(
                "[%s] EMA20 dynamic policy enabled but policy_id missing; disabling dynamic mode.",
                self.env_prefix,
            )
            self._dynamic_enabled = False

        self._context_builder: Optional[MarketContextBuilder] = None
        self._regime_classifier: Optional[RegimeClassifier] = None
        self._policy_engine: Optional[DynamicPolicyEngine] = None
        self._current_regime: Regime = Regime.NO_TRADE
        self._effective_params: Dict[str, Any] = {}
        self._frozen_params: Optional[Dict[str, Any]] = None
        self._last_policy_log_regime: Optional[Regime] = None
        self._last_policy_log_mono = 0.0
        self._policy_log_every_bars = 50
        self._policy_bars_seen = 0
        self._policy_unknown_snapshot: Dict[str, int] = {}
        self._policy_clamp_snapshot: Dict[str, int] = {}
        self._policy_fallback_snapshot: Dict[str, int] = {}
        self._metrics = get_labeled_metrics()
        self._tenant_label = self._env_raw("TENANT_ID") or "default"
        self._base_metric_labels = {
            "underlying": str(self.underlying_label),
            "tenant": str(self._tenant_label),
        }
        if self._dynamic_enabled:
            self._context_builder = MarketContextBuilder(ema_period=self.ema_period)
            thresholds = RegimeThresholds(
                adx_trend=self._dynamic_policy_cfg.thresholds.adx_trend,
                di_spread_trend=self._dynamic_policy_cfg.thresholds.di_spread_trend,
                ema_slope_trend_min=self._dynamic_policy_cfg.thresholds.ema_slope_trend_min,
                atr_norm_high=self._dynamic_policy_cfg.thresholds.atr_norm_high,
                atr_norm_secondary=self._dynamic_policy_cfg.thresholds.atr_norm_secondary,
                gap_ratio_spike=self._dynamic_policy_cfg.thresholds.gap_ratio_spike,
                atr_norm_low=self._dynamic_policy_cfg.thresholds.atr_norm_low,
                adx_normal_low=self._dynamic_policy_cfg.thresholds.adx_normal_low,
                adx_normal_high=self._dynamic_policy_cfg.thresholds.adx_normal_high,
                chop_adx_max=self._dynamic_policy_cfg.thresholds.chop_adx_max,
                chop_di_spread_max=self._dynamic_policy_cfg.thresholds.chop_di_spread_max,
                use_chop_index=self._dynamic_policy_cfg.thresholds.use_chop_index,
                chop_index_high=self._dynamic_policy_cfg.thresholds.chop_index_high,
                hold_bars=self._dynamic_policy_cfg.hold_bars,
                min_regime_hold_minutes=self._dynamic_policy_cfg.min_regime_hold_minutes,
            )
            self._regime_classifier = RegimeClassifier(thresholds=thresholds)
            self._policy_engine = DynamicPolicyEngine(config=self._dynamic_policy_cfg)

        settings = get_settings()
        if self.env_prefix == "NIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_nifty_options
        elif self.env_prefix == "BANKNIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_banknifty_options
        elif self.env_prefix == "FINNIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_finnifty_options
        elif self.env_prefix == "SENSEX_":
            self._use_hub_router_flag = settings.use_hub_router_for_sensex_options
        elif self.env_prefix == "MIDCPNIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_midcpnifty_options
        elif self.env_prefix == "NG_":
            self._use_hub_router_flag = settings.use_hub_router_for_ng_options
        else:
            self._use_hub_router_flag = False
        self._strategy_id = EMA20_STRATEGY_ID

        self.last_price: Dict[str, float] = {}
        self.ce_label = self._find_label_prefix(self.ce_label_prefix)
        self._last_fired_start_ts: Optional[datetime] = None
        self._debug_gate_listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._rsi_prev: Optional[float] = None
        self._rsi_prev_prev: Optional[float] = None
        self.risk_manager = risk_manager
        self.position: Optional[Ema20Position] = None
        self._managed_positions: Dict[str, Ema20Position] = {}
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        exit_retry_cfg = self._parse_float(
            self._env("EMA20_EXIT_RETRY_COOLDOWN_SECONDS", "5")
        )
        self._exit_retry_cooldown_seconds = (
            max(0.5, float(exit_retry_cfg))
            if exit_retry_cfg is not None
            else 5.0
        )
        # Issue #223: per-instrument entry cooloff. Defends against rapid
        # consecutive entries on the same option label (the 2026-05-08
        # NATURALGAS22MAY26265CE incident: two BUY entries 47 seconds apart
        # accumulated a 2-lot long that the exit engines could not cleanly
        # close). The existing `_last_fired_start_ts` guard blocks twice on
        # the same bar; this is a wall-clock backstop that is independent of
        # bar boundaries, position-state syncs, and aggregation quirks.
        # Default = one bar (signal_timeframe). Override per symbol via
        # `<PREFIX>EMA20_ENTRY_COOLOFF_SECONDS` (e.g. `NG_EMA20_ENTRY_COOLOFF_SECONDS=180`).
        # Set to 0 to disable (NOT recommended for LIVE).
        entry_cooloff_cfg = self._parse_float(
            self._env(
                "EMA20_ENTRY_COOLOFF_SECONDS",
                str(int(self.signal_timeframe)),
            )
        )
        # Codex review (PR #232): reject non-finite values. Without this
        # validation, `inf` survives `max(0.0, ...)` and blocks all future
        # same-label entries forever; `NaN` collapses to 0.0 in float
        # comparisons, silently disabling the gate. Either malformation
        # would defeat the safety guarantee. Fall back to one full bar.
        if (
            entry_cooloff_cfg is None
            or not math.isfinite(float(entry_cooloff_cfg))
        ):
            if entry_cooloff_cfg is not None:
                logger.warning(
                    "[%s] EMA20 entry cooloff env value is non-finite (%r); "
                    "falling back to one bar (%ds)",
                    self.env_prefix,
                    entry_cooloff_cfg,
                    int(self.signal_timeframe),
                )
            self._entry_cooloff_seconds = float(self.signal_timeframe)
        else:
            self._entry_cooloff_seconds = max(0.0, float(entry_cooloff_cfg))
        # Per-option_label monotonic timestamp of last entry submission.
        # In-memory only — restart resets the cooloff (acceptable: operator
        # restarts mid-session are rare and the sync_position_from_risk_manager
        # path will re-load any open position so duplicate-fill risk is bounded).
        self._last_entry_mono_by_label: Dict[str, float] = {}
        self._exit_max_retries = max(1, int(self._cfg.get_int("EMA20_EXIT_MAX_RETRIES", 12)))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            float(self._cfg.get_float("EMA20_EXIT_CIRCUIT_OPEN_SECONDS", 300.0)),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            float(self._cfg.get_float("EMA20_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0)),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0
        confirm_positions_age_cfg = self._parse_float(
            self._env("EMA20_EXIT_CONFIRM_POSITIONS_MAX_AGE_SECONDS", "20")
        )
        self._exit_confirm_positions_max_age_seconds = (
            max(1.0, float(confirm_positions_age_cfg))
            if confirm_positions_age_cfg is not None
            else 20.0
        )
        sync_grace_cfg = self._parse_float(
            self._env("EMA20_RISK_SYNC_GRACE_SECONDS", "30")
        )
        self._risk_sync_grace_seconds = (
            max(5.0, float(sync_grace_cfg)) if sync_grace_cfg is not None else 30.0
        )
        # PHX#199: per-label pending-exit guard. Maps option_label -> monotonic
        # timestamp of when the most-recent exit was submitted to the bridge.
        # Survives Ema20Position object replacement (sync_position_from_risk_
        # _manager can restore a fresh Position into _managed_positions while
        # an exit is in flight against the broker — that's the 2026-05-07 NG
        # scenario where Path A submitted an exit and Path B fired a duplicate
        # 11s later from a re-restored position). Watchdog auto-clears entries
        # older than _pending_exit_max_seconds so a stuck guard cannot
        # permanently block exits.
        self._pending_exit_by_label: Dict[str, float] = {}
        pending_exit_max_cfg = self._parse_float(
            self._env("EMA20_PENDING_EXIT_MAX_SECONDS", "60")
        )
        self._pending_exit_max_seconds = (
            max(15.0, float(pending_exit_max_cfg))
            if pending_exit_max_cfg is not None
            else 60.0
        )

        self._lots_cache: Optional[tuple] = None
        lots, keys, values, selected_key = self._resolve_lots()
        self._warn_if_qty_looks_like_lot_size(lots=lots, key=selected_key, values=values)
        logger.info(
            "[%s] EMA20 lots resolved | underlying=%s lots=%s key=%s keys=%s values=%s",
            self.env_prefix,
            self.underlying_label,
            lots,
            selected_key or "default",
            keys,
            values,
        )

        logger.info(
            "[%s] Ema20Strategy init | underlying=%s tf=%ds ema=%d min_atr=%s ce_prefix=%s",
            self.env_prefix,
            self.underlying_label,
            self.signal_timeframe,
            self.ema_period,
            self.min_atr,
            self.ce_label_prefix,
        )
        if self.trail_buffer_pct > 0:
            logger.info(
                "[%s] EMA20 trailing enabled | buffer_pct=%.3f trigger_pct=%s",
                self.env_prefix,
                self.trail_buffer_pct,
                self.trail_trigger_pct if self.trail_trigger_pct is not None else "tp_pct",
            )
        if self.square_off_time is not None:
            logger.info(
                "[%s] EMA20 intraday square-off enabled | square_off_time=%s",
                self.env_prefix,
                self.square_off_time.strftime("%H:%M"),
            )
        if self.first_entry_time is not None:
            logger.info(
                "[%s] EMA20 first-entry guard enabled | first_entry_time=%s",
                self.env_prefix,
                self.first_entry_time.strftime("%H:%M"),
            )
        if self.use_adx_filter:
            logger.info(
                "[%s] EMA20 ADX filter enabled | adx_period=%d min_adx=%.3f require_bearish_di=%s min_di_spread=%.3f",
                self.env_prefix,
                self.adx_period,
                self.min_adx,
                self.require_bearish_di,
                self.min_di_spread,
            )
        logger.info(
            "[%s] EMA20 exit retry cooldown enabled | cooldown_seconds=%.2f",
            self.env_prefix,
            self._exit_retry_cooldown_seconds,
        )
        if self._entry_cooloff_seconds > 0.0:
            logger.info(
                "[%s] EMA20 entry cooloff enabled (issue #223) | cooloff_seconds=%.2f",
                self.env_prefix,
                self._entry_cooloff_seconds,
            )
        else:
            logger.warning(
                "[%s] EMA20 entry cooloff DISABLED (cooloff_seconds=0) — "
                "rapid consecutive entries on the same label are NOT guarded; "
                "see issue #223 for context.",
                self.env_prefix,
            )
        logger.info(
            "[%s] EMA20 exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        logger.info(
            "[%s] EMA20 exit confirmation guard | positions_max_age_seconds=%.1f",
            self.env_prefix,
            self._exit_confirm_positions_max_age_seconds,
        )
        self._set_metric_gauge(
            "dynamic_enabled_gauge",
            value=1 if self._dynamic_enabled else 0,
        )
        if self._dynamic_enabled:
            logger.info(
                "[%s] EMA20 dynamic policy init | enabled=%s policy_id=%s policy_hash=%s hold_bars=%d min_regime_hold_minutes=%d thresholds=%s base_params=%s recognized_profile_keys=%s",
                self.env_prefix,
                self._dynamic_enabled,
                self._dynamic_policy_id,
                self._dynamic_policy_cfg.policy_hash,
                self._dynamic_policy_cfg.hold_bars,
                self._dynamic_policy_cfg.min_regime_hold_minutes,
                {
                    "adx_trend": self._dynamic_policy_cfg.thresholds.adx_trend,
                    "di_spread_trend": self._dynamic_policy_cfg.thresholds.di_spread_trend,
                    "ema_slope_trend_min": self._dynamic_policy_cfg.thresholds.ema_slope_trend_min,
                    "atr_norm_high": self._dynamic_policy_cfg.thresholds.atr_norm_high,
                    "atr_norm_secondary": self._dynamic_policy_cfg.thresholds.atr_norm_secondary,
                    "gap_ratio_spike": self._dynamic_policy_cfg.thresholds.gap_ratio_spike,
                    "atr_norm_low": self._dynamic_policy_cfg.thresholds.atr_norm_low,
                    "adx_normal_low": self._dynamic_policy_cfg.thresholds.adx_normal_low,
                    "adx_normal_high": self._dynamic_policy_cfg.thresholds.adx_normal_high,
                    "chop_adx_max": self._dynamic_policy_cfg.thresholds.chop_adx_max,
                    "chop_di_spread_max": self._dynamic_policy_cfg.thresholds.chop_di_spread_max,
                },
                self._base_params_snapshot(),
                sorted(
                    {
                        key
                        for profile in self._dynamic_policy_cfg.profiles.values()
                        for key in profile.keys()
                    }
                ),
            )
        self._restore_position_from_risk_manager()
        # Register with the direct subscription registry so POS_SYNC can clear
        # managed state without relying on routing_table.
        position_flat_registry.subscribe(self)

    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> Dict[str, Any]:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    def _restore_position_from_risk_manager(self) -> None:
        if self.position is not None or self.risk_manager is None:
            return
        self.sync_position_from_risk_manager(source="startup", use_restored=True)

    def _matching_risk_positions(
        self,
        *,
        use_restored: bool = False,
    ) -> list[tuple[datetime, str, Dict[str, Any]]]:
        if self.risk_manager is None:
            return []
        attr_name = "restored_positions" if use_restored else "open_positions"
        raw_positions = getattr(self.risk_manager, attr_name, None)
        if not isinstance(raw_positions, dict) or not raw_positions:
            return []
        state_lock = getattr(self.risk_manager, "_state_lock", None)
        if state_lock is None:
            positions_snapshot = dict(raw_positions)
        else:
            with state_lock:
                positions_snapshot = {
                    str(label): dict(entry)
                    for label, entry in raw_positions.items()
                    if isinstance(entry, dict)
                }
        candidates: list[tuple[datetime, str, Dict[str, Any]]] = []
        for raw_label, entry in positions_snapshot.items():
            label = str(raw_label or "").strip()
            if not label or not isinstance(entry, dict):
                continue
            if not self._is_matching_restored_position(label=label, entry=entry):
                continue
            entry_ts = self._parse_datetime(entry.get("entry_ts")) or self._now_utc()
            candidates.append((entry_ts, label, entry))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    def _sync_primary_position(self) -> None:
        if not self._managed_positions:
            self.position = None
            self._frozen_params = None
            return
        positions = sorted(
            self._managed_positions.values(),
            key=lambda item: item.entry_time,
            reverse=True,
        )
        self.position = positions[0]
        self._apply_restored_strategy_context(self.position.strategy_context)

    def _positions_for_label(self, label: str) -> list["Ema20Position"]:
        positions = [
            pos
            for pos in self._managed_positions.values()
            if pos.option_label == label
        ]
        positions.sort(key=lambda item: item.entry_time, reverse=True)
        return positions

    def _drop_managed_position(self, label: str, *, reason: str) -> None:
        removed = self._managed_positions.pop(label, None)
        if removed is None and (
            self.position is None or self.position.option_label != label
        ):
            return
        if self.position is not None and self.position.option_label == label:
            self.position = None
        self._sync_primary_position()
        logger.info(
            "[%s] EMA20 cleared managed position | label=%s reason=%s remaining=%d",
            self.env_prefix,
            label,
            reason,
            len(self._managed_positions),
        )

    def sync_position_from_risk_manager(
        self,
        *,
        source: str = "runtime",
        use_restored: bool = False,
    ) -> bool:
        candidates = self._matching_risk_positions(use_restored=use_restored)
        candidate_label_list = [label for _ts, label, _entry in candidates]
        if len(candidates) > 1 and sorted(candidate_label_list) != sorted(
            self._managed_positions.keys()
        ):
            logger.warning(
                "[%s] Multiple EMA20 positions matched %s state; adopting all labels=%s",
                self.env_prefix,
                source,
                candidate_label_list,
            )

        candidate_labels: set[str] = set()
        changed = False
        for _entry_ts, label, entry in candidates:
            candidate_labels.add(label)
            restored_position = self._build_restored_position(label=label, entry=entry)
            if restored_position is None:
                continue
            existing = self._managed_positions.get(label)
            if existing is not None:
                # §91: If the restored entry_price differs from the in-memory
                # one and the fill price hasn't been confirmed yet, treat the
                # synced entry_price as the actual broker fill and re-anchor
                # SL/TP/trail before copying other state fields.
                if (
                    not existing.fill_price_confirmed
                    and restored_position.entry_price > 0
                    and abs(restored_position.entry_price - existing.entry_price)
                    > 0.001
                ):
                    sc = existing.strategy_context or {}
                    sl_pct = float(sc.get("sl_pct") or self.sl_pct)
                    tp_pct = float(sc.get("tp_pct") or self.tp_pct)
                    existing.adjust_for_fill_price(
                        fill_price=restored_position.entry_price,
                        sl_pct=sl_pct,
                        tp_pct=tp_pct,
                    )
                    if existing.fill_price_confirmed:
                        logger.info(
                            "[%s] EMA20 SL/TP adjusted to fill price | label=%s "
                            "fill=%.3f sl=%.3f tp=%.3f",
                            self.env_prefix,
                            label,
                            existing.entry_price,
                            existing.sl_price,
                            existing.tp_price,
                        )
                restored_position.best_price = min(
                    float(existing.best_price),
                    float(restored_position.best_price),
                )
                restored_position.trail_active = bool(existing.trail_active)
                restored_position.sl_price = min(
                    float(existing.sl_price),
                    float(restored_position.sl_price),
                )
                restored_position.tp_price = float(existing.tp_price)
                restored_position.entry_time = existing.entry_time
                restored_position.fill_price_confirmed = existing.fill_price_confirmed
                if existing.strategy_context:
                    restored_position.strategy_context = dict(existing.strategy_context)
            elif source != "entry":
                logger.info(
                    "[%s] EMA20 adopted synced position | label=%s broker_symbol=%s qty=%d entry_price=%.3f source=%s",
                    self.env_prefix,
                    restored_position.option_label,
                    restored_position.broker_symbol,
                    restored_position.qty,
                    restored_position.entry_price,
                    source,
                )
            if existing is None or existing != restored_position:
                self._managed_positions[label] = restored_position
                changed = True

        missing_labels = [
            label
            for label in self._managed_positions.keys()
            if label not in candidate_labels
        ]
        if missing_labels and source != "entry":
            logger.debug(
                "[%s] EMA20 keeping managed positions pending explicit reconciliation | labels=%s source=%s",
                self.env_prefix,
                sorted(missing_labels),
                source,
            )

        if changed or (self._managed_positions and self.position is None):
            self._sync_primary_position()
            if self._managed_positions:
                self._reset_exit_retry_state()
        return bool(self._managed_positions)

    def _is_matching_restored_position(self, *, label: str, entry: Dict[str, Any]) -> bool:
        if label not in self.instrument_meta:
            return False
        if str(entry.get("side") or "").strip().upper() != "SELL":
            return False
        if self._positive_int(entry.get("qty")) is None:
            return False

        strategy_candidates = [
            str(entry.get("strategy_name") or "").strip(),
            str(entry.get("template_name") or "").strip(),
        ]
        strategy_match = False
        for raw_name in strategy_candidates:
            if not raw_name:
                continue
            if is_recovery_position_marker(raw_name):
                continue
            if raw_name == self._strategy_id:
                strategy_match = True
                break
            if (
                canonicalize_strategy_name(
                    raw_name,
                    source="ema20.restore",
                    warn_alias=False,
                )
                == "ema20_strategy"
            ):
                strategy_match = True
                break
        if not strategy_match:
            return False

        meta = self.instrument_meta.get(label, {})
        restored_underlying = canonical_underlying_key(
            entry.get("underlying")
            or meta.get("underlying")
            or str(label).split("_", 1)[0]
        )
        return bool(restored_underlying) and restored_underlying == self._underlying_key

    def _build_restored_position(
        self, *, label: str, entry: Dict[str, Any]
    ) -> Optional["Ema20Position"]:
        meta = self.instrument_meta.get(label, {})
        entry_price = self._parse_non_negative_float(entry.get("entry_price"))
        qty = self._positive_int(entry.get("qty"))
        if entry_price is None or entry_price <= 0 or qty is None:
            logger.warning(
                "[%s] EMA20 restore skipped invalid entry state | label=%s entry_price=%s qty=%s",
                self.env_prefix,
                label,
                entry.get("entry_price"),
                entry.get("qty"),
            )
            return None

        broker_symbol = (
            str(entry.get("tradingsymbol") or "").strip()
            or self._resolve_broker_symbol(meta, label)
        )
        exchange = (
            str(entry.get("exchange") or "").strip()
            or str(meta.get("exchange") or meta.get("exch_seg") or "").strip()
            or None
        )
        symbol_token_raw = entry.get("symboltoken")
        if symbol_token_raw in (None, ""):
            symbol_token_raw = self._resolve_symbol_token(meta)
        symbol_token = (
            str(symbol_token_raw).strip()
            if symbol_token_raw not in (None, "")
            else None
        )
        lot_size = self._positive_int(entry.get("lot_size")) or self._resolve_lot_size(
            meta=meta,
            broker_symbol=broker_symbol,
        )
        broker_qty = qty * lot_size if lot_size else None
        strategy_context = (
            dict(entry.get("strategy_context"))
            if isinstance(entry.get("strategy_context"), dict)
            else {}
        )
        sl_pct = self._parse_non_negative_float(strategy_context.get("sl_pct"))
        if sl_pct is None:
            sl_pct = float(self.sl_pct)
        tp_pct = self._parse_non_negative_float(strategy_context.get("tp_pct"))
        if tp_pct is None:
            tp_pct = float(self.tp_pct)
        entry_time = self._parse_datetime(entry.get("entry_ts")) or self._now_utc()

        return Ema20Position(
            option_label=label,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
            qty=qty,
            requested_lots=qty,
            lot_size=lot_size,
            broker_qty=broker_qty,
            entry_price=entry_price,
            sl_price=entry_price * (1.0 + sl_pct),
            tp_price=entry_price * (1.0 - tp_pct),
            best_price=entry_price,
            trail_active=False,
            entry_time=entry_time,
            strategy_context=dict(strategy_context),
            # PHX#182: restored positions assume no prior partial booking. If a
            # restart happens after a TP1 fill, the broker qty already reflects
            # the residual; using it as both qty and original_qty is correct.
            original_qty=qty,
        )

    def _apply_restored_strategy_context(self, raw_context: Any) -> None:
        if not isinstance(raw_context, dict):
            return
        base_params = self._base_params_snapshot()
        restored_params: Dict[str, Any] = {}
        for key in ("sl_pct", "tp_pct", "trail_buffer_pct", "trail_trigger_pct"):
            if key == "trail_trigger_pct" and raw_context.get(key) is None:
                restored_params[key] = None
                continue
            parsed = self._parse_non_negative_float(raw_context.get(key))
            if parsed is not None:
                restored_params[key] = parsed
        if not restored_params:
            return
        base_params.update(restored_params)
        self._effective_params = base_params
        self._frozen_params = dict(restored_params)

    # Refresh instrument metadata and option labels.
    def refresh_meta(self, instrument_meta: Dict[str, Dict[str, Any]]) -> None:
        self.instrument_meta = instrument_meta
        self.ce_label = self._find_label_prefix(self.ce_label_prefix)

    # Alias to refresh ATM labels after universe update.
    def refresh_atm_labels(self, instrument_meta: Dict[str, Dict[str, Any]]) -> None:
        self.refresh_meta(instrument_meta)

    def set_debug_gate_listener(
        self,
        listener: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        self._debug_gate_listener = listener

    def _emit_gate_decision(
        self,
        *,
        candle: Any,
        gate: str,
        passed: bool,
        reason: str,
        timeframe_seconds: Optional[int] = None,
        **extra: Any,
    ) -> None:
        listener = self._debug_gate_listener
        if not callable(listener):
            return
        bar_start_ts = getattr(candle, "start_ts", None)
        if isinstance(bar_start_ts, datetime):
            if bar_start_ts.tzinfo is None:
                bar_start_ts = bar_start_ts.replace(tzinfo=timezone.utc)
            bar_start_iso = (
                bar_start_ts.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        else:
            bar_start_iso = None
        payload: Dict[str, Any] = {
            "underlying": str(self.underlying_label),
            "gate": str(gate),
            "passed": bool(passed),
            "reason": str(reason),
            "bar_start_ts": bar_start_iso,
            "timeframe_seconds": (
                int(timeframe_seconds)
                if timeframe_seconds not in (None, "")
                else None
            ),
            "regime": self._current_regime.value,
        }
        for key, value in extra.items():
            if value is None:
                continue
            payload[key] = value.value if isinstance(value, Regime) else value
        try:
            listener(payload)
        except Exception:
            logger.debug("[%s] EMA20 debug gate listener failed", self.env_prefix)

    # Handle tick updates and manage exits.
    #
    # Order of evaluation (first match wins):
    #   1. SL  — hard stop, must always win.
    #   2. GIVEBACK (PHX#183) — peak-retrace guardrail, fires before trail to
    #      preserve gains when trailing is loose or disabled.
    #   3. TRAIL — trailing stop on best_price.
    #   4. TP1 (PHX#182) — partial book at first target, leaves residual to ride.
    #   5. TP — full target hit.
    def _manage_position_tick(self, pos: "Ema20Position", ltp: float) -> None:
        trail_buffer_pct = self._live_param_float(
            "trail_buffer_pct",
            self.trail_buffer_pct,
        )
        tp_pct_live = self._live_param_float("tp_pct", self.tp_pct)
        trail_trigger_live = self._live_param_optional_float(
            "trail_trigger_pct",
            self.trail_trigger_pct,
        )
        # §90: Minimum absolute trail buffer — prevents the percentage buffer
        # from shrinking to noise-level amounts on low-price OTM options.
        trail_min_abs_buffer = self._live_param_float("trail_min_abs_buffer", 0.0)
        # §90: Cooldown between trail SL tightenings — prevents rapid cascade
        # tightening from a burst of ticks in a fast market.
        trail_update_cooldown_s = self._live_param_float("trail_update_cooldown_seconds", 0.0)

        # PHX#184: tighten tp_price and trail_buffer once we cross into the
        # decay window (configured minutes before square_off). Recompute once.
        trail_buffer_pct, tp_pct_live = self._apply_decay_window(
            pos=pos,
            trail_buffer_pct=trail_buffer_pct,
            tp_pct_live=tp_pct_live,
        )

        # Track best (lowest) price for trailing and give-back math regardless
        # of whether trailing is enabled — give-back uses the same anchor.
        if ltp < pos.best_price:
            pos.best_price = ltp

        # PHX#183: peak_pct tracks the best move-in-favour reached so far
        # (anchored on best_price); current_pct uses ltp and may be lower.
        peak_pct = (pos.entry_price - pos.best_price) / max(pos.entry_price, 0.000001)
        current_pct = (pos.entry_price - ltp) / max(pos.entry_price, 0.000001)
        if peak_pct > pos.max_favorable_pct:
            pos.max_favorable_pct = float(peak_pct)
        giveback_pct = self._live_param_optional_float("giveback_pct", self.giveback_pct)
        giveback_arm = self._live_param_optional_float(
            "giveback_arm_pct", self.giveback_arm_pct
        )
        if (
            giveback_pct is not None
            and giveback_pct > 0
            and giveback_arm is not None
            and giveback_arm > 0
            and not pos.giveback_armed
            and pos.max_favorable_pct >= giveback_arm
        ):
            pos.giveback_armed = True
            logger.info(
                "[%s] EMA20 giveback armed | label=%s peak_pct=%.4f arm_pct=%.4f",
                self.env_prefix,
                pos.option_label,
                pos.max_favorable_pct,
                giveback_arm,
            )

        # ── Trailing-stop maintenance ────────────────────────────────────────
        if trail_buffer_pct > 0:
            trigger_pct = (
                trail_trigger_live if trail_trigger_live is not None else tp_pct_live
            )
            # Trail activates on peak (best_price) — once activated, stays active.
            if trigger_pct > 0 and peak_pct >= trigger_pct:
                pos.trail_active = True
            if pos.trail_active:
                new_trail_sl_pct = pos.best_price * (1.0 + trail_buffer_pct)
                new_trail_sl_abs = (
                    pos.best_price + float(trail_min_abs_buffer)
                    if trail_min_abs_buffer > 0
                    else new_trail_sl_pct
                )
                new_trail_sl = max(new_trail_sl_pct, new_trail_sl_abs)
                if new_trail_sl < pos.sl_price:
                    now_mono = time.monotonic()
                    last_tighten = getattr(pos, "_last_trail_tighten_at", 0.0)
                    if (
                        trail_update_cooldown_s <= 0
                        or (now_mono - last_tighten) >= trail_update_cooldown_s
                    ):
                        logger.info(
                            "[%s] EMA20 trail SL update | label=%s old=%.3f new=%.3f best=%.3f",
                            self.env_prefix,
                            pos.option_label,
                            pos.sl_price,
                            new_trail_sl,
                            pos.best_price,
                        )
                        pos.sl_price = new_trail_sl
                        setattr(pos, "_last_trail_tighten_at", now_mono)

        # ── Exit triggers — order matters (see method docstring) ────────────
        # 1. SL always wins.
        if ltp >= pos.sl_price:
            reason = "TRAIL" if pos.trail_active else "SL"
            self._exit_position(reason=reason, price=ltp, position=pos)
            return

        # 2. Give-back guardrail (PHX#183).
        if (
            pos.giveback_armed
            and giveback_pct is not None
            and giveback_pct > 0
        ):
            allowed_floor = pos.max_favorable_pct * (1.0 - giveback_pct)
            # Use current_pct (anchored on ltp) — peak_pct never falls so we'd never trigger.
            if current_pct < allowed_floor:
                logger.info(
                    "[%s] EMA20 giveback exit | label=%s peak_pct=%.4f current_pct=%.4f floor_pct=%.4f giveback_pct=%.4f",
                    self.env_prefix,
                    pos.option_label,
                    pos.max_favorable_pct,
                    current_pct,
                    allowed_floor,
                    giveback_pct,
                )
                self._exit_position(reason="GIVEBACK", price=ltp, position=pos)
                return

        # 3. TP1 — partial booking (PHX#182).
        if not pos.tp1_filled:
            tp1_pct_live = self._live_param_optional_float("tp1_pct", self.tp1_pct)
            tp1_qty_pct_live = self._live_param_float(
                "tp1_qty_pct", float(self.tp1_qty_pct), minimum=0.0
            )
            if tp1_pct_live is not None and tp1_pct_live > 0 and tp1_qty_pct_live > 0:
                tp1_price = pos.entry_price * (1.0 - float(tp1_pct_live))
                if ltp <= tp1_price:
                    tp1_lots = self._compute_tp1_lots(pos, tp1_qty_pct_live)
                    if tp1_lots > 0 and tp1_lots < int(pos.qty):
                        logger.info(
                            "[%s] EMA20 TP1 partial book | label=%s ltp=%.3f tp1_price=%.3f tp1_lots=%d/%d tp1_pct=%.4f tp1_qty_pct=%.4f",
                            self.env_prefix,
                            pos.option_label,
                            ltp,
                            tp1_price,
                            tp1_lots,
                            int(pos.qty),
                            tp1_pct_live,
                            tp1_qty_pct_live,
                        )
                        self._exit_position(
                            reason="TP1",
                            price=ltp,
                            position=pos,
                            partial_lots=tp1_lots,
                        )
                        return
                    # tp1_qty_pct rounds to entire position → fall through to TP.

        # 4. Standard TP.
        if ltp <= pos.tp_price:
            self._exit_position(reason="TP", price=ltp, position=pos)

    # PHX#182: compute the lot count for a TP1 partial exit, rounded to lot size
    # boundaries when known, capped to (qty - 1) so at least one lot remains.
    def _compute_tp1_lots(self, pos: "Ema20Position", tp1_qty_pct: float) -> int:
        base_qty = int(pos.original_qty or pos.qty)
        if base_qty <= 1:
            return 0
        target = float(base_qty) * float(tp1_qty_pct)
        lots = int(math.floor(target))
        if lots <= 0:
            return 0
        # Always leave at least one lot to ride. If tp1_qty_pct rounds to the
        # whole position, signal "no partial — let TP handle it" by returning 0.
        max_partial = int(pos.qty) - 1
        if max_partial <= 0:
            return 0
        return min(lots, max_partial)

    # PHX#184: when within decay_tighten_minutes_before_eod, recompute the
    # effective tp_price and trail_buffer_pct ONCE and stamp the position so
    # we don't recompute on every tick.
    def _apply_decay_window(
        self,
        *,
        pos: "Ema20Position",
        trail_buffer_pct: float,
        tp_pct_live: float,
    ) -> tuple[float, float]:
        if pos.decay_window_applied:
            # Already tightened — keep returning the current effective values.
            return trail_buffer_pct, tp_pct_live
        sc = pos.strategy_context or {}
        minutes_window = sc.get("decay_tighten_minutes_before_eod")
        try:
            minutes_window = int(minutes_window) if minutes_window is not None else 0
        except (TypeError, ValueError):
            minutes_window = 0
        if minutes_window <= 0 or self.square_off_time is None:
            return trail_buffer_pct, tp_pct_live
        minutes_remaining = self._minutes_to_square_off()
        if minutes_remaining is None or minutes_remaining > minutes_window:
            return trail_buffer_pct, tp_pct_live
        tp_mult = float(sc.get("decay_tp_multiplier", 1.0) or 1.0)
        trail_mult = float(sc.get("decay_trail_buffer_multiplier", 1.0) or 1.0)
        new_tp_pct = max(0.0, tp_pct_live * tp_mult)
        new_trail_buffer = max(0.0, trail_buffer_pct * trail_mult)
        new_tp_price = pos.entry_price * (1.0 - new_tp_pct)
        # Only tighten — never loosen tp_price (a multiplier > 1 is allowed by
        # config but should not push tp_price further from entry mid-trade).
        if new_tp_price > pos.tp_price:
            pos.tp_price = float(new_tp_price)
        pos.decay_window_applied = True
        logger.info(
            "[%s] EMA20 decay window applied | label=%s minutes_left=%d window=%d tp_pct=%.4f->%.4f trail_buffer=%.4f->%.4f tp_price=%.3f",
            self.env_prefix,
            pos.option_label,
            int(minutes_remaining),
            int(minutes_window),
            tp_pct_live,
            new_tp_pct,
            trail_buffer_pct,
            new_trail_buffer,
            pos.tp_price,
        )
        return new_trail_buffer, new_tp_pct

    # PHX#184: minutes from now (IST) to square_off_time. Returns None if not configured.
    def _minutes_to_square_off(self) -> Optional[int]:
        if self.square_off_time is None:
            return None
        now_ist = self._now_ist()
        target = datetime.combine(now_ist.date(), self.square_off_time, tzinfo=IST)
        if now_ist >= target:
            return 0
        return int((target - now_ist).total_seconds() // 60)

    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        self.sync_position_from_risk_manager(source="tick")
        if self._managed_positions and self._is_after_square_off():
            # force_exit_all(EOD) performs per-position authoritative checks internally;
            # it skips already-flat labels and only places orders for genuinely open ones.
            self.force_exit_all(reason="EOD", submit_orders=True)
            return
        if label == self.underlying_label and self.ce_label is None:
            self.ce_label = self._select_nearest_option(side="CE", target_price=ltp)
        for pos in self._positions_for_label(label):
            if self._exit_in_flight:
                return
            self._manage_position_tick(pos, ltp)
            if self._exit_in_flight:
                return

    # Handle bar-close signals for EMA entries.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label or timeframe_seconds != self.signal_timeframe:
            self._emit_gate_decision(
                candle=candle,
                gate="dispatch",
                passed=False,
                reason="label_or_timeframe_mismatch",
                timeframe_seconds=timeframe_seconds,
                label=label,
            )
            return
        self.sync_position_from_risk_manager(source="bar")
        if self._is_after_square_off(candle):
            self._emit_gate_decision(
                candle=candle,
                gate="square_off",
                passed=False,
                reason="square_off_reached",
                timeframe_seconds=timeframe_seconds,
            )
            if self._managed_positions:
                # force_exit_all(EOD) checks each position individually;
                # flat ones are cleared, open ones get close orders.
                self.force_exit_all(reason="EOD", submit_orders=True)
            return

        self._refresh_effective_params(candle=candle, indicators=indicators)

        ema_period = self._live_param_int("ema_period", self.ema_period, minimum=2)
        min_atr = self._live_param_optional_float("min_atr", self.min_atr)
        require_rsi_falling = self._live_param_bool(
            "require_rsi_falling",
            self.require_rsi_falling,
        )
        use_adx_filter = self._live_param_bool("use_adx_filter", self.use_adx_filter)
        min_adx = self._live_param_float("min_adx", self.min_adx)
        require_bearish_di = self._live_param_bool(
            "require_bearish_di",
            self.require_bearish_di,
        )
        min_di_spread = self._live_param_float("min_di_spread", self.min_di_spread)
        disable_entries = self._live_param_bool("disable_entries", False)
        first_entry_delay_minutes = self._live_param_int(
            "first_entry_delay_minutes",
            0,
            minimum=0,
        )

        ema_key = f"ema_{ema_period}"
        ema_val = indicators.get(ema_key)
        atr = indicators.get("atr")
        rsi = indicators.get("rsi")
        adx = self._indicator_float(indicators.get("adx"))
        plus_di = self._indicator_float(indicators.get("plus_di"))
        minus_di = self._indicator_float(indicators.get("minus_di"))
        close_price = candle.c

        logger.info(
            "[%s] EMA20 signal | label=%s tf=%ss close=%.3f ema=%s atr=%s min_atr=%s rsi=%s adx=%s plus_di=%s minus_di=%s",
            self.env_prefix,
            label,
            timeframe_seconds,
            close_price,
            ema_val,
            atr,
            min_atr,
            rsi,
            adx,
            plus_di,
            minus_di,
        )

        prev_rsi_prev = self._rsi_prev_prev
        prev_rsi = self._rsi_prev
        if isinstance(rsi, float) and math.isnan(rsi):
            rsi = None
        if rsi is not None:
            self._rsi_prev_prev = prev_rsi
            self._rsi_prev = rsi

        if self._is_before_first_entry(candle):
            self._emit_gate_decision(
                candle=candle,
                gate="first_entry_time",
                passed=False,
                reason="before_first_entry",
                timeframe_seconds=timeframe_seconds,
            )
            return
        if first_entry_delay_minutes > 0 and self._is_before_dynamic_entry_delay(
            candle,
            delay_minutes=first_entry_delay_minutes,
        ):
            self._inc_metric("no_trade_total", reason="first_entry_delay")
            self._emit_gate_decision(
                candle=candle,
                gate="first_entry_delay",
                passed=False,
                reason="first_entry_delay",
                timeframe_seconds=timeframe_seconds,
                delay_minutes=first_entry_delay_minutes,
            )
            return

        if ema_val is None or (isinstance(ema_val, float) and math.isnan(ema_val)):
            self._emit_gate_decision(
                candle=candle,
                gate="indicator",
                passed=False,
                reason="missing_ema",
                timeframe_seconds=timeframe_seconds,
            )
            return
        if (
            self._dynamic_enabled
            and self._current_regime == Regime.NO_TRADE
            and disable_entries
        ):
            self._inc_metric("no_trade_total", reason="regime_no_trade")
            self._emit_gate_decision(
                candle=candle,
                gate="dynamic_policy",
                passed=False,
                reason="regime_no_trade",
                timeframe_seconds=timeframe_seconds,
            )
            return
        if disable_entries:
            self._inc_metric("no_trade_total", reason="policy_disable_entries")
            self._emit_gate_decision(
                candle=candle,
                gate="dynamic_policy",
                passed=False,
                reason="policy_disable_entries",
                timeframe_seconds=timeframe_seconds,
            )
            return
        if min_atr is not None and (atr is None or atr < min_atr):
            self._emit_gate_decision(
                candle=candle,
                gate="atr",
                passed=False,
                reason="min_atr_not_met",
                timeframe_seconds=timeframe_seconds,
                atr=atr,
                min_atr=min_atr,
            )
            return
        if require_rsi_falling:
            if rsi is None or prev_rsi is None or prev_rsi_prev is None:
                self._emit_gate_decision(
                    candle=candle,
                    gate="rsi",
                    passed=False,
                    reason="rsi_history_missing",
                    timeframe_seconds=timeframe_seconds,
                    rsi=rsi,
                    prev_rsi=prev_rsi,
                    prev_rsi_prev=prev_rsi_prev,
                )
                return
            if not (prev_rsi_prev > prev_rsi > rsi):
                self._emit_gate_decision(
                    candle=candle,
                    gate="rsi",
                    passed=False,
                    reason="rsi_not_falling",
                    timeframe_seconds=timeframe_seconds,
                    rsi=rsi,
                    prev_rsi=prev_rsi,
                    prev_rsi_prev=prev_rsi_prev,
                )
                return
        if not self._passes_adx_filter(
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
            use_adx_filter=use_adx_filter,
            min_adx=min_adx,
            require_bearish_di=require_bearish_di,
            min_di_spread=min_di_spread,
        ):
            self._emit_gate_decision(
                candle=candle,
                gate="adx",
                passed=False,
                reason="adx_filter_blocked",
                timeframe_seconds=timeframe_seconds,
                adx=adx,
                plus_di=plus_di,
                minus_di=minus_di,
            )
            return
        if close_price >= ema_val:
            self._emit_gate_decision(
                candle=candle,
                gate="price_vs_ema",
                passed=False,
                reason="close_not_below_ema",
                timeframe_seconds=timeframe_seconds,
                close=close_price,
                ema=ema_val,
            )
            return

        self._emit_gate_decision(
            candle=candle,
            gate="entry",
            passed=True,
            reason="candidate_entry",
            timeframe_seconds=timeframe_seconds,
            close=close_price,
            ema=ema_val,
            atr=atr,
            rsi=rsi,
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
        )
        self._short_call_once_per_bar(candle, close_price, ema_val)

    def _base_params_snapshot(self) -> Dict[str, Any]:
        return {
            "ema_period": int(self.ema_period),
            "min_atr": self.min_atr,
            "require_rsi_falling": bool(self.require_rsi_falling),
            "use_adx_filter": bool(self.use_adx_filter),
            "min_adx": float(self.min_adx),
            "require_bearish_di": bool(self.require_bearish_di),
            "min_di_spread": float(self.min_di_spread),
            "sl_pct": float(self.sl_pct),
            "tp_pct": float(self.tp_pct),
            "trail_buffer_pct": float(self.trail_buffer_pct),
            "trail_trigger_pct": self.trail_trigger_pct,
            "first_entry_delay_minutes": 0,
            "disable_entries": False,
            "qty_mult": 1.0,
            # PHX#182/#183/#184: profit-booking enhancement params.
            "tp1_pct": self.tp1_pct,
            "tp1_qty_pct": float(self.tp1_qty_pct),
            "giveback_pct": self.giveback_pct,
            "giveback_arm_pct": self.giveback_arm_pct,
            "decay_tighten_minutes_before_eod": self.decay_tighten_minutes_before_eod,
            "decay_tp_multiplier": float(self.decay_tp_multiplier),
            "decay_trail_buffer_multiplier": float(self.decay_trail_buffer_multiplier),
        }

    def _refresh_effective_params(self, *, candle: Any, indicators: Dict[str, Any]) -> None:
        base_params = self._base_params_snapshot()
        self._effective_params = dict(base_params)
        if not self._dynamic_enabled:
            return
        if (
            self._context_builder is None
            or self._regime_classifier is None
            or self._policy_engine is None
        ):
            return

        self._policy_bars_seen += 1
        context = self._context_builder.build(candle=candle, indicators=indicators)
        previous_regime = self._current_regime
        regime = self._regime_classifier.update(context)
        self._current_regime = regime
        self._set_metric_gauge(
            "regime_current_gauge",
            value=1,
            regime=regime.value,
        )
        if previous_regime != regime:
            self._inc_metric(
                "regime_switch_total",
                from_regime=previous_regime.value if previous_regime else "UNKNOWN",
                to_regime=regime.value,
            )
            logger.info(
                "[%s] EMA20 regime switch | from=%s to=%s atr_norm=%s adx=%s di_spread=%s ema_slope=%s gap_ratio=%s policy_id=%s policy_hash=%s",
                self.env_prefix,
                previous_regime.value if previous_regime else "UNKNOWN",
                regime.value,
                context.atr_norm,
                context.adx14,
                context.di_spread,
                context.ema_slope,
                context.gap_ratio,
                self._dynamic_policy_id,
                self._dynamic_policy_cfg.policy_hash,
            )

        state = PositionPolicyState(
            position_open=bool(self._managed_positions),
            entry_frozen_params=dict(self._frozen_params or {}),
            entry_ts=getattr(self.position, "entry_time", None) if self.position else None,
        )
        self._effective_params = self._policy_engine.apply(
            base_params=base_params,
            regime=regime,
            context=context,
            state=state,
        )

        if previous_regime != regime or self._policy_bars_seen % self._policy_log_every_bars == 0:
            logger.info(
                "[%s] EMA20 dynamic policy apply | policy_id=%s policy_hash=%s regime=%s effective_keys=%s",
                self.env_prefix,
                self._dynamic_policy_id,
                self._dynamic_policy_cfg.policy_hash,
                regime.value,
                sorted(self._effective_params.keys()),
            )
        self._sync_policy_engine_metrics()
        self._inc_metric("policy_apply_total", regime=regime.value)

    def _inc_metric(self, name: str, amount: int = 1, **labels: Any) -> None:
        if amount <= 0:
            return
        merged = dict(self._base_metric_labels)
        merged.update({k: str(v) for k, v in labels.items() if v is not None and str(k).strip()})
        self._metrics.inc_counter(
            name,
            labels=merged,
            amount=float(amount),
        )

    def _set_metric_gauge(self, name: str, *, value: float, **labels: Any) -> None:
        merged = dict(self._base_metric_labels)
        merged.update({k: str(v) for k, v in labels.items() if v is not None and str(k).strip()})
        self._metrics.set_gauge(
            name,
            labels=merged,
            value=float(value),
        )

    def _sync_policy_engine_metrics(self) -> None:
        if self._policy_engine is None:
            return
        unknown_now = self._policy_engine.unknown_key_counts()
        for key, value in unknown_now.items():
            prev = int(self._policy_unknown_snapshot.get(key, 0))
            if value > prev:
                self._inc_metric("policy_unknown_key_total", value - prev, key=key)
        self._policy_unknown_snapshot = unknown_now

        clamp_now = self._policy_engine.clamp_counts()
        for key, value in clamp_now.items():
            prev = int(self._policy_clamp_snapshot.get(key, 0))
            if value > prev:
                self._inc_metric("policy_clamp_total", value - prev, key=key)
        self._policy_clamp_snapshot = clamp_now

        fallback_now = self._policy_engine.fallback_counts()
        for key, value in fallback_now.items():
            prev = int(self._policy_fallback_snapshot.get(key, 0))
            if value > prev:
                self._inc_metric("policy_fallback_base_total", value - prev, reason=key)
        self._policy_fallback_snapshot = fallback_now

    def _live_param_bool(self, key: str, default: bool) -> bool:
        raw = self._effective_params.get(key, default)
        return self._parse_bool_env(raw, default=default)

    def _live_param_int(self, key: str, default: int, *, minimum: int = 0) -> int:
        raw = self._effective_params.get(key, default)
        try:
            out = int(raw)
        except Exception:
            out = int(default)
        return max(minimum, out)

    def _live_param_float(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        raw = self._effective_params.get(key, default)
        try:
            out = float(raw)
        except Exception:
            out = float(default)
        if not math.isfinite(out):
            out = float(default)
        return max(minimum, out)

    def _live_param_optional_float(self, key: str, default: Optional[float]) -> Optional[float]:
        raw = self._effective_params.get(key, default)
        if raw is None:
            return None
        try:
            out = float(raw)
        except Exception:
            return default
        if not math.isfinite(out):
            return default
        return out

    def _is_before_dynamic_entry_delay(self, candle: Any, *, delay_minutes: int) -> bool:
        if self.first_entry_time is None:
            return False
        ts = getattr(candle, "start_ts", None)
        if not isinstance(ts, datetime):
            ts = self._now_utc()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now_ist = ts.astimezone(IST)
        start_dt = datetime.combine(now_ist.date(), self.first_entry_time, tzinfo=IST)
        return now_ist < (start_dt + timedelta(minutes=max(0, int(delay_minutes))))

    # ---- internals ----

    # Validate ADX/DI trend gate for EMA20 short entries.
    def _passes_adx_filter(
        self,
        *,
        adx: Optional[float],
        plus_di: Optional[float],
        minus_di: Optional[float],
        use_adx_filter: Optional[bool] = None,
        min_adx: Optional[float] = None,
        require_bearish_di: Optional[bool] = None,
        min_di_spread: Optional[float] = None,
    ) -> bool:
        effective_use_adx_filter = (
            bool(self.use_adx_filter) if use_adx_filter is None else bool(use_adx_filter)
        )
        effective_min_adx = (
            float(self.min_adx) if min_adx is None else float(min_adx)
        )
        effective_require_bearish_di = (
            bool(self.require_bearish_di)
            if require_bearish_di is None
            else bool(require_bearish_di)
        )
        effective_min_di_spread = (
            float(self.min_di_spread) if min_di_spread is None else float(min_di_spread)
        )

        if not effective_use_adx_filter:
            return True
        if adx is None or plus_di is None or minus_di is None:
            logger.info(
                "[%s][NO_TRADE][ADX_FILTER] missing_values | adx=%s plus_di=%s minus_di=%s",
                self.env_prefix,
                adx,
                plus_di,
                minus_di,
            )
            return False
        if adx < effective_min_adx:
            logger.info(
                "[%s][NO_TRADE][ADX_FILTER] low_adx | adx=%.3f min_adx=%.3f",
                self.env_prefix,
                adx,
                effective_min_adx,
            )
            return False
        if effective_require_bearish_di and minus_di <= plus_di:
            logger.info(
                "[%s][NO_TRADE][ADX_FILTER] direction_not_bearish | minus_di=%.3f plus_di=%.3f",
                self.env_prefix,
                minus_di,
                plus_di,
            )
            return False
        di_spread = minus_di - plus_di
        if effective_min_di_spread > 0.0 and di_spread < effective_min_di_spread:
            logger.info(
                "[%s][NO_TRADE][ADX_FILTER] low_di_spread | spread=%.3f min_di_spread=%.3f minus_di=%.3f plus_di=%.3f",
                self.env_prefix,
                di_spread,
                effective_min_di_spread,
                minus_di,
                plus_di,
            )
            return False
        return True

    # Enter a short call once per signal bar.
    def _short_call_once_per_bar(
        self, candle: Any, close_price: float, ema_val: float
    ) -> None:
        if self._last_fired_start_ts == getattr(candle, "start_ts", None):
            return
        self.sync_position_from_risk_manager(source="pre_entry")
        if self._managed_positions:
            return
        if self._matching_risk_positions():
            logger.warning(
                "[%s] EMA20 entry blocked because risk manager still tracks an open EMA20 position",
                self.env_prefix,
            )
            return

        ce_label = self.ce_label or self._select_nearest_option(
            side="CE", target_price=close_price
        )
        if ce_label is None:
            logger.warning("[%s] No CE label resolved for EMA20 trade", self.env_prefix)
            return

        # Issue #223: wall-clock entry cooloff per option_label. Defends
        # against rapid consecutive entries that the bar-boundary
        # `_last_fired_start_ts` guard cannot block (e.g. when bar
        # aggregation timing drifts, or when sync_position_from_risk_manager
        # clears `_managed_positions` between the two fires). On 2026-05-08
        # NATURALGAS22MAY26265CE, two BUY entries fired 47 seconds apart and
        # accumulated a 2-lot long that the trailing-lock could not cleanly
        # exit. This gate stops the second fire deterministically.
        #
        # Codex review (PR #232): replay/backtest harnesses process historical
        # candles in a tight loop, so wall-clock ``time.monotonic()`` does not
        # advance while the strategy fires across many simulated bars. A
        # cooloff of 300 real seconds would block ALL same-label re-entries
        # for 5 real minutes of replay execution and corrupt experiment
        # results. Detect replay context via the existing
        # ``app.orders.replay_context.get_replay_flag()`` (the same flag the
        # bridge uses) and skip the wall-clock cooloff there — the bar-
        # boundary ``_last_fired_start_ts`` guard remains active in replay
        # so same-bar duplicates are still blocked.
        if self._entry_cooloff_seconds > 0.0:
            try:
                from app.orders.replay_context import get_replay_flag
                in_replay = bool(get_replay_flag())
            except Exception:  # pragma: no cover - defensive
                in_replay = False
            if not in_replay:
                now_mono = time.monotonic()
                last_entry_mono = self._last_entry_mono_by_label.get(ce_label)
                if last_entry_mono is not None:
                    elapsed = now_mono - last_entry_mono
                    if elapsed < self._entry_cooloff_seconds:
                        remaining = self._entry_cooloff_seconds - elapsed
                        logger.warning(
                            "[%s] EMA20 ENTRY_BLOCKED_COOLOFF | label=%s "
                            "elapsed_s=%.2f cooloff_s=%.2f remaining_s=%.2f "
                            "(issue #223)",
                            self.env_prefix,
                            ce_label,
                            elapsed,
                            self._entry_cooloff_seconds,
                            remaining,
                        )
                        return

        meta = self.instrument_meta.get(ce_label, {})
        broker_symbol = self._resolve_broker_symbol(meta, ce_label)
        requested_lots = self._resolve_qty(meta)
        qty_mult = self._live_param_float("qty_mult", 1.0, minimum=0.01)
        qty = max(1, int(round(float(requested_lots) * float(qty_mult))))
        if qty <= 0:
            logger.warning("[%s] Non-positive qty for %s", self.env_prefix, ce_label)
            return
        lot_size = self._resolve_lot_size(meta=meta, broker_symbol=broker_symbol)

        exchange = meta.get("exchange") or meta.get("exch_seg")
        token = self._resolve_symbol_token(meta)
        sl_pct_live = self._live_param_float("sl_pct", self.sl_pct, minimum=0.0)
        tp_pct_live = self._live_param_float("tp_pct", self.tp_pct, minimum=0.0)
        trail_buffer_pct_live = self._live_param_float(
            "trail_buffer_pct",
            self.trail_buffer_pct,
            minimum=0.0,
        )
        trail_trigger_pct_live = self._live_param_optional_float(
            "trail_trigger_pct",
            self.trail_trigger_pct,
        )
        # PHX#182/#183/#184: capture profit-booking params at entry so they're
        # frozen for the lifetime of the position (matches existing freeze semantics).
        tp1_pct_live = self._live_param_optional_float("tp1_pct", self.tp1_pct)
        tp1_qty_pct_live = self._live_param_float(
            "tp1_qty_pct", float(self.tp1_qty_pct), minimum=0.0
        )
        giveback_pct_live = self._live_param_optional_float(
            "giveback_pct", self.giveback_pct
        )
        giveback_arm_pct_live = self._live_param_optional_float(
            "giveback_arm_pct", self.giveback_arm_pct
        )
        decay_minutes_live = self._live_param_int(
            "decay_tighten_minutes_before_eod",
            int(self.decay_tighten_minutes_before_eod or 0),
            minimum=0,
        )
        decay_tp_mult_live = self._live_param_float(
            "decay_tp_multiplier", float(self.decay_tp_multiplier), minimum=0.0
        )
        decay_trail_mult_live = self._live_param_float(
            "decay_trail_buffer_multiplier",
            float(self.decay_trail_buffer_multiplier),
            minimum=0.0,
        )
        entry_strategy_context = {
            "sl_pct": float(sl_pct_live),
            "tp_pct": float(tp_pct_live),
            "trail_buffer_pct": float(trail_buffer_pct_live),
            "trail_trigger_pct": trail_trigger_pct_live,
            "tp1_pct": tp1_pct_live,
            "tp1_qty_pct": float(tp1_qty_pct_live),
            "giveback_pct": giveback_pct_live,
            "giveback_arm_pct": giveback_arm_pct_live,
            "decay_tighten_minutes_before_eod": (
                int(decay_minutes_live) if decay_minutes_live > 0 else None
            ),
            "decay_tp_multiplier": float(decay_tp_mult_live),
            "decay_trail_buffer_multiplier": float(decay_trail_mult_live),
            "managed_exit_mode": "strategy",
            "broker_brackets_enabled": False,
            # PHX#186: snapshot regime at entry for exit attribution.
            "regime_at_entry": (
                self._current_regime.value if self._current_regime else ""
            ),
        }
        order_req = OrderRequest(
            symbol=broker_symbol,
            quantity=qty,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType(self.product_type),
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            stop_price=None,
            tag="EMA20_CALL_SHORT",
            purpose=OrderPurpose.ENTRY,
            exchange=exchange,
            symbol_token=str(token) if token is not None else None,
            position_label=ce_label,
            strategy_context=entry_strategy_context,
        )

        routing_kwargs = self._routing_kwargs()
        response = place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            **routing_kwargs,
        )
        status = str(getattr(response, "status", "")).upper()
        if status in {"REJECTED", "FAILED", "ERROR"}:
            logger.warning(
                "[%s] EMA20 call short rejected | label=%s status=%s msg=%s",
                self.env_prefix,
                ce_label,
                status,
                getattr(response, "message", ""),
            )
            return

        effective_lots = int(qty)
        effective_broker_qty: Optional[int] = None
        requested_broker_qty = self._positive_int(
            getattr(response, "requested_quantity", None)
        )
        if requested_broker_qty is not None:
            effective_broker_qty = int(requested_broker_qty)
            if lot_size and lot_size > 0 and requested_broker_qty % lot_size == 0:
                effective_lots = max(1, int(requested_broker_qty // lot_size))
            logger.info(
                "[%s] EMA20 entry quantity resolved | label=%s requested_lots=%d lot_size=%s broker_qty=%s effective_lots=%d",
                self.env_prefix,
                ce_label,
                requested_lots,
                lot_size,
                requested_broker_qty,
                effective_lots,
            )

        self._last_fired_start_ts = getattr(candle, "start_ts", None)
        # Issue #223: record monotonic entry timestamp so the cooloff gate at
        # the top of this method blocks any subsequent fire on the same label
        # within `_entry_cooloff_seconds`. Recorded only after the broker
        # response has been accepted (no REJECTED/FAILED status) so that a
        # broker-rejected attempt does not lock out a legitimate retry.
        if self._entry_cooloff_seconds > 0.0:
            self._last_entry_mono_by_label[ce_label] = time.monotonic()
        entry_price = self.last_price.get(ce_label, close_price)
        self._reset_exit_retry_state()
        self._managed_positions[ce_label] = Ema20Position(
            option_label=ce_label,
            broker_symbol=broker_symbol,
            exchange=str(exchange) if exchange not in (None, "") else None,
            symbol_token=str(token) if token not in (None, "") else None,
            qty=effective_lots,
            requested_lots=requested_lots,
            lot_size=lot_size,
            broker_qty=effective_broker_qty,
            entry_price=entry_price,
            sl_price=entry_price * (1.0 + sl_pct_live),
            tp_price=entry_price * (1.0 - tp_pct_live),
            best_price=entry_price,
            trail_active=False,
            entry_time=self._now_utc(),
            strategy_context=dict(entry_strategy_context),
            original_qty=effective_lots,  # PHX#182: track full position size for partial-exit math
        )
        self._sync_primary_position()
        if self._dynamic_enabled and self._policy_engine is not None:
            self._frozen_params = self._policy_engine.snapshot_entry_params(self._effective_params)
            logger.info(
                "[%s] EMA20 entry frozen params | policy_id=%s regime=%s frozen=%s",
                self.env_prefix,
                self._dynamic_policy_id,
                self._current_regime.value,
                self._frozen_params,
            )
        logger.info(
            "[%s] EMA20 CALL SHORT fired | label=%s close=%.3f ema=%.3f qty=%d",
            self.env_prefix,
            ce_label,
            close_price,
            ema_val,
            effective_lots,
        )

    # Resolve lots/quantity from config.
    def _resolve_qty(self, meta: Dict[str, Any]) -> int:
        lots, _keys, _values, _selected_key = self._resolve_lots()
        try:
            return int(lots)
        except (TypeError, ValueError):
            logger.error(
                "[%s] Could not compute quantity for %s", self.env_prefix, meta.get("symbol")
            )
            return 0

    # Resolve EMA20 lots with explicit env precedence (cached after first call).
    def _resolve_lots(self) -> tuple[int, list[str], list[str], Optional[str]]:
        if self._lots_cache is not None:
            return self._lots_cache
        underlying_key = self._underlying_key or (
            self.env_prefix.rstrip("_") or self.underlying_label.split("_")[0]
        )
        keys = [
            f"{underlying_key}_EMA20_LOTS",
            "EMA20_LOTS",
            f"{underlying_key}_EMA20_QTY",
            "EMA20_QTY",
        ]
        values: list[str] = []
        for key in keys:
            env_val = self._cfg.env.get(key)
            if env_val not in (None, ""):
                values.append(str(env_val))
                continue
            param_val = self._cfg.params.get(key)
            if param_val in (None, ""):
                param_val = self._cfg.params.get(key.lower())
            values.append(str(param_val or ""))
        selected_key: Optional[str] = None
        selected_val: Optional[int] = None

        for key, raw in zip(keys, values):
            raw_str = str(raw or "").strip()
            if not raw_str:
                continue
            try:
                parsed = int(raw_str)
                if parsed > 0:
                    selected_val = parsed
                    selected_key = key
                    break
                logger.warning(
                    "[%s] EMA20 qty invalid | key=%s value=%s keys=%s values=%s",
                    self.env_prefix,
                    key,
                    raw,
                    keys,
                    values,
                )
            except (TypeError, ValueError):
                logger.warning(
                    "[%s] EMA20 qty invalid | key=%s value=%s keys=%s values=%s",
                    self.env_prefix,
                    key,
                    raw,
                    keys,
                    values,
                )

        if selected_val is None:
            selected_val = 1
            logger.warning(
                "[%s] EMA20 qty defaulted | keys=%s values=%s selected=%s",
                self.env_prefix,
                keys,
                values,
                selected_val,
            )

        result = (selected_val, keys, values, selected_key)
        self._lots_cache = result
        return result

    # Emit a warning when configured lots exactly match a known lot size.
    def _warn_if_qty_looks_like_lot_size(
        self,
        *,
        lots: int,
        key: Optional[str],
        values: list[str],
    ) -> None:
        try:
            if int(lots) <= 1:
                return
        except (TypeError, ValueError):
            return

        underlying_key = self._underlying_key or (
            self.env_prefix.rstrip("_") or str(self.underlying_label).split("_")[0]
        ).upper()
        candidates = underlying_lookup_candidates(underlying_key)

        expected_lot_size: Optional[int] = None
        for candidate in candidates:
            size = lot_size_for_symbol_optional(candidate)
            if size:
                expected_lot_size = int(size)
                break
        if expected_lot_size is None:
            return
        if int(lots) != int(expected_lot_size):
            return

        logger.warning(
            "[%s] EMA20 lots may be misconfigured: resolved lots=%s equals lot_size=%s for %s. "
            "EMA20_LOTS expects lots (for one lot use value 1); legacy EMA20_QTY remains compatibility-only. "
            "key=%s raw_values=%s",
            self.env_prefix,
            lots,
            expected_lot_size,
            underlying_key,
            key or "default",
            values,
        )

    # Find a label matching a prefix.
    def _find_label_prefix(self, prefix: str) -> Optional[str]:
        for label in self.instrument_meta.keys():
            if label.startswith(prefix):
                return label
        if "CE" in prefix:
            return self._select_nearest_option(side="CE")
        return None

    # Choose the nearest option label by strike.
    def _select_nearest_option(
        self, *, side: str, target_price: Optional[float] = None
    ) -> Optional[str]:
        side = side.upper()
        base = self._underlying_key or canonical_underlying_key(self.underlying_label)
        candidates: list[tuple[str, float]] = []
        for lbl in self.instrument_meta.keys():
            if not lbl.startswith(f"{base}_"):
                continue
            if side not in lbl:
                continue
            strike = self._parse_strike_from_label(lbl)
            if strike is None:
                continue
            candidates.append((lbl, strike))
        if not candidates:
            return None
        if target_price is None:
            target_price = sum(s for _, s in candidates) / len(candidates)
        best = min(candidates, key=lambda ls: abs(ls[1] - target_price))
        return best[0]

    # Resolve broker tradingsymbol from metadata with safe fallback.
    @staticmethod
    def _resolve_broker_symbol(meta: Dict[str, Any], fallback_label: str) -> str:
        for key in ("symbol", "tradingsymbol", "trading_symbol", "tsym"):
            value = meta.get(key)
            if value not in (None, ""):
                return str(value)
        return fallback_label

    # Resolve broker symbol token from metadata.
    @staticmethod
    def _resolve_symbol_token(meta: Dict[str, Any]) -> Optional[str]:
        for key in ("token", "symboltoken", "instrument_token"):
            value = meta.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    # Resolve lot size for a tradingsymbol from metadata/patterns.
    @staticmethod
    def _resolve_lot_size(*, meta: Dict[str, Any], broker_symbol: str) -> Optional[int]:
        raw = meta.get("lot_size")
        if raw in (None, ""):
            raw = lot_size_for_symbol_optional(broker_symbol)
        try:
            lot_size = int(raw) if raw is not None else None
            if lot_size is None or lot_size <= 0:
                return None
            return lot_size
        except (TypeError, ValueError):
            return None

    # Parse positive int values safely.
    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            out = int(value)
            if out > 0:
                return out
        except (TypeError, ValueError):
            return None
        return None

    # Resolve exit lots from open positions when available.
    def _resolve_exit_lots(self, pos: "Ema20Position") -> int:
        fallback_lots = self._positive_int(pos.qty) or 1
        lot_size = self._positive_int(pos.lot_size)

        if lot_size and pos.broker_qty and pos.broker_qty % lot_size == 0:
            fallback_lots = max(1, int(pos.broker_qty // lot_size))

        try:
            runtime = _get_hub_runtime()
            routing_table = getattr(runtime, "routing_table", None)
            state_store = getattr(runtime, "state_store", None)
            if routing_table is None or state_store is None:
                return fallback_lots
            routes = routing_table.get_routes_for_strategy(self._strategy_id)
            if not routes:
                return fallback_lots
            for route in routes:
                positions = state_store.get_positions(route.broker_account_id)
                for entry in positions or []:
                    symbol = str(getattr(entry, "symbol", "") or "")
                    if symbol.upper() != str(pos.broker_symbol).upper():
                        continue
                    broker_qty = abs(int(getattr(entry, "quantity", 0) or 0))
                    if broker_qty <= 0:
                        continue
                    if lot_size and broker_qty % lot_size == 0:
                        resolved = max(1, broker_qty // lot_size)
                    else:
                        resolved = fallback_lots
                    logger.info(
                        "[%s] EMA20 exit qty from state_store | label=%s broker_symbol=%s broker_qty=%d lot_size=%s exit_lots=%d fallback_lots=%d",
                        self.env_prefix,
                        pos.option_label,
                        pos.broker_symbol,
                        broker_qty,
                        lot_size,
                        resolved,
                        fallback_lots,
                    )
                    return int(resolved)
        except Exception:
            logger.debug(
                "[%s] EMA20 exit qty state lookup failed; using fallback qty.",
                self.env_prefix,
                exc_info=True,
            )
        return fallback_lots

    # Reset retry/circuit-breaker state after a successful transition.
    def _reset_exit_retry_state(self) -> None:
        self._next_exit_retry_mono = 0.0
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

    # Register a failed exit attempt and schedule either retry or circuit open.
    def _register_exit_failure(self, *, now_mono: float) -> tuple[int, float, bool]:
        self._exit_failure_count += 1
        attempt = self._exit_failure_count
        if attempt >= self._exit_max_retries:
            self._exit_circuit_open_until_mono = now_mono + self._exit_circuit_open_seconds
            self._next_exit_retry_mono = self._exit_circuit_open_until_mono
            self._exit_last_alert_mono = now_mono
            return attempt, self._exit_circuit_open_seconds, True
        self._next_exit_retry_mono = now_mono + self._exit_retry_cooldown_seconds
        return attempt, self._exit_retry_cooldown_seconds, False

    @staticmethod
    def _is_transient_exit_exception(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return True
        text = str(exc).lower()
        markers = (
            "timed out",
            "timeout",
            "connection to remote host was lost",
            "connection reset",
            "connection aborted",
            "network is unreachable",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _parse_iso_datetime(value: Any) -> Optional[datetime]:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    # Return: True=open, False=confirmed flat, None=unknown (stale/unreliable state).
    def _confirm_exit_position_state(self, pos: "Ema20Position") -> Optional[bool]:
        symbol = str(pos.broker_symbol or "").upper()
        if not symbol:
            return None
        try:
            runtime = _get_hub_runtime()
            routing_table = getattr(runtime, "routing_table", None)
            state_store = getattr(runtime, "state_store", None)
            if routing_table is None or state_store is None:
                return None
            get_positions_status = getattr(state_store, "get_positions_status", None)
            if not callable(get_positions_status):
                return None
            routes = routing_table.get_routes_for_strategy(self._strategy_id)
            if not routes:
                return None

            now_utc = self._now_utc()
            checked_accounts = 0
            for route in routes:
                account_id = route.broker_account_id
                status = get_positions_status(account_id) or {}
                if str(status.get("status") or "").upper() != "OK":
                    return None
                last_ok_ts = self._parse_iso_datetime(status.get("last_ok_ts"))
                if last_ok_ts is None:
                    return None
                age_seconds = max(0.0, (now_utc - last_ok_ts).total_seconds())
                if age_seconds > self._exit_confirm_positions_max_age_seconds:
                    return None
                checked_accounts += 1

                positions = state_store.get_positions(account_id) or []
                for entry in positions:
                    entry_symbol = str(getattr(entry, "symbol", "") or "").upper()
                    if entry_symbol != symbol:
                        continue
                    qty = abs(int(getattr(entry, "quantity", 0) or 0))
                    if qty > 0:
                        return True
            if checked_accounts > 0:
                return False
        except Exception:
            logger.debug(
                "[%s] EMA20 exit state confirmation failed; treating broker state as unknown.",
                self.env_prefix,
                exc_info=True,
            )
        return None

    # Parse strike from label suffix.
    @staticmethod
    def _parse_strike_from_label(label: str) -> Optional[float]:
        try:
            part = label.rsplit("_", 1)[-1]
            return float(part)
        except Exception:
            return None

    # Parse a float with safe fallback.
    @staticmethod
    def _parse_float(val: Any) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_non_negative_float(val: Any) -> Optional[float]:
        out = Ema20Strategy._parse_float(val)
        if out is None or not math.isfinite(out) or out < 0.0:
            return None
        return out

    @staticmethod
    def _parse_datetime(val: Any) -> Optional[datetime]:
        if val in (None, ""):
            return None
        if isinstance(val, datetime):
            dt = val
        else:
            try:
                dt = datetime.fromisoformat(str(val))
            except Exception:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _env_raw(self, key: str) -> Optional[str]:
        prefixed_key = f"{self.env_prefix}{key}"
        prefixed_val = self._cfg.env.get(prefixed_key)
        if prefixed_val not in (None, ""):
            return str(prefixed_val)
        plain_val = self._cfg.env.get(key)
        if plain_val not in (None, ""):
            return str(plain_val)
        return None

    @staticmethod
    def _parse_bool_env(value: Any, *, default: bool = False) -> bool:
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

    # Convert an indicator value to float, dropping NaN/Inf safely.
    @staticmethod
    def _indicator_float(val: Any) -> Optional[float]:
        out = Ema20Strategy._parse_float(val)
        if out is None or not math.isfinite(out):
            return None
        return out

    # Parse HH:MM text into a time object.
    @staticmethod
    def _parse_time(val: Any) -> Optional[dt_time]:
        if val in (None, ""):
            return None
        try:
            text = str(val).strip()
            hour_s, min_s = text.split(":", 1)
            hour = int(hour_s)
            minute = int(min_s)
            return dt_time(hour=hour, minute=minute)
        except Exception:
            return None

    # Current strategy clock in IST. Replay harness patches this for deterministic time checks.
    def _now_utc(self) -> datetime:
        return self._now_ist().astimezone(timezone.utc)

    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    # Determine whether now (or candle timestamp) is past configured square-off time.
    def _is_after_square_off(self, candle: Any = None) -> bool:
        if self.square_off_time is None:
            return False
        ts = getattr(candle, "start_ts", None) if candle is not None else None
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now_ist = ts.astimezone(IST)
        else:
            now_ist = self._now_ist()
        return now_ist.time() >= self.square_off_time

    # Determine whether now (or candle timestamp) is before configured entry start time.
    def _is_before_first_entry(self, candle: Any = None) -> bool:
        if self.first_entry_time is None:
            return False
        ts = getattr(candle, "start_ts", None) if candle is not None else None
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now_ist = ts.astimezone(IST)
        else:
            now_ist = self._now_ist()
        return now_ist.time() < self.first_entry_time

    # Exit the current position (or a partial slice when partial_lots is set).
    #
    # When partial_lots is provided (PHX#182 TP1), exactly that many lots are
    # closed and the position is retained with reduced qty rather than dropped.
    def _exit_position(
        self,
        reason: str,
        price: Optional[float] = None,
        *,
        position: Optional["Ema20Position"] = None,
        partial_lots: Optional[int] = None,
    ) -> None:
        pos = position or self.position
        if not pos:
            return
        if self._exit_in_flight:
            return
        now_mono = time.monotonic()
        # PHX#199: per-label pending-exit guard. The global _exit_in_flight
        # flag above only blocks synchronous re-entry during the bridge call
        # itself (it is reset in finally:). Without this check, a state-
        # reconciliation exit can fire while the broker has already ACKed the
        # original exit but the fill has not yet propagated back to state_store,
        # producing a duplicate exit that flips the position direction.
        # Keyed by option_label (not position object) so the guard survives
        # the Ema20Position being replaced via sync_position_from_risk_manager
        # while an exit is in flight.
        #
        # Replay-flag bypass mirroring the entry-cooloff fix (issue #232
        # line 1656): replay/backtest harnesses process historical candles
        # in a tight loop, so wall-clock ``time.monotonic()`` does not
        # advance while the strategy fires across many simulated bars. The
        # 60-second pending-exit window would block ALL same-label re-exits
        # within that real-time window and produce <1 closed trade per
        # multi-day replay regardless of parameter sweep choices. Skip the
        # wall-clock CHECK in replay; the guard is still ARMED on every
        # bridge submission below, so LIVE PHX#199 protection is identical
        # when the replay flag is off. The existing 60s auto-recover (a few
        # lines below) handles any stale-guard cleanup that the in-replay
        # bypass would otherwise leave behind.
        try:
            from app.orders.replay_context import get_replay_flag
            in_replay = bool(get_replay_flag())
        except Exception:  # pragma: no cover - defensive
            in_replay = False

        prior_pending = self._pending_exit_by_label.get(pos.option_label)
        if not in_replay and prior_pending is not None:
            age = now_mono - prior_pending
            if age < self._pending_exit_max_seconds:
                logger.info(
                    "[%s] EMA20 exit suppressed (in-flight) | label=%s reason=%s "
                    "pending_age=%.1fs max=%.1fs",
                    self.env_prefix,
                    pos.option_label,
                    reason,
                    age,
                    self._pending_exit_max_seconds,
                )
                return
            # Auto-recover: the prior exit has been pending too long without a
            # fill observation. Clear the guard with a WARNING so this is
            # visible in alerts; allow the retry to proceed.
            logger.warning(
                "[%s] EMA20 pending-exit guard auto-cleared after %.1fs | "
                "label=%s broker_symbol=%s reason=%s; allowing retry",
                self.env_prefix,
                age,
                pos.option_label,
                pos.broker_symbol,
                reason,
            )
            self._pending_exit_by_label.pop(pos.option_label, None)
        if now_mono < self._exit_circuit_open_until_mono:
            if (
                now_mono - self._exit_last_alert_mono
                >= self._exit_circuit_alert_interval_seconds
            ):
                self._exit_last_alert_mono = now_mono
                remaining = max(0.0, self._exit_circuit_open_until_mono - now_mono)
                logger.error(
                    "[%s][ALERT] EMA20 exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    pos.broker_symbol,
                    self._exit_failure_count,
                    self._exit_max_retries,
                    remaining,
                )
            return
        if now_mono < self._next_exit_retry_mono:
            return
        ltp = price if price is not None else self.last_price.get(
            pos.option_label, pos.entry_price
        )
        meta = self.instrument_meta.get(pos.option_label, {})
        broker_symbol = pos.broker_symbol or self._resolve_broker_symbol(meta, pos.option_label)
        exchange = pos.exchange or meta.get("exchange") or meta.get("exch_seg")
        token = pos.symbol_token or self._resolve_symbol_token(meta)
        # PHX#182: partial exits specify exact lots; full exits resolve from broker state.
        is_partial = partial_lots is not None and int(partial_lots) > 0
        if is_partial:
            requested = int(partial_lots)
            available = max(0, int(pos.qty))
            exit_lots = max(0, min(requested, max(0, available - 1)))
        else:
            exit_lots = self._resolve_exit_lots(pos)
            # Cap to local qty in case state_store has not yet reflected an
            # earlier partial fill.
            if pos.qty and int(pos.qty) > 0:
                exit_lots = min(int(exit_lots), int(pos.qty))
        if exit_lots <= 0:
            logger.warning(
                "[%s] EMA20 exit qty invalid; skipping exit | label=%s broker_symbol=%s qty=%s partial=%s",
                self.env_prefix,
                pos.option_label,
                broker_symbol,
                exit_lots,
                is_partial,
            )
            return
        order_req = OrderRequest(
            symbol=broker_symbol,
            quantity=exit_lots,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType(self.product_type),
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            stop_price=None,
            tag=f"EMA20_EXIT_{reason}",
            purpose=OrderPurpose.EXIT,
            exchange=exchange,
            symbol_token=str(token) if token is not None else None,
            # Issue #253: EMA20 entries set position_label=ce_label; exits
            # must carry the same stable label so replay PnL pairing can key
            # entries and exits by option_label (e.g. "NIFTY_ATM_CE") rather
            # than fall back to broker_symbol (e.g. "NIFTYCEXXXXXX") which
            # mismatches the entry key and orphans the position in PnL.
            position_label=pos.option_label,
        )
        routing_kwargs = self._routing_kwargs()
        self._exit_in_flight = True
        # PHX#199: arm the per-label guard BEFORE the bridge call so a nested
        # re-entry (or a state-reconciliation exit fired before the bridge
        # returns) cannot slip past. Keyed by label, not position object.
        self._pending_exit_by_label[pos.option_label] = now_mono
        try:
            response = place_order_via_bridge(
                strategy_id=self._strategy_id,
                order_req=order_req,
                **routing_kwargs,
            )
        except Exception as exc:
            # PHX#199: clear the per-label guard on failure paths so the
            # retry cooldown (_next_exit_retry_mono) can fire the next attempt.
            self._pending_exit_by_label.pop(pos.option_label, None)
            if self._is_transient_exit_exception(exc):
                broker_state = self._confirm_exit_position_state(pos)
                if broker_state is False:
                    logger.warning(
                        "[%s] EMA20 exit got transient exception but position is already flat in fresh broker state | label=%s broker_symbol=%s reason=%s; clearing local position",
                        self.env_prefix,
                        pos.option_label,
                        broker_symbol,
                        reason,
                    )
                    self._reset_exit_retry_state()
                    self._drop_managed_position(
                        pos.option_label,
                        reason="broker_state_flat_after_exception",
                    )
                    return
            attempt, wait_s, opened = self._register_exit_failure(
                now_mono=time.monotonic()
            )
            if opened:
                logger.exception(
                    "[%s][ALERT] EMA20 exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    broker_symbol,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] EMA20 exit failed with exception | label=%s broker_symbol=%s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.option_label,
                    broker_symbol,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        finally:
            self._exit_in_flight = False
        status = str(getattr(response, "status", "")).upper()
        if status in {"REJECTED", "FAILED", "ERROR"}:
            # PHX#199: broker rejected the exit — clear the guard so the
            # retry cooldown can fire the next attempt.
            self._pending_exit_by_label.pop(pos.option_label, None)
            attempt, wait_s, opened = self._register_exit_failure(
                now_mono=time.monotonic()
            )
            if opened:
                logger.error(
                    "[%s][ALERT] EMA20 exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    broker_symbol,
                    reason,
                    status,
                    getattr(response, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.warning(
                    "[%s] EMA20 exit rejected | label=%s broker_symbol=%s status=%s msg=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.option_label,
                    broker_symbol,
                    status,
                    getattr(response, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state()
        # PHX#182/#186: partial exits keep the position open with reduced qty;
        # full exits drop it. Either way, emit an attribution telemetry record.
        if is_partial:
            new_qty = max(0, int(pos.qty) - int(exit_lots))
            pos.qty = new_qty
            if reason == "TP1":
                pos.tp1_filled = True
            logger.info(
                "[%s] EMA20 partial exit applied | label=%s reason=%s closed=%d remaining=%d",
                self.env_prefix,
                pos.option_label,
                reason,
                int(exit_lots),
                new_qty,
            )
            self._emit_exit_attribution(
                pos=pos,
                reason=reason,
                exit_price=float(ltp),
                exit_lots=int(exit_lots),
                final=False,
            )
            # PHX#199: partial exit succeeded; clear the per-label guard so the
            # next exit on the residual (TP / SL on remaining qty) can fire.
            # The duplicate-exit guard is only needed between FULL-exit
            # submission and fill observation, not after a TP1 partial.
            self._pending_exit_by_label.pop(pos.option_label, None)
            return
        self._emit_exit_attribution(
            pos=pos,
            reason=reason,
            exit_price=float(ltp),
            exit_lots=int(exit_lots),
            final=True,
        )
        self._drop_managed_position(pos.option_label, reason=f"exit_{reason.lower()}")

    # PHX#186: emit a structured exit-attribution event to support post-hoc
    # evaluation of the profit-booking policy. Best-effort (errors swallowed).
    def _emit_exit_attribution(
        self,
        *,
        pos: "Ema20Position",
        reason: str,
        exit_price: float,
        exit_lots: int,
        final: bool,
    ) -> None:
        try:
            entry_price = float(pos.entry_price or 0.0)
            profit_pct = (
                (entry_price - float(exit_price)) / entry_price
                if entry_price > 0
                else 0.0
            )
            held_seconds = max(
                0.0,
                (self._now_utc() - pos.entry_time).total_seconds(),
            )
            sc = pos.strategy_context or {}
            payload = {
                "v": 1,
                "schema": "ema20.exit_attribution",
                "ts": self._now_utc().isoformat(),
                "env_prefix": self.env_prefix,
                "underlying": str(self.underlying_label),
                "label": pos.option_label,
                "broker_symbol": pos.broker_symbol,
                "reason": reason,
                "final": bool(final),
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                "peak_favorable_pct": float(pos.max_favorable_pct or 0.0),
                "final_profit_pct": float(profit_pct),
                "held_seconds": float(held_seconds),
                "trail_was_active": bool(pos.trail_active),
                "tp1_filled": bool(pos.tp1_filled),
                "decay_window_applied": bool(pos.decay_window_applied),
                "exit_lots": int(exit_lots),
                "original_qty": int(pos.original_qty or pos.qty or 0),
                "remaining_qty": int(pos.qty or 0) if not final else 0,
                "regime_at_entry": str(sc.get("regime_at_entry") or ""),
                "regime_at_exit": (
                    self._current_regime.value if self._current_regime else ""
                ),
                "policy_id": str(self._dynamic_policy_id or ""),
            }
            logger.info(
                "[%s] exit_attribution | %s",
                self.env_prefix,
                payload,
            )
            self._write_exit_attribution_jsonl(payload)
        except Exception:
            logger.debug(
                "[%s] EMA20 exit attribution emit failed",
                self.env_prefix,
                exc_info=True,
            )

    def _write_exit_attribution_jsonl(self, payload: Dict[str, Any]) -> None:
        try:
            import json
            log_dir = self._cfg.get("APP_LOG_DIR", "/app/logs") or "/app/logs"
            path = os.path.join(str(log_dir), "exit_attribution.jsonl")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")
        except Exception:
            # File logging is best-effort; the structured logger above is the source of truth.
            pass

    # Force exit any open position.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self._managed_positions:
            return
        if submit_orders:
            # For EOD exits, check each position individually against the authoritative
            # source. Only place a close order for positions that are genuinely open;
            # silently clear any that risk_manager already considers flat.
            if reason == "EOD":
                open_labels, flat_labels = self._partition_managed_by_authority()
                for label in flat_labels:
                    logger.info(
                        "[%s] EMA20 EOD: skipping already-flat label=%s (risk_manager shows flat)",
                        getattr(self, "env_prefix", "?"), label,
                    )
                    self._managed_positions.pop(label, None)
                if not open_labels:
                    # Nothing genuinely open — sync state and return
                    self._sync_primary_position()
                    self._reset_exit_retry_state()
                    return
                for label in open_labels:
                    pos = self._managed_positions.get(label)
                    if pos is not None:
                        self._exit_position(reason=reason, position=pos)
            else:
                for pos in list(self._managed_positions.values()):
                    self._exit_position(reason=reason, position=pos)
        else:
            self._managed_positions.clear()
            self._sync_primary_position()
            self._reset_exit_retry_state()

    def _partition_managed_by_authority(self) -> tuple[list[str], list[str]]:
        """Split managed labels into (open, flat) based on risk_manager.open_positions.

        Returns (open_labels, flat_labels). Positions whose status cannot be
        determined are placed in open_labels (fail-open: close is risk-reducing).
        """
        rm = getattr(self, "risk_manager", None)
        if rm is None:
            # No risk_manager — cannot determine; treat all as open
            return list(self._managed_positions.keys()), []
        open_pos = getattr(rm, "open_positions", None)
        if open_pos is None:
            return list(self._managed_positions.keys()), []
        open_labels: list[str] = []
        flat_labels: list[str] = []
        for label in list(self._managed_positions.keys()):
            entry = open_pos.get(label)
            if entry and int((entry or {}).get("qty", 0) or 0) > 0:
                open_labels.append(label)
            elif label not in open_pos:
                # Label completely absent from risk_manager → authoritative flat
                flat_labels.append(label)
            else:
                # Entry present but qty == 0 → flat
                flat_labels.append(label)
        return open_labels, flat_labels

    def on_position_flat_by_sync(self, *, label: str, reason: str) -> None:
        """POS_SYNC confirmed flat with STRONG evidence. Clear without placing orders."""
        cleared = False
        if hasattr(self, "_managed_positions") and label in self._managed_positions:
            logger.info(
                "[%s] EMA20 clearing managed position via POS_SYNC flat | label=%s reason=%s",
                getattr(self, "env_prefix", "?"), label, reason,
            )
            self._managed_positions.pop(label, None)
            cleared = True
        # Also clear primary position if it matches this label
        pos = getattr(self, "position", None)
        if pos is not None:
            pos_label = getattr(pos, "option_label", None) or getattr(pos, "label", None)
            if pos_label == label:
                logger.info(
                    "[%s] EMA20 clearing primary position via POS_SYNC flat | label=%s reason=%s",
                    getattr(self, "env_prefix", "?"), label, reason,
                )
                self.position = None
                cleared = True
        if cleared:
            # Reset any exit retry state
            _reset = getattr(self, "_reset_exit_retry_state", None)
            if callable(_reset):
                _reset()
            _sync = getattr(self, "sync_position_from_risk_manager", None)
            if callable(_sync):
                try:
                    _sync(source="pos_sync_flat")
                except Exception:
                    pass

    def _has_authoritative_open_position(self) -> bool:
        """Returns True if risk_manager confirms position still open."""
        rm = getattr(self, "risk_manager", None)
        if rm is not None:
            open_pos = getattr(rm, "open_positions", {}) or {}
            managed = getattr(self, "_managed_positions", {})
            for label in list(managed.keys()):
                entry = open_pos.get(label)
                if entry:
                    qty = int((entry or {}).get("qty", 0) or 0)
                    if qty > 0:
                        return True
            # risk_manager is present and has open_positions dict — trust it
            if open_pos is not None and managed:
                if not any(lbl in open_pos for lbl in managed):
                    return False
        # Fallback: if we can't determine → fail-open (assume open)
        return True


# State for an open EMA20 option position.
@dataclass
class Ema20Position:
    option_label: str
    broker_symbol: str
    exchange: Optional[str]
    symbol_token: Optional[str]
    qty: int
    requested_lots: int
    lot_size: Optional[int]
    broker_qty: Optional[int]
    entry_price: float
    sl_price: float
    tp_price: float
    best_price: float
    trail_active: bool
    entry_time: datetime
    strategy_context: Dict[str, Any] = field(default_factory=dict)
    fill_price_confirmed: bool = False  # §91: True once broker fill price applied
    # PHX#182: partial booking state.
    tp1_filled: bool = False
    original_qty: Optional[int] = None  # qty at entry, before any partial exits
    # PHX#183: give-back guardrail tracking. max_favorable_pct is the peak
    # favourable move (entry - best_price) / entry seen so far.
    max_favorable_pct: float = 0.0
    giveback_armed: bool = False
    # PHX#184: set True once the time-decay window has tightened tp_price.
    decay_window_applied: bool = False

    def adjust_for_fill_price(
        self,
        fill_price: float,
        sl_pct: float,
        tp_pct: float,
    ) -> None:
        """Re-anchor SL, TP, and trail from the confirmed broker fill price.

        Called when the ORDER_LIFECYCLE_TERMINAL_FILL event delivers the actual
        average price, overriding the pre-fill tick price used at signal time.
        No-op if fill_price_confirmed is already True or fill_price <= 0.
        """
        if self.fill_price_confirmed or fill_price <= 0:
            return
        self.entry_price = fill_price
        self.sl_price = fill_price * (1.0 + sl_pct)
        self.tp_price = fill_price * (1.0 - tp_pct)
        self.best_price = fill_price
        self.fill_price_confirmed = True


@dataclass
class Ema20LotsResolution:
    """Result of resolving EMA20 lot/quantity configuration."""

    lots: int
    selected_key: Optional[str]
    defaulted: bool


def resolve_ema20_lots_config(
    *,
    underlying_key: str,
    env: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Ema20LotsResolution:
    """Resolve EMA20 lots from env/params without constructing a full strategy."""

    _env = dict(env or {})
    _params = dict(params or {})
    keys = [
        f"{underlying_key}_EMA20_LOTS",
        "EMA20_LOTS",
        f"{underlying_key}_EMA20_QTY",
        "EMA20_QTY",
    ]
    selected_key: Optional[str] = None
    selected_val: Optional[int] = None

    for key in keys:
        raw = _env.get(key) or _params.get(key) or _params.get(key.lower())
        raw_str = str(raw or "").strip()
        if not raw_str:
            continue
        try:
            parsed = int(raw_str)
            if parsed > 0:
                selected_val = parsed
                selected_key = key
                break
        except (TypeError, ValueError):
            pass

    defaulted = selected_val is None
    if defaulted:
        selected_val = 1

    return Ema20LotsResolution(
        lots=selected_val,
        selected_key=selected_key,
        defaulted=defaulted,
    )


__all__ = ["Ema20Strategy", "Ema20LotsResolution", "resolve_ema20_lots_config"]
