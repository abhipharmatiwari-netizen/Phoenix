"""
Put momentum scalper: buys weekly Puts on breakdowns in a downtrend.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from dataclasses import dataclass, fields
from datetime import datetime, time, timezone, timedelta, date
from typing import Any, Callable, Dict, Optional, List

from app.strategies.base import BaseStrategy
from app.config.boot_config import StrategyValueResolver
from app.config.settings import get_settings
from app.core.logging_utils import log_event
from app.observability.prometheus_metrics import counter_inc
from app.strategies.adaptive.adapter import AdaptivePolicyAdapter, OverrideRule
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import PUT_MOMENTUM_SCALPER_ID
from app.strategies.restart_state import (
    find_restored_position,
    parse_datetime,
    parse_non_negative_float,
    parse_positive_int,
    sync_open_position_state,
)
from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# Configuration for the put momentum scalper strategy.
@dataclass
class PutMomentumScalperConfig:
    entry_start: Optional[str] = None
    entry_end: Optional[str] = None
    morning_start: str = "09:20"
    morning_end: str = "11:30"
    afternoon_start: str = "13:30"
    afternoon_end: str = "15:00"
    min_atr_ratio: float = 0.0015
    trend_ema_tolerance_ratio: float = 0.0
    rsi_min: float = 25.0
    rsi_max: float = 45.0
    option_sl_pct: float = 0.25
    partial_tp_r: float = 1.0
    final_tp_r: float = 1.5
    rsi_falling_bars_required: int = 2
    lookback_breakdown_bars: int = 10
    volume_mult: float = 1.2
    max_bars_in_trade: int = 8
    strike_mode: str = "ATM"  # or ITM1
    timeframe_seconds_5m: int = 300
    timeframe_seconds_15m: int = 900
    lots_per_trade: int = 1
    skip_log_throttle_seconds: float = 60.0


# State for an open option position.
@dataclass
class OptionPosition:
    label: str
    qty: int
    entry_price: float
    stop_price: float
    partial_tp: float
    final_tp: float
    entry_time: datetime
    entry_bar_index: int
    breakdown_high: float
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# Rolling indicator and position state per instrument.
@dataclass
class InstrumentState:
    bars_5m: List[Dict[str, float]]
    last_rsi: List[float]
    last_macd_hist: List[float]
    trend_15m_down: bool = False
    last_15m_ema20: Optional[float] = None
    last_15m_close: Optional[float] = None
    vwap_sum_pv: float = 0.0
    vwap_sum_v: float = 0.0
    position: Optional[OptionPosition] = None
    last_reset_date: Optional[datetime.date] = None
    indicator_availability: Dict[str, Dict[str, Dict[str, int]]] | None = None


# Momentum-based put buying strategy with trend filter.
class PutMomentumScalperStrategy(BaseStrategy):
    """
    Intraday put momentum buyer on 5m breakdowns, filtered by 15m downtrend.
    """

    # Initialize strategy configuration and state.
    def __init__(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        order_client: Any,
        risk_manager: Any,
        *,
        env_prefix: str,
        underlying_label: str,
        params: Optional[Dict[str, Any]] = None,
        config_resolver: Optional[StrategyValueResolver] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.order_client = order_client
        self.risk_manager = risk_manager
        self.env_prefix = env_prefix
        self.underlying_label = underlying_label
        self.underlying_key = self.underlying_label.replace("_IDX", "")
        cfg_dict = params or {}
        defaults = PutMomentumScalperConfig().__dict__
        config_field_names = {f.name for f in fields(PutMomentumScalperConfig)}
        config_overrides = {
            key: value for key, value in cfg_dict.items() if key in config_field_names
        }
        merged = {**defaults, **config_overrides}
        self.config = PutMomentumScalperConfig(**merged)
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg_dict,
            env=dict(os.environ),
        )

        self.product_type = str(cfg_dict.get("product_type", "INTRADAY")).upper()
        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()
        self.timeframe_5m = int(cfg_dict.get("timeframe_seconds_5m", 300))
        self.timeframe_15m = int(cfg_dict.get("timeframe_seconds_15m", 900))
        self.lots_per_trade = int(cfg_dict.get("lots_per_trade", 1))
        dynamic_policy_raw = cfg_dict.get("dynamic_policy", {})
        if not isinstance(dynamic_policy_raw, dict):
            dynamic_policy_raw = {}
        self._adaptive_policy = AdaptivePolicyAdapter(
            strategy_name="put_momentum_scalper",
            env_prefix=self.env_prefix,
            cfg=self._cfg,
            dynamic_policy_raw=dynamic_policy_raw,
            base_params=self._base_policy_params(),
            extra_rules={
                "min_atr_ratio": OverrideRule(float, 0.0, 1.0),
                "rsi_min": OverrideRule(float, 0.0, 100.0),
                "rsi_max": OverrideRule(float, 0.0, 100.0),
                "lookback_breakdown_bars": OverrideRule(int, 3.0, 100.0),
                "max_bars_in_trade": OverrideRule(int, 1.0, 100.0),
                "trend_ema_tolerance_ratio": OverrideRule(float, 0.0, 1.0),
            },
            extra_entry_freeze_keys={
                "min_atr_ratio",
                "rsi_min",
                "rsi_max",
                "lookback_breakdown_bars",
            },
            context_builder_kwargs={
                "ema_period": 20,
                "min_median_samples": 10,
            },
            dynamic_enabled_env_key="PUT_MOM_DYNAMIC_POLICY_ENABLED",
            dynamic_policy_id_env_key="PUT_MOM_DYNAMIC_POLICY_ID",
        )

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
        else:
            self._use_hub_router_flag = False

        self._strategy_id = PUT_MOMENTUM_SCALPER_ID
        self.state: InstrumentState = InstrumentState(
            bars_5m=[],
            last_rsi=[],
            last_macd_hist=[],
            indicator_availability={},
        )
        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._debug_gate_listener: Optional[Callable[[Dict[str, Any]], None]] = None
        self._last_skip_log_mono: Dict[str, float] = {}
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        retry_cfg = self._cfg.get_float("PUT_MOM_EXIT_RETRY_COOLDOWN_SECONDS", 5.0)
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_int("PUT_MOM_EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("PUT_MOM_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("PUT_MOM_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

        logger.info(
            "[%s] PutMomentumScalper init | underlying=%s",
            self.env_prefix,
            self.underlying_label,
        )
        logger.info(
            "[%s] PUT_MOM filters | min_atr_ratio=%.6f trend_ema_tolerance_ratio=%.6f",
            self.env_prefix,
            self.config.min_atr_ratio,
            max(0.0, float(self.config.trend_ema_tolerance_ratio)),
        )
        logger.info(
            "[%s] PUT_MOM exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        self._restore_position_from_risk_manager()
        if self._adaptive_policy.enabled:
            logger.info(
                "[%s] PUT_MOM dynamic policy enabled | policy_id=%s policy_hash=%s",
                self.env_prefix,
                self._adaptive_policy.policy_config.policy_id or "",
                self._adaptive_policy.policy_config.policy_hash,
            )

    # ---- helpers ----

    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    # Parse a HH:MM time value.
    def _parse_time(self, val: str) -> time:
        try:
            h, m = (int(p) for p in str(val).split(":"))
            return time(hour=h, minute=m)
        except Exception:
            return time(9, 30)

    def _base_policy_params(self) -> Dict[str, Any]:
        return {
            "disable_entries": False,
            "qty_mult": 1.0,
            "min_atr_ratio": float(self.config.min_atr_ratio),
            "rsi_min": float(self.config.rsi_min),
            "rsi_max": float(self.config.rsi_max),
            "lookback_breakdown_bars": int(self.config.lookback_breakdown_bars),
            "max_bars_in_trade": int(self.config.max_bars_in_trade),
            "trend_ema_tolerance_ratio": float(self.config.trend_ema_tolerance_ratio),
        }

    def set_debug_gate_listener(
        self,
        listener: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        self._debug_gate_listener = listener

    def indicator_availability_snapshot(self) -> Dict[str, Any]:
        snapshot = self.state.indicator_availability or {}
        return {
            str(timeframe_seconds): {
                str(indicator): {
                    "present": int(counts.get("present", 0)),
                    "missing": int(counts.get("missing", 0)),
                }
                for indicator, counts in sorted((indicators or {}).items())
            }
            for timeframe_seconds, indicators in sorted(snapshot.items())
        }

    def _emit_gate_decision(
        self,
        *,
        candle: Any,
        gate: str,
        passed: bool,
        reason: str,
        timeframe_seconds: int,
        **extra: Any,
    ) -> None:
        listener = self._debug_gate_listener
        if not callable(listener):
            return
        bar_start_ts = getattr(candle, "start_ts", None)
        bar_start_iso: Optional[str]
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
            "timeframe_seconds": int(timeframe_seconds),
        }
        for key, value in extra.items():
            if value is None:
                continue
            payload[key] = value
        try:
            listener(payload)
        except Exception:
            logger.debug("[%s] PUT_MOM debug gate listener failed", self.env_prefix)

    def _record_indicator_availability(
        self,
        *,
        timeframe_seconds: int,
        indicators: Dict[str, Any],
    ) -> None:
        required_indicators: List[str] = []
        if int(timeframe_seconds) == int(self.timeframe_5m):
            required_indicators = [
                "ema_20",
                "ema_50",
                "rsi",
                "macd",
                "macd_signal",
                "macd_hist",
                "atr",
            ]
        elif int(timeframe_seconds) == int(self.timeframe_15m):
            required_indicators = ["ema_20"]
        if not required_indicators:
            return
        availability = self.state.indicator_availability
        if availability is None:
            availability = {}
            self.state.indicator_availability = availability
        timeframe_key = str(int(timeframe_seconds))
        timeframe_bucket = availability.setdefault(timeframe_key, {})
        for indicator_name in required_indicators:
            counts = timeframe_bucket.setdefault(
                indicator_name,
                {"present": 0, "missing": 0},
            )
            if indicators.get(indicator_name) is None:
                counts["missing"] = int(counts.get("missing", 0)) + 1
            else:
                counts["present"] = int(counts.get("present", 0)) + 1

    def _inc_gate_metric(
        self,
        *,
        reason: str,
        timeframe_seconds: int,
        passed: bool,
        indicator: Optional[str] = None,
    ) -> None:
        labels = {
            "strategy": "put_momentum_scalper",
            "underlying": str(self.underlying_label),
            "timeframe_seconds": str(int(timeframe_seconds)),
            "passed": str(bool(passed)).lower(),
            "reason": str(reason),
        }
        if indicator:
            labels["indicator"] = str(indicator)
        counter_inc(
            "phoenix_strategy_gate_decisions_total",
            labels=labels,
            help_text="Strategy gate decisions",
        )

    def _log_gate_skip(
        self,
        *,
        candle: Any,
        timeframe_seconds: int,
        reason: str,
        indicator: Optional[str] = None,
        **extra: Any,
    ) -> None:
        throttle_seconds = max(0.0, float(self.config.skip_log_throttle_seconds))
        throttle_key = f"{int(timeframe_seconds)}|{reason}|{indicator or ''}"
        now_mono = monotonic()
        last_logged_mono = self._last_skip_log_mono.get(throttle_key)
        if (
            throttle_seconds > 0.0
            and last_logged_mono is not None
            and (now_mono - last_logged_mono) < throttle_seconds
        ):
            return
        self._last_skip_log_mono[throttle_key] = now_mono
        payload = {
            "underlying": str(self.underlying_label),
            "strategy": "put_momentum_scalper",
            "reason": str(reason),
            "timeframe_seconds": int(timeframe_seconds),
            "bar_start_ts": getattr(candle, "start_ts", None),
        }
        if indicator:
            payload["indicator"] = str(indicator)
        payload.update(extra)
        log_event(
            logger,
            event_type="STRATEGY_GATE_SKIP",
            message="Skipped put_momentum_scalper entry after gate evaluation.",
            instrument=str(self.underlying_label),
            **payload,
        )

    def _reject_entry(
        self,
        *,
        candle: Any,
        timeframe_seconds: int,
        gate: str,
        reason: str,
        indicator: Optional[str] = None,
        **extra: Any,
    ) -> None:
        self._inc_gate_metric(
            reason=reason,
            timeframe_seconds=timeframe_seconds,
            passed=False,
            indicator=indicator,
        )
        self._emit_gate_decision(
            candle=candle,
            gate=gate,
            passed=False,
            reason=reason,
            timeframe_seconds=timeframe_seconds,
            indicator=indicator,
            **extra,
        )
        self._log_gate_skip(
            candle=candle,
            timeframe_seconds=timeframe_seconds,
            reason=reason,
            indicator=indicator,
            **extra,
        )

    def _dynamic_policy_skip_details(self, *, disable_entries: bool) -> Dict[str, Any]:
        regime = getattr(self._adaptive_policy.regime, "value", str(self._adaptive_policy.regime))
        context = self._adaptive_policy.last_context
        payload: Dict[str, Any] = {
            "regime": regime,
            "disable_entries": bool(disable_entries),
            "selected_profile": self._adaptive_policy.selected_profile(),
            "atr_norm": None,
            "adx14": None,
            "di_spread": None,
            "ema_slope": None,
        }
        if context is not None:
            payload.update(
                {
                    "atr_norm": context.atr_norm,
                    "adx14": context.adx14,
                    "di_spread": context.di_spread,
                    "ema_slope": context.ema_slope,
                }
            )
        return payload

    def _accept_candidate(
        self,
        *,
        candle: Any,
        timeframe_seconds: int,
        **extra: Any,
    ) -> None:
        self._inc_gate_metric(
            reason="candidate_entry",
            timeframe_seconds=timeframe_seconds,
            passed=True,
        )
        self._emit_gate_decision(
            candle=candle,
            gate="entry",
            passed=True,
            reason="candidate_entry",
            timeframe_seconds=timeframe_seconds,
            **extra,
        )

    def _rsi_is_falling(self, required_declines: int) -> bool:
        declines = max(1, int(required_declines))
        history = self.state.last_rsi[-(declines + 1) :]
        if len(history) < (declines + 1):
            return False
        return all(history[idx] < history[idx - 1] for idx in range(1, len(history)))

    def _has_fresh_negative_macd_cross(self) -> bool:
        history = self.state.last_macd_hist[-2:]
        return len(history) == 2 and history[0] >= 0 and history[1] < 0

    # Return current IST time.
    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    # Reset state for a new trading day.
    def _reset_if_new_day(self, current_dt: datetime) -> None:
        d = current_dt.date()
        if self.state.last_reset_date == d:
            return
        self.state = InstrumentState(
            bars_5m=[],
            last_rsi=[],
            last_macd_hist=[],
            indicator_availability={},
            last_reset_date=d,
        )
        self._last_skip_log_mono = {}
        self._reset_exit_retry_state()
        logger.info("[%s] Daily reset for %s", self.env_prefix, d)

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

    # Persist broker identity from entry-time metadata.
    def _remember_order_identity(self, label: str) -> None:
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange") or meta.get("exch_seg")
        self._order_identity_by_label[label] = {
            "broker_symbol": self._resolve_broker_symbol(meta, label),
            "exchange": str(exchange) if exchange not in (None, "") else None,
            "symbol_token": self._resolve_symbol_token(meta),
        }

    # Resolve order identity with entry-time preference, then metadata fallback.
    def _resolve_order_identity(
        self, label: str
    ) -> tuple[str, Optional[str], Optional[str]]:
        stored = self._order_identity_by_label.get(label, {})
        meta = self.instrument_meta.get(label, {})
        broker_symbol = stored.get("broker_symbol") or self._resolve_broker_symbol(meta, label)
        exchange = stored.get("exchange") or meta.get("exchange") or meta.get("exch_seg")
        token = stored.get("symbol_token") or self._resolve_symbol_token(meta)
        return (
            str(broker_symbol),
            str(exchange) if exchange not in (None, "") else None,
            str(token) if token not in (None, "") else None,
        )

    def _position_strategy_context(self, pos: OptionPosition) -> Dict[str, Any]:
        bars_in_trade = max(0, len(self.state.bars_5m) - int(pos.entry_bar_index))
        return {
            "stop_price": float(pos.stop_price),
            "partial_tp": float(pos.partial_tp),
            "final_tp": float(pos.final_tp),
            "breakdown_high": float(pos.breakdown_high),
            "position_qty": int(pos.qty),
            "bars_in_trade": int(bars_in_trade),
        }

    def _sync_position_state_to_risk_manager(self) -> None:
        pos = self.state.position
        if not pos:
            return
        sync_open_position_state(
            risk_manager=self.risk_manager,
            label=pos.label,
            side="BUY",
            qty=max(1, int(pos.qty)),
            entry_price=float(pos.entry_price),
            strategy_context=self._position_strategy_context(pos),
        )

    def _restore_position_from_risk_manager(self) -> None:
        if self.state.position is not None or self.risk_manager is None:
            return
        match = find_restored_position(
            risk_manager=self.risk_manager,
            instrument_meta=self.instrument_meta,
            strategy_id=self._strategy_id,
            canonical_strategy_name="put_momentum_scalper",
            underlying_candidates=(self.underlying_label, self.underlying_key),
            side="BUY",
        )
        if match is None:
            return
        label, entry = match
        strategy_context = (
            dict(entry.get("strategy_context"))
            if isinstance(entry.get("strategy_context"), dict)
            else {}
        )
        entry_price = parse_non_negative_float(entry.get("entry_price"))
        stop_price = parse_non_negative_float(strategy_context.get("stop_price"))
        partial_tp = parse_non_negative_float(strategy_context.get("partial_tp"))
        final_tp = parse_non_negative_float(strategy_context.get("final_tp"))
        breakdown_high = parse_non_negative_float(strategy_context.get("breakdown_high"))
        qty = parse_positive_int(strategy_context.get("position_qty")) or parse_positive_int(
            entry.get("qty")
        )
        bars_in_trade = parse_positive_int(strategy_context.get("bars_in_trade")) or 0
        if (
            entry_price is None
            or stop_price is None
            or partial_tp is None
            or final_tp is None
            or breakdown_high is None
            or qty is None
        ):
            return

        entry_time = parse_datetime(entry.get("entry_ts")) or datetime.now(timezone.utc)
        meta = self.instrument_meta.get(label, {})
        broker_symbol = str(entry.get("tradingsymbol") or "").strip() or self._resolve_broker_symbol(
            meta,
            label,
        )
        exchange = str(
            entry.get("exchange") or meta.get("exchange") or meta.get("exch_seg") or ""
        ).strip() or None
        symbol_token = str(
            entry.get("symboltoken") or meta.get("token") or ""
        ).strip() or None

        self._order_identity_by_label[label] = {
            "broker_symbol": broker_symbol,
            "exchange": exchange,
            "symbol_token": symbol_token,
        }
        restored_entry_time = entry_time.astimezone(IST)
        self.state.position = OptionPosition(
            label=label,
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            partial_tp=partial_tp,
            final_tp=final_tp,
            entry_time=restored_entry_time,
            entry_bar_index=0,
            breakdown_high=breakdown_high,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self.state.bars_5m = [
            {"h": 0.0, "l": 0.0, "c": 0.0}
            for _ in range(max(0, int(bars_in_trade)))
        ]
        self.state.last_reset_date = restored_entry_time.date()
        self._reset_exit_retry_state()

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

    # Select the PE option label based on strike and expiry.
    def _select_put_option(self, spot: float) -> Optional[str]:
        current_expiry = self._current_expiry()
        candidates = []
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            if "PE" not in str(meta.get("kind", "")).upper():
                continue
            if current_expiry:
                exp_dt = self._parse_expiry(meta.get("expiry") or meta.get("expiry_dt"))
                if not exp_dt:
                    continue
                if exp_dt != current_expiry:
                    continue
            strike = self._parse_strike(lbl)
            if strike is None:
                continue
            candidates.append((lbl, strike))
        if not candidates:
            if current_expiry:
                logger.warning(
                    "[%s] No PE candidates matching expiry=%s", self.env_prefix, current_expiry
                )
            return None
        candidates.sort(key=lambda x: x[1])
        strike_values = [s for _, s in candidates]
        diffs = [b - a for a, b in zip(strike_values, strike_values[1:]) if (b - a) > 0]
        strike_step = diffs[0] if diffs else 50.0
        atm_label, atm_strike = min(candidates, key=lambda x: abs(x[1] - spot))
        target_strike = atm_strike
        if self.config.strike_mode.upper() == "ITM1":
            target_strike = atm_strike + strike_step  # deeper ITM for puts => higher strike
        chosen = min(candidates, key=lambda x: abs(x[1] - target_strike))
        return chosen[0]

    # Extract strike price from label.
    def _parse_strike(self, label: str) -> Optional[float]:
        try:
            return float(label.rsplit("_", 1)[-1])
        except Exception:
            return None

    # Parse expiry date from metadata.
    def _parse_expiry(self, raw: Any) -> Optional[date]:
        if not raw:
            return None
        try:
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
            if hasattr(raw, "to_pydatetime"):
                return raw.to_pydatetime().date()
            if hasattr(raw, "date"):
                return raw.date()
            return datetime.fromisoformat(str(raw)).date()
        except Exception:
            try:
                return datetime.strptime(str(raw), "%d%b%Y").date()
            except Exception:
                return None

    # Choose the nearest valid expiry date.
    def _current_expiry(self) -> Optional[date]:
        today = self._now_ist().date()
        expiries = []
        for meta in self.instrument_meta.values():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            kind = str(meta.get("kind", "")).upper()
            if "PE" not in kind:
                continue
            exp_dt = self._parse_expiry(meta.get("expiry") or meta.get("expiry_dt"))
            if exp_dt and exp_dt >= today:
                expiries.append(exp_dt)
        if not expiries:
            return None
        return min(expiries)

    # Resolve quantity to trade for a label.
    def _qty_for_label(self, label: str) -> int:
        qty_mult = self._adaptive_policy.get_float("qty_mult", 1.0, minimum=0.01)
        return max(1, int(round(float(self.lots_per_trade) * float(qty_mult))))

    # Check if time is within allowed entry windows.
    def _within_entry_window(self, t: time) -> bool:
        if self.config.entry_start and self.config.entry_end:
            entry_start = self._parse_time(self.config.entry_start)
            entry_end = self._parse_time(self.config.entry_end)
            return entry_start <= t <= entry_end
        m_start = self._parse_time(self.config.morning_start)
        m_end = self._parse_time(self.config.morning_end)
        a_start = self._parse_time(self.config.afternoon_start)
        a_end = self._parse_time(self.config.afternoon_end)
        return (m_start <= t <= m_end) or (a_start <= t <= a_end)

    # Submit an order through the strategy bridge.
    def _place_order(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: float,
        tag: Optional[str],
        purpose: Optional[OrderPurpose] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> OrderResponse:
        meta = self.instrument_meta.get(label)
        if not meta:
            logger.error("[%s] No meta for %s", self.env_prefix, label)
            return OrderResponse(
                broker_order_id=label,
                status="REJECTED",
                message="meta_missing",
                filled_quantity=0,
                average_price=None,
            )
        broker_symbol, exchange, token = self._resolve_order_identity(label)
        order_req = OrderRequest(
            symbol=broker_symbol,
            quantity=qty,
            side=OrderSide(side.upper()),
            order_type=OrderType.MARKET,
            product_type=ProductType(self.product_type),
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            stop_price=None,
            tag=tag,
            purpose=purpose,
            exchange=exchange,
            symbol_token=str(token) if token is not None else None,
            position_label=label,
            strategy_context=dict(strategy_context) if strategy_context else None,
        )
        routing_kwargs = self._routing_kwargs()
        return place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            **routing_kwargs,
        )

    # ---- hooks ----

    # Handle tick updates and manage exits.
    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        now = self._now_ist()
        self._reset_if_new_day(now)
        pos = self.state.position
        if pos and label == pos.label:
            if ltp <= pos.stop_price:
                logger.info(
                    "[%s] SL hit %s entry=%.3f sl=%.3f ltp=%.3f",
                    self.env_prefix,
                    label,
                    pos.entry_price,
                    pos.stop_price,
                    ltp,
                )
                self._exit_position("SL")
            elif ltp >= pos.final_tp:
                logger.info(
                    "[%s] Final TP hit %s entry=%.3f tp=%.3f ltp=%.3f",
                    self.env_prefix,
                    label,
                    pos.entry_price,
                    pos.final_tp,
                    ltp,
                )
                self._exit_position("TP")
        if pos and now.time() >= time(15, 20):
            self._exit_position("EOD")

    # Handle bar-close updates for 5m and 15m logic.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label:
            return

        ist_dt = candle.start_ts.astimezone(IST)
        self._reset_if_new_day(ist_dt)
        self._adaptive_policy.set_base_params(self._base_policy_params())
        self._adaptive_policy.refresh(
            candle=candle,
            indicators=indicators,
            position_open=bool(self.state.position),
        )
        if timeframe_seconds == self.timeframe_15m:
            self._handle_15m_bar(
                candle,
                indicators,
                trend_tolerance_ratio=self._adaptive_policy.get_float(
                    "trend_ema_tolerance_ratio",
                    self.config.trend_ema_tolerance_ratio,
                    minimum=0.0,
                ),
            )
        if timeframe_seconds == self.timeframe_5m:
            self._handle_5m_bar(candle, indicators, ist_dt)

    # ---- internal ----

    # Update trend state from 15m bars.
    def _handle_15m_bar(
        self,
        candle: Any,
        indicators: Dict[str, Any],
        *,
        trend_tolerance_ratio: Optional[float] = None,
    ) -> None:
        self._record_indicator_availability(
            timeframe_seconds=self.timeframe_15m,
            indicators=indicators,
        )
        ema20 = indicators.get("ema_20")
        close = candle.c
        if ema20 is None:
            self.state.last_15m_ema20 = None
            self.state.last_15m_close = close
            self.state.trend_15m_down = False
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_15m,
                gate="indicator",
                reason="missing_indicator",
                indicator="ema_20",
            )
            return
        self.state.last_15m_ema20 = ema20
        self.state.last_15m_close = close
        tol = max(
            0.0,
            float(
                self.config.trend_ema_tolerance_ratio
                if trend_tolerance_ratio is None
                else trend_tolerance_ratio
            ),
        )
        self.state.trend_15m_down = close <= (ema20 * (1.0 + tol))
        if not self.state.trend_15m_down:
            self._emit_gate_decision(
                candle=candle,
                gate="trend_15m",
                passed=False,
                reason="trend_15m_not_down",
                timeframe_seconds=self.timeframe_15m,
                close=close,
                ema20=ema20,
                tolerance_ratio=tol,
            )

    # Evaluate 5m setup and possibly enter trades.
    def _handle_5m_bar(self, candle: Any, indicators: Dict[str, Any], ist_dt: datetime) -> None:
        state = self.state
        live_disable_entries = self._adaptive_policy.get_bool("disable_entries", False)
        live_rsi_min = self._adaptive_policy.get_float(
            "rsi_min",
            self.config.rsi_min,
            minimum=0.0,
        )
        live_rsi_max = self._adaptive_policy.get_float(
            "rsi_max",
            self.config.rsi_max,
            minimum=0.0,
        )
        live_min_atr_ratio = self._adaptive_policy.get_float(
            "min_atr_ratio",
            self.config.min_atr_ratio,
            minimum=0.0,
        )
        live_lookback_breakdown_bars = self._adaptive_policy.get_int(
            "lookback_breakdown_bars",
            self.config.lookback_breakdown_bars,
            minimum=3,
        )
        live_rsi_falling_bars_required = max(
            1,
            int(self.config.rsi_falling_bars_required),
        )
        live_max_bars_in_trade = self._adaptive_policy.get_int(
            "max_bars_in_trade",
            self.config.max_bars_in_trade,
            minimum=1,
        )
        state.bars_5m.append({"h": candle.h, "l": candle.low, "c": candle.c})
        if len(state.bars_5m) > 100:
            state.bars_5m = state.bars_5m[-100:]

        # lightweight VWAP proxy using closes
        state.vwap_sum_pv += candle.c
        state.vwap_sum_v += 1.0
        vwap = state.vwap_sum_pv / state.vwap_sum_v if state.vwap_sum_v > 0 else None

        rsi = indicators.get("rsi")
        macd = indicators.get("macd")
        macd_sig = indicators.get("macd_signal")
        macd_hist = indicators.get("macd_hist")
        atr = indicators.get("atr")
        ema20 = indicators.get("ema_20")
        ema50 = indicators.get("ema_50")

        self._record_indicator_availability(
            timeframe_seconds=self.timeframe_5m,
            indicators=indicators,
        )

        if rsi is not None:
            state.last_rsi.append(float(rsi))
            if len(state.last_rsi) > 5:
                state.last_rsi = state.last_rsi[-5:]
        if macd_hist is not None:
            state.last_macd_hist.append(float(macd_hist))
            if len(state.last_macd_hist) > 5:
                state.last_macd_hist = state.last_macd_hist[-5:]

        if state.position:
            self._maybe_invalidate(candle, ema20)
            self._maybe_time_stop(max_bars_in_trade=live_max_bars_in_trade)
            if state.position:
                self._sync_position_state_to_risk_manager()
            return

        if live_disable_entries:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="dynamic_policy",
                reason="policy_disable_entries",
                **self._dynamic_policy_skip_details(disable_entries=live_disable_entries),
            )
            return
        if not self._within_entry_window(ist_dt.time()):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="entry_window",
                reason="outside_entry_window",
                current_time=ist_dt.strftime("%H:%M"),
            )
            return
        if not state.trend_15m_down:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="trend_15m",
                reason="trend_15m_not_down",
                last_15m_close=state.last_15m_close,
                last_15m_ema20=state.last_15m_ema20,
            )
            return
        if ema20 is None or ema50 is None:
            missing_indicator = "ema_20" if ema20 is None else "ema_50"
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="indicator",
                reason="missing_indicator",
                indicator=missing_indicator,
            )
            return
        if candle.c >= ema20 or candle.c >= ema50:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="price_vs_ema",
                reason="close_not_below_ema20_ema50",
                close=candle.c,
                ema20=ema20,
                ema50=ema50,
            )
            return
        if vwap is not None and candle.c >= vwap:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="price_vs_vwap",
                reason="close_not_below_vwap",
                close=candle.c,
                vwap=vwap,
            )
            return
        if rsi is None:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="indicator",
                reason="missing_indicator",
                indicator="rsi",
            )
            return
        if len(state.last_rsi) < (live_rsi_falling_bars_required + 1):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="rsi",
                reason="rsi_history_missing",
                rsi=rsi,
                required_declines=live_rsi_falling_bars_required,
            )
            return
        if not (live_rsi_min <= rsi <= live_rsi_max):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="rsi",
                reason="rsi_out_of_range",
                rsi=rsi,
                rsi_min=live_rsi_min,
                rsi_max=live_rsi_max,
            )
            return
        if not self._rsi_is_falling(live_rsi_falling_bars_required):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="rsi",
                reason="rsi_not_falling",
                rsi=rsi,
                required_declines=live_rsi_falling_bars_required,
            )
            return
        if macd is None or macd_sig is None or macd_hist is None:
            missing_indicator = "macd"
            if macd is not None and macd_sig is None:
                missing_indicator = "macd_signal"
            elif macd is not None and macd_sig is not None and macd_hist is None:
                missing_indicator = "macd_hist"
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="indicator",
                reason="missing_indicator",
                indicator=missing_indicator,
            )
            return
        if not (macd < macd_sig and macd_hist < 0):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="macd",
                reason="macd_not_bearish",
                macd=macd,
                macd_signal=macd_sig,
                macd_hist=macd_hist,
            )
            return
        if not self._has_fresh_negative_macd_cross():
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="macd",
                reason="macd_no_fresh_negative_cross",
                macd_hist=macd_hist,
                prev_macd_hist=(
                    state.last_macd_hist[-2]
                    if len(state.last_macd_hist) >= 2
                    else None
                ),
            )
            return
        if atr is None:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="indicator",
                reason="missing_indicator",
                indicator="atr",
            )
            return
        if (atr / candle.c) < live_min_atr_ratio:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="atr",
                reason="atr_below_threshold",
                atr=atr,
                atr_ratio=(atr / candle.c),
                min_atr_ratio=live_min_atr_ratio,
            )
            return

        if not self._is_breakdown_bar(candle, lookback_bars=live_lookback_breakdown_bars):
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="breakdown",
                reason="breakdown_not_confirmed",
                lookback_bars=live_lookback_breakdown_bars,
            )
            return

        self._accept_candidate(
            candle=candle,
            timeframe_seconds=self.timeframe_5m,
            close=candle.c,
            ema20=ema20,
            ema50=ema50,
            vwap=vwap,
            rsi=rsi,
            macd=macd,
            macd_signal=macd_sig,
            macd_hist=macd_hist,
            atr=atr,
        )
        self._enter_position(candle, ema20, vwap)

    # Detect breakdown bar characteristics.
    def _is_breakdown_bar(self, candle: Any, *, lookback_bars: Optional[int] = None) -> bool:
        lookback = max(
            3,
            int(
                self.config.lookback_breakdown_bars
                if lookback_bars is None
                else lookback_bars
            ),
        )
        lows = [b["l"] for b in self.state.bars_5m[-lookback:]]
        if not lows:
            return False
        is_lowest = candle.low <= min(lows)
        rng = max(candle.h - candle.low, 1e-6)
        lower_wick_ratio = (candle.c - candle.low) / rng if rng else 1.0
        return is_lowest and lower_wick_ratio <= 0.30

    # Enter a new put position based on signals.
    def _enter_position(self, candle: Any, ema20: float, vwap: Optional[float]) -> Dict[str, Any]:
        option_label = self._select_put_option(candle.c)
        if not option_label:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="option_selection",
                reason="no_put_option",
            )
            return {
                "entered": False,
                "option_label": None,
                "entry_price_available": False,
                "order_submitted": False,
            }
        entry_price = self.last_price.get(option_label)
        if entry_price is None:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="option_price",
                reason="entry_price_missing",
                option_label=option_label,
            )
            return {
                "entered": False,
                "option_label": option_label,
                "entry_price_available": False,
                "order_submitted": False,
            }
        qty = self._qty_for_label(option_label)
        risk = entry_price * self.config.option_sl_pct
        stop_price = entry_price - risk
        partial_tp = entry_price + risk * self.config.partial_tp_r
        final_tp = entry_price + risk * self.config.final_tp_r
        position_context = {
            "stop_price": float(stop_price),
            "partial_tp": float(partial_tp),
            "final_tp": float(final_tp),
            "breakdown_high": float(candle.h),
            "position_qty": int(qty),
            "bars_in_trade": 0,
        }
        resp = self._place_order(
            label=option_label,
            side="BUY",
            qty=qty,
            price=entry_price,
            tag="PUT_MOM_ENTRY",
            purpose=OrderPurpose.ENTRY,
            strategy_context=position_context,
        )
        status = str(getattr(resp, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            self._reject_entry(
                candle=candle,
                timeframe_seconds=self.timeframe_5m,
                gate="order",
                reason="entry_order_rejected",
                option_label=option_label,
                order_status=status,
                broker_message=getattr(resp, "message", ""),
            )
            return {
                "entered": False,
                "option_label": option_label,
                "entry_price_available": True,
                "order_submitted": False,
                "order_status": status,
            }
        self._remember_order_identity(option_label)
        broker_symbol, exchange, symbol_token = self._resolve_order_identity(option_label)
        self.state.position = OptionPosition(
            label=option_label,
            qty=qty,
            entry_price=entry_price,
            stop_price=stop_price,
            partial_tp=partial_tp,
            final_tp=final_tp,
            entry_time=self._now_ist(),
            entry_bar_index=len(self.state.bars_5m),
            breakdown_high=candle.h,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self._reset_exit_retry_state()
        self._adaptive_policy.mark_entry_committed()
        self._sync_position_state_to_risk_manager()
        logger.info(
            "[%s] ENTER PUT_MOM | opt=%s qty=%d entry=%.3f sl=%.3f tp1=%.3f tp2=%.3f ema20=%.3f vwap=%s",
            self.env_prefix,
            option_label,
            qty,
            entry_price,
            stop_price,
            partial_tp,
            final_tp,
            ema20,
            vwap,
        )
        self._emit_gate_decision(
            candle=candle,
            gate="order",
            passed=True,
            reason="entry_submitted",
            timeframe_seconds=self.timeframe_5m,
            option_label=option_label,
            entry_price=entry_price,
            qty=qty,
        )
        return {
            "entered": True,
            "option_label": option_label,
            "entry_price_available": True,
            "order_submitted": True,
            "entry_price": entry_price,
            "qty": qty,
            "broker_symbol": broker_symbol,
        }

    # Invalidate position if underlying reverses.
    def _maybe_invalidate(self, candle: Any, ema20: Optional[float]) -> None:
        pos = self.state.position
        if not pos:
            return
        if candle.c > pos.breakdown_high:
            self._exit_position("INVALID_HIGH")
            return
        if ema20 is not None and candle.c > ema20:
            self._exit_position("EMA_FAIL")

    # Exit if the trade has exceeded max bars in trade.
    def _maybe_time_stop(self, *, max_bars_in_trade: Optional[int] = None) -> None:
        pos = self.state.position
        if not pos:
            return
        bars_since_entry = len(self.state.bars_5m) - pos.entry_bar_index
        limit = (
            int(self.config.max_bars_in_trade)
            if max_bars_in_trade is None
            else int(max_bars_in_trade)
        )
        if bars_since_entry >= max(1, limit):
            self._exit_position("TIME_STOP")

    # Exit the current position with a reason.
    def _exit_position(self, reason: str) -> None:
        pos = self.state.position
        if not pos:
            return
        if self._exit_in_flight:
            return
        now_mono = monotonic()
        if now_mono < self._exit_circuit_open_until_mono:
            if now_mono - self._exit_last_alert_mono >= self._exit_circuit_alert_interval_seconds:
                self._exit_last_alert_mono = now_mono
                remaining = max(0.0, self._exit_circuit_open_until_mono - now_mono)
                logger.error(
                    "[%s][ALERT] PUT_MOM exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.label,
                    pos.broker_symbol or pos.label,
                    self._exit_failure_count,
                    self._exit_max_retries,
                    remaining,
                )
            return
        if now_mono < self._next_exit_retry_mono:
            return
        ltp = self.last_price.get(pos.label, pos.entry_price)
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=pos.label,
                side="SELL",
                qty=pos.qty,
                price=ltp,
                tag=f"PUT_MOM_EXIT_{reason}",
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] PUT_MOM exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.label,
                    pos.broker_symbol or pos.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] Exit exception for %s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        finally:
            self._exit_in_flight = False
        status = str(getattr(resp, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.error(
                    "[%s][ALERT] PUT_MOM exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.label,
                    pos.broker_symbol or pos.label,
                    reason,
                    status,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.warning(
                    "[%s] Exit rejected for %s: %s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.label,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state()
        logger.info(
            "[%s] EXIT PUT_MOM | opt=%s qty=%d price=%.3f reason=%s",
            self.env_prefix,
            pos.label,
            pos.qty,
            ltp,
            reason,
        )
        self.state.position = None
        self._adaptive_policy.mark_position_closed()

    # Force exit any open position.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.state.position:
            return
        if submit_orders:
            self._exit_position(reason=reason)
        else:
            self.state.position = None
            self._adaptive_policy.mark_position_closed()
            self._reset_exit_retry_state()


__all__ = ["PutMomentumScalperStrategy", "PutMomentumScalperConfig"]
