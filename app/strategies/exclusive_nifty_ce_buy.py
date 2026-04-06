"""
Exclusive Nifty CE buy strategy mirroring the rules from Nifty_strategy_and_backtest.py.
"""

from __future__ import annotations

import logging
import math
import os
import re
from time import monotonic
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Deque, Dict, Optional

import numpy as np

from pathlib import Path

from app.strategies.base import BaseStrategy
from app.config.boot_config import StrategyValueResolver
from app.config.settings import get_settings
from app.strategies.adaptive.adapter import AdaptivePolicyAdapter, OverrideRule
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import EXCLUSIVE_NIFTY_CE_BUY_ID
from app.strategies.exclusive_indicator_store import (
    EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN,
    IndicatorSessionRow,
    build_exclusive_nifty_ce_indicator_store,
)
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
_ORDER_TAG_MAX_LEN = 20
_ENTRY_TAG = "XNCB_ENTRY"
_EXIT_TAG_PREFIX = "XNCB_EXIT_"


# State for an open CE position.
@dataclass
class PositionState:
    option_label: str
    qty: int
    entry_option_price: float
    entry_underlying_price: float
    entry_atr: float
    sl_price: float
    target_price: float
    entry_time: datetime
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# Rolling indicator buffers for signal generation.
@dataclass
class IndicatorBuffers:
    closes: Deque[float] = field(default_factory=lambda: deque(maxlen=300))
    ret1: Deque[float] = field(default_factory=lambda: deque(maxlen=300))
    vol20: Deque[float] = field(default_factory=lambda: deque(maxlen=300))
    rsi: Deque[float] = field(default_factory=lambda: deque(maxlen=5))
    macd_div: Deque[float] = field(default_factory=lambda: deque(maxlen=5))
    macd_hist: Deque[float] = field(default_factory=lambda: deque(maxlen=5))


# Overall strategy state for entries/exits.
@dataclass
class StrategyState:
    position: Optional[PositionState] = None
    cooldown_bars: int = 0
    ema_fail_count: int = 0
    pending_entry_at: Optional[datetime] = None
    pending_entry_atr: Optional[float] = None
    pending_entry_underlying: Optional[float] = None
    vol_threshold: Optional[float] = None
    last_reset_date: Optional[date] = None
    trades_today: int = 0


