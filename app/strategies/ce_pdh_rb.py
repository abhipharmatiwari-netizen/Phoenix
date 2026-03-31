"""
PDH range breakout call scalping strategy.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import Any, Dict, Optional

from app.strategies.base import BaseStrategy
from app.config.boot_config import StrategyValueResolver
from app.config.settings import get_settings
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import CE_PDH_RB_ID
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


# State for an open PDH breakout position.
@dataclass
class OpenPosition:
    option_label: str
    qty: int
    entry_price: float
    sl_price: float
    target_price: float
    entry_time: datetime
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# Tracking state for PDH breakout conditions.
@dataclass
class SymbolState:
    pdh: Optional[float] = None
    prev_close: Optional[float] = None
    today_open: Optional[float] = None
    ib_high: Optional[float] = None
    ib_low: Optional[float] = None
    ib_done: bool = False
    hod: Optional[float] = None
    preconditions_evaluated: bool = False
    preconditions_met: bool = False
    disabled_reason: Optional[str] = None
    trades_today: int = 0
    open_position: Optional[OpenPosition] = None
    vwap_sum_pv: float = 0.0
    vwap_sum_v: float = 0.0
    last_vwap: Optional[float] = None
    last_reset_date: Optional[datetime.date] = None


# PDH range breakout call scalping strategy.
class CePdhRangeBreakoutStrategy(BaseStrategy):
    """
    PDH range breakout call scalping for indices.
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
        cfg = params or {}
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg,
            env=dict(os.environ),
        )

        self.bar_minutes = int(cfg.get("bar_minutes", 1)) or 1
        self.timeframe_seconds = self.bar_minutes * 60
        self.ib_end_time = self._parse_time(cfg.get("ib_end_time", "10:15"))
        self.max_gap_abs_pct = float(cfg.get("max_gap_abs_pct", 0.003))
        self.max_ib_range_pct = float(cfg.get("max_ib_range_pct", 0.005))
        self.pre_breakout_band_pct = float(cfg.get("pre_breakout_band_pct", 0.003))
        self.breakout_buffer_pct = float(cfg.get("breakout_buffer_pct", 0.0005))
        self.min_breakout_body_pct = float(cfg.get("min_breakout_body_pct", 0.002))
        self.sl_pct = float(cfg.get("sl_pct", 0.25))
        self.target_pct = float(cfg.get("target_pct", 0.40))
        self.lots_per_trade = int(cfg.get("lots_per_trade", 1))
        self.max_trades_per_day = int(cfg.get("max_trades_per_day_per_symbol", 3))
        self.moneyness = str(cfg.get("moneyness", "ATM")).upper()
        self.itm_strike_offset = int(cfg.get("itm_strike_offset", 0))
        self.first_entry_time = self._parse_time(cfg.get("first_entry_time", "10:15"))
        self.last_entry_time = self._parse_time(cfg.get("last_entry_time", "14:45"))
        self.square_off_time = self._parse_time(cfg.get("square_off_time", "15:20"))

        self.product_type = str(cfg.get("product_type", "INTRADAY")).upper()
        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()

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

        self._strategy_id = CE_PDH_RB_ID
        self.state = SymbolState()
        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        retry_cfg = self._cfg.get_float("CE_PDH_RB_EXIT_RETRY_COOLDOWN_SECONDS", 5.0)
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_int("CE_PDH_RB_EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("CE_PDH_RB_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("CE_PDH_RB_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

        logger.info(
            "[%s] CePdhRangeBreakout init | underlying=%s tf=%ds ib_end=%s gap<=%.4f ib_range<=%.4f",
            self.env_prefix,
            self.underlying_label,
            self.timeframe_seconds,
            self.ib_end_time,
            self.max_gap_abs_pct,
            self.max_ib_range_pct,
        )
        logger.info(
            "[%s] CE_PDH_RB exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        self._restore_position_from_risk_manager()

    # ---- helpers ----

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
            return time(10, 15)

    # Return current IST time.
    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    # Reset state for a new trading day.
    def _reset_if_new_day(self, current_dt: datetime) -> None:
        d = current_dt.date()
        if self.state.last_reset_date == d:
            return
        self.state = SymbolState(last_reset_date=d)
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

    def _position_strategy_context(self, pos: OpenPosition) -> Dict[str, Any]:
        return {
            "sl_price": float(pos.sl_price),
            "target_price": float(pos.target_price),
            "position_qty": int(pos.qty),
            "last_vwap": (
                float(self.state.last_vwap)
                if self.state.last_vwap is not None
                else None
            ),
            "resistance_level": (
                float(self._resistance_level())
                if self._resistance_level() is not None
                else None
            ),
        }

    def _sync_position_state_to_risk_manager(self) -> None:
        pos = self.state.open_position
        if not pos:
            return
        sync_open_position_state(
            risk_manager=self.risk_manager,
            label=pos.option_label,
            side="BUY",
            qty=max(1, int(pos.qty)),
            entry_price=float(pos.entry_price),
            strategy_context=self._position_strategy_context(pos),
        )

    def _restore_position_from_risk_manager(self) -> None:
        if self.state.open_position is not None or self.risk_manager is None:
            return
        match = find_restored_position(
            risk_manager=self.risk_manager,
            instrument_meta=self.instrument_meta,
            strategy_id=self._strategy_id,
            canonical_strategy_name="ce_pdh_rb",
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
        sl_price = parse_non_negative_float(strategy_context.get("sl_price"))
        target_price = parse_non_negative_float(strategy_context.get("target_price"))
        qty = parse_positive_int(strategy_context.get("position_qty")) or parse_positive_int(
            entry.get("qty")
        )
        if (
            entry_price is None
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
        self.state.open_position = OpenPosition(
            option_label=label,
            qty=qty,
            entry_price=entry_price,
            sl_price=sl_price,
            target_price=target_price,
            entry_time=restored_entry_time,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self.state.last_reset_date = restored_entry_time.date()
        self.state.trades_today = max(int(self.state.trades_today), 1)
        self.state.ib_done = True
        restored_vwap = parse_non_negative_float(strategy_context.get("last_vwap"))
        if restored_vwap is not None:
            self.state.last_vwap = restored_vwap
            self.state.vwap_sum_pv = restored_vwap
            self.state.vwap_sum_v = 1.0
        resistance_level = parse_non_negative_float(strategy_context.get("resistance_level"))
        if resistance_level is not None:
            self.state.pdh = resistance_level
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

    # Select the CE option label for entry.
    def _select_call_option(self, spot: float) -> Optional[str]:
        candidates = []
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            if "CE" not in str(meta.get("kind", "")).upper():
                continue
            strike = self._parse_strike(lbl)
            if strike is None:
                continue
            candidates.append((lbl, strike))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])
        strikes = [s for _, s in candidates]
        diffs = [b - a for a, b in zip(strikes[:-1], strikes[1:]) if (b - a) > 0]
        strike_step = diffs[0] if diffs else 50.0
        atm_label, atm_strike = min(candidates, key=lambda x: abs(x[1] - spot))
        target_strike = atm_strike
        if self.moneyness == "ITM" and self.itm_strike_offset > 0:
            target_strike = atm_strike - strike_step * self.itm_strike_offset
        chosen = min(candidates, key=lambda x: abs(x[1] - target_strike))
        return chosen[0]

    # Resolve quantity to trade for a label.
    def _qty_for_label(self, label: str) -> int:
        return max(1, self.lots_per_trade)

    # Submit an order via hub or legacy bridge.
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

    # Check whether a time is within entry window.
    def _within_entry_window(self, t: time) -> bool:
        if self.first_entry_time and t < self.first_entry_time:
            return False
        if self.last_entry_time and t > self.last_entry_time:
            return False
        if self.square_off_time and t >= self.square_off_time:
            return False
        return True

    # ---- lifecycle ----

    # Handle tick updates and manage exits.
    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        now_ist = self._now_ist()
        self._reset_if_new_day(now_ist)
        state = self.state

        if state.open_position and label == state.open_position.option_label:
            pos = state.open_position
            if ltp <= pos.sl_price:
                logger.info("[%s] SL HIT %s entry=%.3f sl=%.3f ltp=%.3f", self.env_prefix, label, pos.entry_price, pos.sl_price, ltp)
                self._exit_position(reason="SL")
            elif ltp >= pos.target_price:
                logger.info("[%s] TP HIT %s entry=%.3f tp=%.3f ltp=%.3f", self.env_prefix, label, pos.entry_price, pos.target_price, ltp)
                self._exit_position(reason="TP")

        if (
            state.open_position
            and self.square_off_time
            and now_ist.time() >= self.square_off_time
        ):
            logger.info("[%s] Square-off window reached, exiting", self.env_prefix)
            self._exit_position(reason="SQUARE_OFF")

    # Handle bar-close updates for PDH logic.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label or timeframe_seconds != self.timeframe_seconds:
            return

        ist_dt = candle.start_ts.astimezone(IST)
        self._reset_if_new_day(ist_dt)
        state = self.state

        # Update today open, prev close, pdh if provided
        if state.today_open is None:
            state.today_open = candle.o
            state.prev_close = indicators.get("prev_close") or indicators.get("pclose")
            state.pdh = indicators.get("pdh")

        # VWAP using simple cum(close) as volume proxy
        state.vwap_sum_pv += candle.c
        state.vwap_sum_v += 1.0
        state.last_vwap = state.vwap_sum_pv / state.vwap_sum_v if state.vwap_sum_v > 0 else None

        # HOD
        state.hod = candle.h if state.hod is None else max(state.hod, candle.h)

        # IB tracking
        if ist_dt.time() < self.ib_end_time:
            state.ib_high = candle.h if state.ib_high is None else max(state.ib_high, candle.h)
            state.ib_low = candle.low if state.ib_low is None else min(state.ib_low, candle.low)
            return

        if not state.ib_done:
            state.ib_done = True
            self._evaluate_preconditions(candle)

        if state.open_position:
            resistance = self._resistance_level()
            below_resistance = resistance is not None and candle.c < resistance
            below_vwap = state.last_vwap is not None and candle.c < state.last_vwap
            if below_resistance or below_vwap:
                logger.info(
                    "[%s] Trend failure exit | close=%.3f res=%s vwap=%s",
                    self.env_prefix,
                    candle.c,
                    resistance,
                    state.last_vwap,
                )
                self._exit_position(reason="TREND_FAIL")

        if state.open_position:
            self._sync_position_state_to_risk_manager()

        if not state.preconditions_met or state.open_position:
            return

        if state.trades_today >= self.max_trades_per_day:
            return

        if not self._within_entry_window(ist_dt.time()):
            return

        resistance = self._resistance_level()
        if resistance is None:
            return
        if candle.c < resistance * (1 + self.breakout_buffer_pct):
            return

        if state.last_vwap is not None and candle.c < state.last_vwap:
            return

        body_pct = abs(candle.c - candle.o) / candle.o if candle.o else 0.0
        if body_pct < self.min_breakout_body_pct:
            return

        self._enter_position(candle, resistance)

    # ---- internals ----

    # Evaluate preconditions before allowing breakout entries.
    def _evaluate_preconditions(self, candle: Any) -> None:
        state = self.state
        if state.preconditions_evaluated:
            return

        # Gap check
        if state.prev_close is None or state.today_open is None:
            state.disabled_reason = "missing_prev_close_or_open"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s", self.env_prefix, state.disabled_reason)
            return
        gap_pct = (state.today_open - state.prev_close) / state.prev_close
        if abs(gap_pct) > self.max_gap_abs_pct:
            state.disabled_reason = "gap_too_wide"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s gap=%.4f", self.env_prefix, state.disabled_reason, gap_pct)
            return

        # IB range
        if state.ib_high is None or state.ib_low is None:
            state.disabled_reason = "ib_missing"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s", self.env_prefix, state.disabled_reason)
            return
        ib_range = state.ib_high - state.ib_low
        ref_price = (state.ib_high + state.ib_low) / 2 if (state.ib_high and state.ib_low) else 0
        ib_range_pct = (ib_range / ref_price) if ref_price else 1.0
        if ib_range_pct > self.max_ib_range_pct:
            state.disabled_reason = "ib_too_wide"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s ib_range_pct=%.4f", self.env_prefix, state.disabled_reason, ib_range_pct)
            return

        resistance = self._resistance_level()
        if resistance is None:
            state.disabled_reason = "resistance_missing"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s", self.env_prefix, state.disabled_reason)
            return
        close_price = candle.c
        if not (close_price < resistance):
            state.disabled_reason = "already_above_resistance"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s close=%.3f res=%.3f", self.env_prefix, state.disabled_reason, close_price, resistance)
            return
        distance_pct = (resistance - close_price) / resistance
        if distance_pct > self.pre_breakout_band_pct:
            state.disabled_reason = "too_far_below_resistance"
            state.preconditions_evaluated = True
            logger.info("[%s] Preconditions failed: %s dist=%.4f", self.env_prefix, state.disabled_reason, distance_pct)
            return

        state.preconditions_met = True
        state.preconditions_evaluated = True
        logger.info("[%s] Preconditions met | gap=%.4f ib_range=%.4f res=%.3f close=%.3f", self.env_prefix, gap_pct, ib_range_pct, resistance, close_price)

    # Compute the resistance level from PDH/IB/HOD.
    def _resistance_level(self) -> Optional[float]:
        state = self.state
        levels = [lvl for lvl in (state.pdh, state.ib_high, state.hod) if lvl is not None]
        if not levels:
            return None
        return max(levels)

    # Enter a new breakout position.
    def _enter_position(self, candle: Any, resistance: float) -> None:
        state = self.state
        option_label = self._select_call_option(candle.c)
        if not option_label:
            logger.warning("[%s] No CE option found for entry", self.env_prefix)
            return
        qty = self._qty_for_label(option_label)
        entry_price = self.last_price.get(option_label)
        if entry_price is None:
            logger.info("[%s] No LTP for %s yet, skipping entry", self.env_prefix, option_label)
            return
        sl_price = entry_price * (1 - self.sl_pct)
        target_price = entry_price * (1 + self.target_pct)
        position_context = {
            "sl_price": float(sl_price),
            "target_price": float(target_price),
            "position_qty": int(qty),
            "last_vwap": (
                float(state.last_vwap)
                if state.last_vwap is not None
                else None
            ),
            "resistance_level": float(resistance),
        }
        resp = self._place_order(
            label=option_label,
            side="BUY",
            qty=qty,
            price=entry_price,
            tag="CE_PDH_RB_ENTRY",
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
            return

        self._remember_order_identity(option_label)
        broker_symbol, exchange, symbol_token = self._resolve_order_identity(option_label)
        state.open_position = OpenPosition(
            option_label=option_label,
            qty=qty,
            entry_price=entry_price,
            sl_price=sl_price,
            target_price=target_price,
            entry_time=self._now_ist(),
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self._reset_exit_retry_state()
        state.trades_today += 1
        self._sync_position_state_to_risk_manager()
        logger.info(
            "[%s] ENTER CE_PDH_RB | opt=%s qty=%d entry=%.3f sl=%.3f tp=%.3f res=%.3f",
            self.env_prefix,
            option_label,
            qty,
            entry_price,
            sl_price,
            target_price,
            resistance,
        )

    # Exit the current position for a given reason.
    def _exit_position(self, reason: str) -> None:
        state = self.state
        pos = state.open_position
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
                    "[%s][ALERT] CE_PDH_RB exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
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
        ltp = self.last_price.get(pos.option_label, pos.entry_price)
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=pos.option_label,
                side="SELL",
                qty=pos.qty,
                price=ltp,
                tag=f"CE_PDH_RB_EXIT_{reason}",
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] CE_PDH_RB exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
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
                    "[%s][ALERT] CE_PDH_RB exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
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
            "[%s] EXIT CE_PDH_RB | opt=%s qty=%d price=%.3f reason=%s",
            self.env_prefix,
            pos.option_label,
            pos.qty,
            ltp,
            reason,
        )
        state.open_position = None

    # Force exit any open position.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.state.open_position:
            return
        if submit_orders:
            self._exit_position(reason=reason)
        else:
            self.state.open_position = None
            self._reset_exit_retry_state()


__all__ = ["CePdhRangeBreakoutStrategy"]