# CE buy strategy aligned with the Nifty backtest rules.
class ExclusiveNiftyCeBuyStrategy(BaseStrategy):
    """
    Intraday CE buy strategy for NIFTY based on the standalone Nifty backtest logic.
    """

    # Initialize strategy configuration, buffers, and state.
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
        cfg = params or {}
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg,
            env=dict(os.environ),
        )

        # Default to 30-second bars; adjust to your feed cadence as needed.
        self.timeframe_seconds = int(cfg.get("timeframe_seconds", 30)) or 30

        # Signal parameters (copied from Nifty_strategy_and_backtest.py)
        self.vol_quantile = float(cfg.get("vol_quantile", 0.45))
        self.ema_atr_buffer = float(cfg.get("ema_atr_buffer", 0.05))
        self.rsi_min = float(cfg.get("rsi_min", 58))
        self.rsi_max = float(cfg.get("rsi_max", 72))
        self.macd_hist_min = float(cfg.get("macd_hist_min", 0.30))
        self.allow_near_macd = bool(cfg.get("allow_near_macd", True))
        self.macd_near = float(cfg.get("macd_near", 0.40))
        self.strict_trend = bool(cfg.get("strict_trend", False))
        self.min_adx = float(cfg.get("min_adx", 20.0))
        self.min_di_spread = float(cfg.get("min_di_spread", 5.0))

        # Trade management parameters (copied from backtest settings)
        self.sl_atr = float(cfg.get("sl_atr", 2.2))
        self.tp_atr = float(cfg.get("tp_atr", 2.5))
        self.ema_fail_bars = int(cfg.get("ema_fail_bars", 3))
        self.ema_fail_buffer_atr = float(cfg.get("ema_fail_buffer_atr", 0.10))
        self.trail_active_atr = float(cfg.get("trail_active_atr", 0.8))
        self.trail_cushion_atr = float(cfg.get("trail_cushion_atr", 0.16))
        self.cooldown_bars_cfg = int(cfg.get("cooldown_bars", 2))
        self.max_trades_per_day = int(cfg.get("max_trades_per_day", 0))  # 0 = unlimited
        self.cost_points = float(cfg.get("cost_points", 0.0))
        self.min_atr = float(cfg.get("min_atr", 0.0))
        self.session_start = self._parse_time(cfg.get("session_start", "09:30"))
        self.last_entry_time = self._parse_time(cfg.get("last_entry_time", "14:45"))
        self.squareoff_time = self._parse_time(cfg.get("squareoff_time", "15:15"))
        self.late_start = self._parse_time(cfg.get("late_start", "14:45"))
        self.late_tp_cap_atr = float(cfg.get("late_tp_cap_atr", 2.6))
        self.late_trail_cushion = float(cfg.get("late_trail_cushion", 0.08))
        self.late_trail_active_atr = float(cfg.get("late_trail_active_atr", 0.6))

        self.lots_per_trade = int(cfg.get("lots_per_trade", 1))
        # Always buy ATM CE on entry (ignore ITM/OTM configs to keep behaviour aligned with backtest).
        self.moneyness = "ATM"
        self.itm_strike_offset = 0
        self.product_type = str(cfg.get("product_type", "INTRADAY")).upper()
        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()
        dynamic_policy_raw = cfg.get("dynamic_policy", {})
        if not isinstance(dynamic_policy_raw, dict):
            dynamic_policy_raw = {}
        self._adaptive_policy = AdaptivePolicyAdapter(
            strategy_name="exclusive_nifty_ce_buy",
            env_prefix=self.env_prefix,
            cfg=self._cfg,
            dynamic_policy_raw=dynamic_policy_raw,
            base_params=self._base_policy_params(),
            extra_rules={
                "rsi_min": OverrideRule(float, 0.0, 100.0),
                "rsi_max": OverrideRule(float, 0.0, 100.0),
                "vol_quantile": OverrideRule(float, 0.0, 1.0),
                "macd_hist_min": OverrideRule(float, -100.0, 100.0),
                "ema_atr_buffer": OverrideRule(float, 0.0, 10.0),
                "strict_trend": OverrideRule(bool),
                "min_adx": OverrideRule(float, 0.0, 100.0),
                "min_di_spread": OverrideRule(float, 0.0, 100.0),
                "sl_atr": OverrideRule(float, 0.5, 5.0),
                "tp_atr": OverrideRule(float, 0.5, 5.0),
            },
            extra_entry_freeze_keys={
                "rsi_min",
                "rsi_max",
                "macd_hist_min",
                "ema_atr_buffer",
                "min_atr",
                "min_adx",
                "min_di_spread",
                "sl_atr",
                "tp_atr",
            },
            context_builder_kwargs={
                "ema_period": 20,
                "min_median_samples": 10,
            },
            dynamic_enabled_env_key="EXCLUSIVE_CE_DYNAMIC_POLICY_ENABLED",
            dynamic_policy_id_env_key="EXCLUSIVE_CE_DYNAMIC_POLICY_ID",
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

        self._strategy_id = EXCLUSIVE_NIFTY_CE_BUY_ID
        self.state = StrategyState()
        self.buffers = IndicatorBuffers()
        self._session_seed_date: Optional[date] = None
        self._private_ema_ready_logged_date: Optional[date] = None
        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        retry_cfg = self._cfg.get_float(
            "EXCLUSIVE_CE_EXIT_RETRY_COOLDOWN_SECONDS",
            5.0,
        )
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_int("EXCLUSIVE_CE_EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("EXCLUSIVE_CE_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("EXCLUSIVE_CE_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0
        entry_skip_log_cfg = self._cfg.get_float(
            "EXCLUSIVE_CE_ENTRY_SKIP_LOG_THROTTLE_SECONDS",
            60.0,
        )
        self._entry_skip_log_throttle_seconds = max(0.0, entry_skip_log_cfg)
        self._last_entry_skip_log_mono: Dict[str, float] = {}
        self.prev_macd: Optional[float] = None
        self.prev_macd_signal: Optional[float] = None
        self.last_atr: Optional[float] = None
        self._private_ema_period = 20
        self._private_ema_column = EXCLUSIVE_NIFTY_CE_BUY_EMA20_30S_COLUMN
        self._private_indicator_store = build_exclusive_nifty_ce_indicator_store(
            enable_backfill=True,
        )
        self.preseed_history = bool(cfg.get("preseed_history", True))
        self.preseed_history_path = cfg.get("preseed_history_path")
        self.preseed_history_rows = int(cfg.get("preseed_history_rows", 400))
        self._restore_position_from_risk_manager()

        logger.info(
            "[%s] ExclusiveNiftyCeBuy init | tf=%ds sl=%.2f tp=%.2f vol_q=%.2f",
            self.env_prefix,
            self.timeframe_seconds,
            self.sl_atr,
            self.tp_atr,
            self.vol_quantile,
        )
        logger.info(
            "[%s] EXCLUSIVE_CE exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        if self._adaptive_policy.enabled:
            logger.info(
                "[%s] EXCLUSIVE_CE dynamic policy enabled | policy_id=%s policy_hash=%s",
                self.env_prefix,
                self._adaptive_policy.policy_config.policy_id or "",
                self._adaptive_policy.policy_config.policy_hash,
            )
        if self._private_indicator_store is not None:
            logger.info(
                "[%s] EXCLUSIVE_CE private EMA store enabled | table=%s column=%s",
                self.env_prefix,
                self._private_indicator_store.table_name,
                self._private_indicator_store.ema_column,
            )
        else:
            log_fn = logger.warning if self.trade_mode == "LIVE" else logger.info
            log_fn(
                "[%s] EXCLUSIVE_CE private EMA store unavailable | warm starts will be cold unless CSV fallback exists",
                self.env_prefix,
            )

    # ---- helpers ----

    # Build compact broker order tags that always fit SmartAPI limits.
    def _build_order_tag(self, *, is_entry: bool, reason: Optional[str] = None) -> str:
        if is_entry:
            return _ENTRY_TAG
        code = self._compact_reason_code(reason or "EXIT")
        max_reason_len = max(1, _ORDER_TAG_MAX_LEN - len(_EXIT_TAG_PREFIX))
        return f"{_EXIT_TAG_PREFIX}{code[:max_reason_len]}"

    # Normalize verbose exit reasons to a short stable code.
    @staticmethod
    def _compact_reason_code(reason: str) -> str:
        mapping = {
            "SL_ATR": "SL",
            "TP_ATR": "TP",
            "TRAIL_EMA20": "TRAIL",
            "EMA20_FAIL_CONFIRMED": "EMAFAIL",
            "EOD_SQUAREOFF_1515": "EOD",
            "EOD_SQUARE_OFF": "EOD",
            "FORCED_EXIT": "FORCE",
        }
        key = str(reason or "").strip().upper()
        normalized = mapping.get(key, key or "EXIT")
        normalized = re.sub(r"[^A-Z0-9]+", "_", normalized).strip("_")
        return normalized or "EXIT"

    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    # Parse a HH:MM time value.
    def _parse_time(self, val: Any) -> time:
        try:
            h, m = (int(p) for p in str(val).split(":"))
            return time(hour=h, minute=m)
        except Exception:
            return time(9, 30)

    def _base_policy_params(self) -> Dict[str, Any]:
        return {
            "disable_entries": False,
            "qty_mult": 1.0,
            "min_atr": float(self.min_atr),
            "rsi_min": float(self.rsi_min),
            "rsi_max": float(self.rsi_max),
            "vol_quantile": float(self.vol_quantile),
            "macd_hist_min": float(self.macd_hist_min),
            "ema_atr_buffer": float(self.ema_atr_buffer),
            "strict_trend": bool(self.strict_trend),
            "min_adx": float(self.min_adx),
            "min_di_spread": float(self.min_di_spread),
            "sl_atr": float(self.sl_atr),
            "tp_atr": float(self.tp_atr),
        }

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

    def _log_entry_skip(self, *, reason: str, candle: Any, **extra: Any) -> None:
        now_mono = monotonic()
        throttle = self._entry_skip_log_throttle_seconds
        last_mono = self._last_entry_skip_log_mono.get(reason, 0.0)
        if throttle > 0.0 and (now_mono - last_mono) < throttle:
            return
        self._last_entry_skip_log_mono[reason] = now_mono
        detail_parts = []
        for key, value in extra.items():
            if value in (None, "", [], {}):
                continue
            detail_parts.append(f"{key}={value}")
        details = f" {' '.join(detail_parts)}" if detail_parts else ""
        logger.info(
            "[%s] EXCLUSIVE_CE entry skipped | reason=%s bar_start_ts=%s%s",
            self.env_prefix,
            reason,
            getattr(candle, "start_ts", None),
            details,
        )

    # Return current IST time.
    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    # Reset state for a new trading day.
    def _reset_if_new_day(self, current_dt: datetime) -> None:
        d = current_dt.date()
        if self.state.last_reset_date == d:
            return
        self.state = StrategyState(last_reset_date=d)
        self.buffers = IndicatorBuffers()
        self._session_seed_date = None
        self._private_ema_ready_logged_date = None
        self.prev_macd = None
        self.prev_macd_signal = None
        self.last_atr = None
        self._last_entry_skip_log_mono = {}
        self._adaptive_policy.reset_runtime()
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

    def _position_strategy_context(self, pos: PositionState) -> Dict[str, Any]:
        return {
            "entry_underlying_price": float(pos.entry_underlying_price),
            "entry_option_price": float(pos.entry_option_price),
            "entry_atr": float(pos.entry_atr),
            "sl_price": float(pos.sl_price),
            "target_price": float(pos.target_price),
            "position_qty": int(pos.qty),
            "ema_fail_count": int(self.state.ema_fail_count),
        }

    def _sync_position_state_to_risk_manager(self) -> None:
        pos = self.state.position
        if not pos:
            return
        sync_open_position_state(
            risk_manager=self.risk_manager,
            label=pos.option_label,
            side="BUY",
            qty=max(1, int(pos.qty)),
            entry_price=float(pos.entry_option_price),
            strategy_context=self._position_strategy_context(pos),
        )

    def _restore_position_from_risk_manager(self) -> None:
        if self.state.position is not None or self.risk_manager is None:
            return
        match = find_restored_position(
            risk_manager=self.risk_manager,
            instrument_meta=self.instrument_meta,
            strategy_id=self._strategy_id,
            canonical_strategy_name="exclusive_nifty_ce_buy",
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
        entry_option_price = parse_non_negative_float(entry.get("entry_price"))
        entry_underlying_price = parse_non_negative_float(
            strategy_context.get("entry_underlying_price")
        )
        entry_atr = parse_non_negative_float(strategy_context.get("entry_atr"))
        sl_price = parse_non_negative_float(strategy_context.get("sl_price"))
        target_price = parse_non_negative_float(strategy_context.get("target_price"))
        qty = parse_positive_int(strategy_context.get("position_qty")) or parse_positive_int(
            entry.get("qty")
        )
        if (
            entry_option_price is None
            or entry_underlying_price is None
            or entry_atr is None
            or sl_price is None
            or target_price is None
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
        self.state.position = PositionState(
            option_label=label,
            qty=qty,
            entry_option_price=entry_option_price,
            entry_underlying_price=entry_underlying_price,
            entry_atr=entry_atr,
            sl_price=sl_price,
            target_price=target_price,
            entry_time=restored_entry_time,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self.state.last_reset_date = restored_entry_time.date()
        self.state.trades_today = max(int(self.state.trades_today), 1)
        self.state.ema_fail_count = int(strategy_context.get("ema_fail_count") or 0)
        self.state.pending_entry_at = None
        self.state.pending_entry_atr = None
        self.state.pending_entry_underlying = None
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

    # Find the nearest valid expiry date.
    def _current_expiry(self) -> Optional[date]:
        today = self._now_ist().date()
        expiries = []
        for meta in self.instrument_meta.values():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            kind = str(meta.get("kind", "")).upper()
            if "CE" not in kind:
                continue
            exp_dt = self._parse_expiry(meta.get("expiry") or meta.get("expiry_dt"))
            if exp_dt and exp_dt >= today:
                expiries.append(exp_dt)
        if not expiries:
            return None
        return min(expiries)

    # Select the CE option label based on spot and expiry.
    def _select_call_option(self, spot: float) -> Optional[str]:
        current_expiry = self._current_expiry()
        candidates = []
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            kind = str(meta.get("kind", "")).upper()
            if "CE" not in kind:
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
                    "[%s] No CE candidates matching expiry=%s", self.env_prefix, current_expiry
                )
            return None
        candidates.sort(key=lambda x: x[1])
        strikes = [s for _, s in candidates]
        diffs = [b - a for a, b in zip(strikes[:-1], strikes[1:]) if (b - a) > 0]
        strike_step = diffs[0] if diffs else 50.0
        atm_label, atm_strike = min(candidates, key=lambda x: abs(x[1] - spot))
        return atm_label

    # Resolve quantity to trade for a label.
    def _qty_for_label(self, label: str) -> int:
        qty_mult = self._adaptive_policy.get_float("qty_mult", 1.0, minimum=0.01)
        return max(1, int(round(float(self.lots_per_trade) * float(qty_mult))))

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

    # Check if current time is within entry window.
    def _within_entry_window(self, t: time) -> bool:
        if self.session_start and t < self.session_start:
            return False
        if self.last_entry_time and t > self.last_entry_time:
            return False
        if self.squareoff_time and t >= self.squareoff_time:
            return False
        return True

    # ---- indicator helpers ----

    # Compute EMA from a sequence of closes.
    def _compute_ema(self, values: Any, period: int) -> Optional[float]:
        seq = [float(v) for v in values]
        if len(seq) < period:
            return None
        k = 2.0 / (period + 1.0)
        ema_val = seq[0]
        for price in seq:
            ema_val = price * k + ema_val * (1.0 - k)
        return ema_val

    # Compute EMA or fall back to indicator values.
    def _ema_with_fallback(
        self, period: int, closes: Deque[float], indicators: Dict[str, Any]
    ) -> Optional[float]:
        ema_val = self._compute_ema(closes, period)
        if ema_val is not None:
            return ema_val
        key = f"ema_{period}"
        if key in indicators and indicators.get(key) is not None:
            try:
                return float(indicators[key])
            except Exception:
                return None
        return None

    def _build_private_indicator_overlay(
        self,
        candle: Any,
        indicators: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[float]]:
        overlay = dict(indicators)
        close_preview = list(self.buffers.closes)
        close_preview.append(float(candle.c))
        ema_val = self._compute_ema(close_preview, self._private_ema_period)
        if ema_val is not None:
            overlay["ema_20"] = float(ema_val)
            overlay[self._private_ema_column] = float(ema_val)
        return overlay, ema_val

    def _seed_row_indicators(self, row: IndicatorSessionRow) -> Dict[str, Any]:
        indicators: Dict[str, Any] = {
            "atr": row.atr,
            "rsi": row.rsi,
            "macd": row.macd,
            "macd_signal": row.macd_signal,
            "macd_hist": row.macd_hist,
            "adx": row.adx,
            "plus_di": row.plus_di,
            "minus_di": row.minus_di,
            "di_spread": row.di_spread,
        }
        if row.private_ema20 is not None:
            indicators["ema_20"] = float(row.private_ema20)
            indicators[self._private_ema_column] = float(row.private_ema20)
        return indicators

    def _seed_from_session_rows(self, rows: list[IndicatorSessionRow]) -> int:
        if not rows:
            return 0
        self.buffers = IndicatorBuffers()
        self.prev_macd = None
        self.prev_macd_signal = None
        self.last_atr = None
        seeded = 0
        for row in rows:
            candle = SimpleNamespace(
                start_ts=row.ts_start,
                end_ts=row.ts_end or (row.ts_start + timedelta(seconds=self.timeframe_seconds)),
                o=row.o,
                h=row.h,
                l=row.l,
                low=row.l,
                c=row.c,
            )
            indicators = self._seed_row_indicators(row)
            self._update_buffers(
                candle,
                indicators,
                vol_quantile=self.vol_quantile,
            )
            if "ema_20" not in indicators or indicators.get("ema_20") is None:
                ema_val = self._compute_ema(self.buffers.closes, self._private_ema_period)
                if ema_val is not None:
                    indicators["ema_20"] = float(ema_val)
                    indicators[self._private_ema_column] = float(ema_val)
            self._adaptive_policy.set_base_params(self._base_policy_params())
            self._adaptive_policy.refresh(
                candle=candle,
                indicators=indicators,
                position_open=False,
            )
            self.last_atr = indicators.get("atr")
            self.prev_macd = indicators.get("macd")
            self.prev_macd_signal = indicators.get("macd_signal")
            seeded += 1
        return seeded

    def _ensure_session_warm_start(
        self,
        current_dt: datetime,
        *,
        before_ts: Optional[datetime] = None,
    ) -> None:
        session_date = current_dt.date()
        if self._session_seed_date == session_date:
            return

        rows_loaded = 0
        source = "cold"
        if self._private_indicator_store is not None:
            try:
                session_rows = self._private_indicator_store.load_session_rows(
                    label=self.underlying_label,
                    timeframe_seconds=self.timeframe_seconds,
                    session_date=session_date,
                    before_ts=before_ts,
                    max_rows=self.preseed_history_rows,
                )
            except Exception as exc:
                session_rows = []
                logger.warning(
                    "[%s] EXCLUSIVE_CE warm start query failed: %s",
                    self.env_prefix,
                    exc,
                )
            rows_loaded = self._seed_from_session_rows(session_rows)
            if rows_loaded > 0:
                source = "postgres"
        if rows_loaded == 0 and self.preseed_history and self.trade_mode != "LIVE":
            if self._preseed_from_history():
                rows_loaded = len(self.buffers.closes)
                source = "csv"

        self._session_seed_date = session_date
        private_ready = (
            rows_loaded > 0
            and self._compute_ema(self.buffers.closes, self._private_ema_period) is not None
        )
        log_fn = logger.warning if self.trade_mode == "LIVE" and source == "cold" else logger.info
        log_fn(
            "[%s] EXCLUSIVE_CE warm start | source=%s rows=%d session=%s private_ema_ready=%s",
            self.env_prefix,
            source,
            rows_loaded,
            session_date.isoformat(),
            bool(private_ready),
        )

    def _persist_private_ema(
        self,
        *,
        candle: Any,
        ema_value: Optional[float],
    ) -> None:
        if self._private_indicator_store is None or ema_value is None:
            return
        try:
            self._private_indicator_store.upsert_private_ema(
                label=self.underlying_label,
                timeframe_seconds=self.timeframe_seconds,
                ts_start=candle.start_ts,
                ts_end=getattr(candle, "end_ts", None),
                ema_value=float(ema_value),
            )
        except Exception as exc:
            logger.warning(
                "[%s] EXCLUSIVE_CE private EMA persist failed | ts=%s error=%s",
                self.env_prefix,
                getattr(candle, "start_ts", None),
                exc,
            )

    # Preload historical data to seed buffers.
    def _preseed_from_history(self) -> bool:
        """
        Preload historical bars to seed EMA/volatility buffers so signals can fire sooner.
        """
        candidate_paths = []
        if self.preseed_history_path:
            candidate_paths.append(Path(self.preseed_history_path))
        root = Path(__file__).resolve().parents[2]
        candidate_paths.append(root / "datasets" / "historical_nifty_labeled.csv")
        candidate_paths.append(root / "datasets" / "history_nifty_labelled.csv")
        path = next((p for p in candidate_paths if p.exists()), None)
        if not path:
            return False
        try:
            import pandas as pd
        except Exception:
            logger.warning("[%s] pandas not available; cannot preseed history", self.env_prefix)
            return False
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            logger.warning("[%s] Failed to read history for preseeding: %s", self.env_prefix, exc)
            return False
        if len(df) == 0:
            return False
        df = df.tail(max(1, self.preseed_history_rows))
        close_col = "close" if "close" in df.columns else "c" if "c" in df.columns else None
        if close_col is None:
            return False
        closes = df[close_col].dropna().tolist()
        for c in closes:
            try:
                self.buffers.closes.append(float(c))
            except Exception:
                continue
        if "ret_1" in df.columns:
            for v in df["ret_1"].dropna().tolist():
                try:
                    self.buffers.ret1.append(float(v))
                except Exception:
                    continue
        if "vol_20" in df.columns:
            vol_vals = [float(x) for x in df["vol_20"].dropna().tolist()]
            for v in vol_vals:
                self.buffers.vol20.append(v)
            try:
                if vol_vals:
                    self.state.vol_threshold = float(np.quantile(vol_vals, self.vol_quantile))
            except Exception:
                pass
        if "rsi_14" in df.columns:
            for v in df["rsi_14"].dropna().tolist():
                try:
                    self.buffers.rsi.append(float(v))
                except Exception:
                    continue
        if all(col in df.columns for col in ("macd", "macd_signal")):
            macd_vals = df[["macd", "macd_signal"]].dropna().values.tolist()
            for macd, macd_signal in macd_vals:
                try:
                    self.buffers.macd_div.append(float(macd - macd_signal))
                except Exception:
                    continue
        if "macd_hist" in df.columns:
            for v in df["macd_hist"].dropna().tolist():
                try:
                    self.buffers.macd_hist.append(float(v))
                except Exception:
                    continue
        logger.info(
            "[%s] Preseeded history from %s | closes=%d ret1=%d vol20=%d rsi=%d",
            self.env_prefix,
            path.name,
            len(self.buffers.closes),
            len(self.buffers.ret1),
            len(self.buffers.vol20),
            len(self.buffers.rsi),
        )
        return True

    # Compute log return over N bars.
    def _ret_n(self, n: int) -> Optional[float]:
        closes = self.buffers.closes
        if len(closes) < n + 1:
            return None
        try:
            return math.log(closes[-1] / closes[-(n + 1)])
        except Exception:
            return None

    # Update rolling buffers with the latest bar.
    def _update_buffers(
        self,
        candle: Any,
        indicators: Dict[str, Any],
        *,
        vol_quantile: Optional[float] = None,
    ) -> None:
        close_val = float(candle.c)
        closes = self.buffers.closes
        closes.append(close_val)

        if len(closes) >= 2 and closes[-2] > 0:
            try:
                ret1 = math.log(closes[-1] / closes[-2])
                self.buffers.ret1.append(ret1)
            except Exception:
                pass

        if len(self.buffers.ret1) >= 20:
            vol = float(np.std(list(self.buffers.ret1)[-20:], ddof=0))
            self.buffers.vol20.append(vol)
            try:
                quantile = (
                    float(vol_quantile)
                    if vol_quantile is not None
                    else float(self.vol_quantile)
                )
                self.state.vol_threshold = float(
                    np.quantile(list(self.buffers.vol20), quantile)
                )
            except Exception:
                pass

        rsi_val = indicators.get("rsi")
        if rsi_val is not None:
            self.buffers.rsi.append(float(rsi_val))

        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        macd_hist = indicators.get("macd_hist")
        if macd is not None and macd_signal is not None:
            self.buffers.macd_div.append(float(macd - macd_signal))
        if macd_hist is not None:
            self.buffers.macd_hist.append(float(macd_hist))

    # Compute the buy signal and diagnostics.
    def _compute_buy_signal(
        self,
        indicators: Dict[str, Any],
        *,
        rsi_min: Optional[float] = None,
        rsi_max: Optional[float] = None,
        macd_hist_min: Optional[float] = None,
        ema_atr_buffer: Optional[float] = None,
        strict_trend: Optional[bool] = None,
        min_adx: Optional[float] = None,
        min_di_spread: Optional[float] = None,
    ) -> tuple[bool, Dict[str, Any]]:
        atr = indicators.get("atr")
        rsi = indicators.get("rsi")
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        macd_hist = indicators.get("macd_hist")
        closes = self.buffers.closes
        live_rsi_min = float(self.rsi_min if rsi_min is None else rsi_min)
        live_rsi_max = float(self.rsi_max if rsi_max is None else rsi_max)
        live_macd_hist_min = float(
            self.macd_hist_min if macd_hist_min is None else macd_hist_min
        )
        live_ema_atr_buffer = float(
            self.ema_atr_buffer if ema_atr_buffer is None else ema_atr_buffer
        )
        live_strict_trend = bool(
            self.strict_trend if strict_trend is None else strict_trend
        )

        ema20 = self._ema_with_fallback(20, closes, indicators)
        ema50 = self._ema_with_fallback(50, closes, indicators)
        ema200 = self._ema_with_fallback(200, closes, indicators)
        ret_1 = self.buffers.ret1[-1] if self.buffers.ret1 else None
        ret_5 = self._ret_n(5)
        vol_20 = self.buffers.vol20[-1] if self.buffers.vol20 else None
        vol_thr = self.state.vol_threshold

        prev_macd_div = self.buffers.macd_div[-2] if len(self.buffers.macd_div) >= 2 else None
        prev_macd_hist = self.buffers.macd_hist[-2] if len(self.buffers.macd_hist) >= 2 else None

        ready = all(
            val is not None
            for val in (atr, rsi, macd, macd_signal, macd_hist, ema20, ema50, ret_1, ret_5, vol_20, vol_thr)
        )
        if not ready:
            return False, {"ready": False}
        if live_strict_trend and ema200 is None:
            return False, {"ready": False}

        macd_div = macd - macd_signal  # type: ignore[operator]
        macd_cross_up = (
            self.prev_macd is not None
            and self.prev_macd_signal is not None
            and self.prev_macd <= self.prev_macd_signal
            and macd > macd_signal  # type: ignore[operator]
        )
        macd_near_cross_up = (
            macd_div < 0
            and macd_div > -self.macd_near
            and (prev_macd_div is not None and macd_div > prev_macd_div)
            and (prev_macd_hist is not None and macd_hist > prev_macd_hist)  # type: ignore[operator]
        )
        macd_confirmed = macd_cross_up and macd_hist >= live_macd_hist_min  # type: ignore[operator]
        macd_ok = macd_confirmed or (self.allow_near_macd and macd_near_cross_up)

        rsi_vals = list(self.buffers.rsi)
        rsi_rising = (
            len(rsi_vals) >= 3 and rsi_vals[-1] > rsi_vals[-2] and rsi_vals[-2] > rsi_vals[-3]
        )
        rsi_ok = live_rsi_min < rsi < live_rsi_max  # type: ignore[operator]

        if live_strict_trend:
            trend_ok = ema20 > ema50 and ema50 > ema200  # type: ignore[operator]
        else:
            trend_ok = ema20 > ema50  # type: ignore[operator]

        above_ema20 = ema20 is not None and atr is not None and closes[-1] > (ema20 + live_ema_atr_buffer * atr)
        mom_ok = (ret_1 > 0) and (ret_5 > 0)  # type: ignore[operator]
        vol_ok = vol_thr is not None and vol_20 is not None and vol_20 >= vol_thr

        # ADX / DI directional filters
        adx_val = indicators.get("adx")
        plus_di_val = indicators.get("plus_di")
        minus_di_val = indicators.get("minus_di")
        live_min_adx = float(self.min_adx if min_adx is None else min_adx)
        live_min_di_spread = float(self.min_di_spread if min_di_spread is None else min_di_spread)

        adx_ok = adx_val is not None and float(adx_val) >= live_min_adx
        di_spread_val = (
            abs(float(plus_di_val) - float(minus_di_val))
            if (plus_di_val is not None and minus_di_val is not None)
            else None
        )
        di_ok = di_spread_val is not None and di_spread_val >= live_min_di_spread
        di_bullish = (
            plus_di_val is not None
            and minus_di_val is not None
            and float(plus_di_val) > float(minus_di_val)
        )

        buy_signal = bool(
            vol_ok
            and trend_ok
            and rsi_rising
            and rsi_ok
            and above_ema20
            and macd_ok
            and mom_ok
            and adx_ok
            and di_ok
            and di_bullish
        )

        # Roll previous MACD values for next bar
        self.prev_macd = macd  # type: ignore[assignment]
        self.prev_macd_signal = macd_signal  # type: ignore[assignment]

        return buy_signal, {
            "vol_ok": vol_ok,
            "trend_ok": trend_ok,
            "rsi_rising": rsi_rising,
            "rsi_ok": rsi_ok,
            "above_ema20": above_ema20,
            "macd_ok": macd_ok,
            "macd_confirmed": macd_confirmed,
            "macd_near": macd_near_cross_up,
            "mom_ok": mom_ok,
            "vol_thr": vol_thr,
            "vol_20": vol_20,
            "adx_ok": adx_ok,
            "di_ok": di_ok,
            "di_bullish": di_bullish,
            "adx": adx_val,
            "di_spread": di_spread_val,
        }

    # ---- lifecycle ----

    # Handle tick updates and manage pending entries/exits.
    def on_tick(self, label: str, ltp: float) -> None:
        self.last_price[label] = ltp
        now_ist = self._now_ist()
        self._reset_if_new_day(now_ist)
        state = self.state

        if state.position and self.squareoff_time and now_ist.time() >= self.squareoff_time:
            self._exit_position(reason="EOD_SQUAREOFF_1515", price=ltp if label == state.position.option_label else None)
            return

        # Pending entry triggers on the first tick after the signal bar
        if (
            label == self.underlying_label
            and state.pending_entry_at
            and now_ist >= state.pending_entry_at
            and not state.position
        ):
            if not self._within_entry_window(now_ist.time()):
                state.pending_entry_at = None
                state.pending_entry_atr = None
                state.pending_entry_underlying = None
                return
            self._enter_position(underlying_price=ltp, ts=now_ist)

    # Handle bar-close updates for signals and exits.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label or timeframe_seconds != self.timeframe_seconds:
            return

        ist_dt = candle.start_ts.astimezone(IST)
        self._reset_if_new_day(ist_dt)
        self._ensure_session_warm_start(
            ist_dt,
            before_ts=getattr(candle, "start_ts", None),
        )
        state = self.state
        self.last_atr = indicators.get("atr")
        self._adaptive_policy.set_base_params(self._base_policy_params())
        adaptive_indicators, private_ema20 = self._build_private_indicator_overlay(
            candle,
            indicators,
        )
        self._adaptive_policy.refresh(
            candle=candle,
            indicators=adaptive_indicators,
            position_open=bool(state.position),
        )
        self._persist_private_ema(candle=candle, ema_value=private_ema20)
        if private_ema20 is not None and self._private_ema_ready_logged_date != ist_dt.date():
            logger.info(
                "[%s] EXCLUSIVE_CE private EMA ready | session=%s ts=%s ema20=%.6f",
                self.env_prefix,
                ist_dt.date().isoformat(),
                getattr(candle, "start_ts", None),
                float(private_ema20),
            )
            self._private_ema_ready_logged_date = ist_dt.date()
        live_disable_entries = self._adaptive_policy.get_bool("disable_entries", False)
        live_min_atr = self._adaptive_policy.get_float("min_atr", self.min_atr, minimum=0.0)
        live_vol_quantile = self._adaptive_policy.get_float(
            "vol_quantile",
            self.vol_quantile,
            minimum=0.0,
        )
        live_rsi_min = self._adaptive_policy.get_float("rsi_min", self.rsi_min, minimum=0.0)
        live_rsi_max = self._adaptive_policy.get_float("rsi_max", self.rsi_max, minimum=0.0)
        live_macd_hist_min = self._adaptive_policy.get_float(
            "macd_hist_min",
            self.macd_hist_min,
            minimum=-100.0,
        )
        live_ema_atr_buffer = self._adaptive_policy.get_float(
            "ema_atr_buffer",
            self.ema_atr_buffer,
            minimum=0.0,
        )
        live_strict_trend = self._adaptive_policy.get_bool(
            "strict_trend",
            self.strict_trend,
        )
        live_min_adx = self._adaptive_policy.get_float("min_adx", self.min_adx, minimum=0.0)
        live_min_di_spread = self._adaptive_policy.get_float("min_di_spread", self.min_di_spread, minimum=0.0)

        # Update histories and manage exits before looking for fresh entries
        self._update_buffers(candle, indicators, vol_quantile=live_vol_quantile)
        self._manage_position_on_bar(candle, indicators)

        if state.cooldown_bars > 0:
            state.cooldown_bars -= 1

        if state.position:
            self._log_entry_skip(
                reason="position_open",
                candle=candle,
                option_label=state.position.option_label,
            )
            return

        if live_disable_entries:
            self._log_entry_skip(
                reason="policy_disable_entries",
                candle=candle,
                **self._dynamic_policy_skip_details(disable_entries=live_disable_entries),
            )
            return
        if self.max_trades_per_day > 0 and state.trades_today >= self.max_trades_per_day:
            self._log_entry_skip(
                reason="max_trades_reached",
                candle=candle,
                trades_today=state.trades_today,
                max_trades_per_day=self.max_trades_per_day,
            )
            return
        try:
            atr_now = float(indicators.get("atr"))
        except Exception:
            atr_now = None
        if atr_now is None or atr_now < live_min_atr:
            self._log_entry_skip(
                reason="min_atr_not_met",
                candle=candle,
                atr=atr_now,
                min_atr=live_min_atr,
            )
            return

        buy_signal, details = self._compute_buy_signal(
            indicators,
            rsi_min=live_rsi_min,
            rsi_max=live_rsi_max,
            macd_hist_min=live_macd_hist_min,
            ema_atr_buffer=live_ema_atr_buffer,
            strict_trend=live_strict_trend,
            min_adx=live_min_adx,
            min_di_spread=live_min_di_spread,
        )
        if not buy_signal:
            self._log_entry_skip(
                reason="no_buy_signal",
                candle=candle,
                signal_details=details,
            )
            return

        next_bar_start = candle.end_ts or (candle.start_ts + timedelta(seconds=self.timeframe_seconds))
        entry_time = next_bar_start.astimezone(IST).time()
        if not self._within_entry_window(entry_time):
            self._log_entry_skip(
                reason="outside_entry_window",
                candle=candle,
                entry_time=entry_time.strftime("%H:%M"),
                session_start=self.session_start.strftime("%H:%M"),
                last_entry_time=self.last_entry_time.strftime("%H:%M"),
            )
            return
        if state.cooldown_bars > 0:
            self._log_entry_skip(
                reason="cooldown_active",
                candle=candle,
                cooldown_bars=state.cooldown_bars,
            )
            return

        state.pending_entry_at = next_bar_start
        state.pending_entry_atr = indicators.get("atr")
        state.pending_entry_underlying = candle.c
        logger.info(
            "[%s] Buy signal | close=%.3f atr=%s vol_thr=%s details=%s",
            self.env_prefix,
            candle.c,
            indicators.get("atr"),
            state.vol_threshold,
            details,
        )

    # ---- internal actions ----

    # Enter a new CE position.
    def _enter_position(self, underlying_price: float, ts: datetime) -> None:
        state = self.state
        option_label = self._select_call_option(underlying_price)
        if not option_label:
            logger.warning("[%s] No CE option found for entry", self.env_prefix)
            state.pending_entry_at = None
            return

        qty = self._qty_for_label(option_label)
        option_price = self.last_price.get(option_label)
        if option_price is None:
            logger.info("[%s] No LTP for %s yet, skipping entry", self.env_prefix, option_label)
            return

        entry_atr = state.pending_entry_atr if state.pending_entry_atr is not None else self.last_atr
        if entry_atr is None or entry_atr <= 0:
            logger.info("[%s] ATR missing for entry, skipping", self.env_prefix)
            return

        late = self.late_start and ts.time() >= self.late_start
        tp_eff = self.tp_atr if not late else min(self.tp_atr, self.late_tp_cap_atr)
        sl_price = option_price - self.sl_atr * entry_atr
        target_price = option_price + tp_eff * entry_atr
        position_context = {
            "entry_underlying_price": float(underlying_price),
            "entry_option_price": float(option_price),
            "entry_atr": float(entry_atr),
            "sl_price": float(sl_price),
            "target_price": float(target_price),
            "position_qty": int(qty),
            "ema_fail_count": 0,
        }

        resp = self._place_order(
            label=option_label,
            side="BUY",
            qty=qty,
            price=option_price,
            tag=self._build_order_tag(is_entry=True),
            purpose=OrderPurpose.ENTRY,
            strategy_context=position_context,
        )
        status = str(getattr(resp, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            logger.warning(
                "[%s] Entry rejected for %s: %s",
                self.env_prefix,
                option_label,
                getattr(resp, "message", ""),
            )
            state.pending_entry_at = None
            state.pending_entry_atr = None
            state.pending_entry_underlying = None
            return

        state.trades_today += 1
        self._remember_order_identity(option_label)
        broker_symbol, exchange, symbol_token = self._resolve_order_identity(option_label)
        state.position = PositionState(
            option_label=option_label,
            qty=qty,
            entry_option_price=option_price,
            entry_underlying_price=underlying_price,
            entry_atr=entry_atr,
            sl_price=sl_price,
            target_price=target_price,
            entry_time=ts,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self._reset_exit_retry_state()
        state.ema_fail_count = 0
        state.pending_entry_at = None
        state.pending_entry_atr = None
        state.pending_entry_underlying = None
        self._adaptive_policy.mark_entry_committed()
        self._sync_position_state_to_risk_manager()
        logger.info(
            "[%s] ENTER Exclusive_Nifty_CE_Buy | opt=%s qty=%d entry=%.3f sl=%.3f tp=%.3f spot=%.3f",
            self.env_prefix,
            option_label,
            qty,
            option_price,
            sl_price,
            target_price,
            underlying_price,
        )

    # Manage exits and trailing logic on each bar.
    def _manage_position_on_bar(self, candle: Any, indicators: Dict[str, Any]) -> None:
        state = self.state
        pos = state.position
        if not pos:
            return

        ist_time = candle.start_ts.astimezone(IST).time()
        if self.squareoff_time and ist_time >= self.squareoff_time:
            self._exit_position(reason="EOD_SQUAREOFF_1515")
            return

        atr = pos.entry_atr if pos.entry_atr is not None else indicators.get("atr")
        ema20 = self._compute_ema(self.buffers.closes, 20)
        if atr is None or ema20 is None:
            return

        late = self.late_start and ist_time >= self.late_start
        tp_eff = self.tp_atr if not late else min(self.tp_atr, self.late_tp_cap_atr)
        trail_active_atr = self.trail_active_atr if not late else self.late_trail_active_atr
        trail_cushion = self.trail_cushion_atr if not late else self.late_trail_cushion

        sl_level = pos.entry_underlying_price - self.sl_atr * pos.entry_atr
        tp_level = pos.entry_underlying_price + tp_eff * pos.entry_atr
        trail_active_level = pos.entry_underlying_price + trail_active_atr * pos.entry_atr
        trail_level = ema20 - trail_cushion * atr

        exit_reason = None
        if candle.low <= sl_level:
            exit_reason = "SL_ATR"
        elif candle.h >= tp_level:
            exit_reason = "TP_ATR"
        elif candle.h >= trail_active_level and candle.low < trail_level:
            exit_reason = "TRAIL_EMA20"
        else:
            previous_ema_fail_count = state.ema_fail_count
            if candle.c < (ema20 - self.ema_fail_buffer_atr * atr):
                state.ema_fail_count += 1
            else:
                state.ema_fail_count = 0
            if state.ema_fail_count != previous_ema_fail_count:
                self._sync_position_state_to_risk_manager()
            if state.ema_fail_count >= self.ema_fail_bars:
                exit_reason = "EMA20_FAIL_CONFIRMED"

        if exit_reason:
            self._exit_position(reason=exit_reason)

    # Exit the current position with a reason.
    def _exit_position(self, reason: str, price: Optional[float] = None) -> None:
        state = self.state
        pos = state.position
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
                    "[%s][ALERT] EXCLUSIVE_CE exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    pos.broker_symbol or pos.option_label,
                    self._exit_failure_count,
                    self._exit_max_retries,
                    remaining,
                )
            return
        if now_mono < self._next_exit_retry_mono:
            return
        ltp = price if price is not None else self.last_price.get(pos.option_label, pos.entry_option_price)
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=pos.option_label,
                side="SELL",
                qty=pos.qty,
                price=ltp,
                tag=self._build_order_tag(is_entry=False, reason=reason),
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] EXCLUSIVE_CE exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    pos.broker_symbol or pos.option_label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] Exit exception for %s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.option_label,
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
                    "[%s][ALERT] EXCLUSIVE_CE exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    pos.broker_symbol or pos.option_label,
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
                    pos.option_label,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state()
        logger.info(
            "[%s] EXIT Exclusive_Nifty_CE_Buy | opt=%s qty=%d price=%.3f reason=%s",
            self.env_prefix,
            pos.option_label,
            pos.qty,
            ltp,
            reason,
        )
        state.position = None
        state.cooldown_bars = self.cooldown_bars_cfg
        state.ema_fail_count = 0
        state.pending_entry_at = None
        state.pending_entry_atr = None
        state.pending_entry_underlying = None
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
            self.state.pending_entry_at = None
            self.state.pending_entry_atr = None
            self.state.pending_entry_underlying = None
            self._adaptive_policy.mark_position_closed()
            self._reset_exit_retry_state()


__all__ = ["ExclusiveNiftyCeBuyStrategy"]
