"""
Put mean-reversion writer: shorts slightly OTM weekly Puts on failed breakdowns.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import Any, Dict, Optional, List

from app.strategies.base import BaseStrategy
from app.config.boot_config import StrategyValueResolver
from app.config.settings import get_settings
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import PUT_REVERSION_WRITER_ID
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


# Configuration for put reversion writer strategy.
@dataclass
class PutReversionWriterConfig:
    morning_entry_start: str = "10:00"
    morning_entry_end: str = "11:30"
    afternoon_entry_start: str = "13:30"
    afternoon_entry_end: str = "14:30"
    support_lookback_bars: int = 20
    initial_window_bars: int = 6
    breakdown_buffer_pct: float = 0.0015
    rsi_oversold_level: float = 35.0
    rsi_recovery_level: float = 40.0
    sl_pct: float = 0.35  # option SL for short
    tp_decay_pct: float = 0.40
    max_bars_in_trade: int = 12
    invalidation_buffer_pct: float = 0.0015
    lots_per_trade: int = 1
    otm_steps: int = 1  # number of strikes below spot for PE
    timeframe_seconds_5m: int = 300
    timeframe_seconds_15m: int = 900


# Rolling indicator and position state.
@dataclass
class ReversionInstrumentState:
    bars_5m: List[Dict[str, float]]
    last_rsi: List[float]
    last_macd_hist: List[float]
    support_price: Optional[float] = None
    last_breakdown_low: Optional[float] = None
    pending_recovery: bool = False
    breakdown_index: Optional[int] = None
    position: bool = False
    option_label: Optional[str] = None
    qty: int = 0
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None
    entry_time: Optional[datetime] = None
    entry_bar_index: Optional[int] = None
    last_reset_date: Optional[datetime.date] = None


# Mean-reversion strategy that shorts OTM puts after failed breakdowns.
class PutReversionWriterStrategy(BaseStrategy):
    """
    Shorts slightly OTM PEs when breakdown fails and market reclaims support.
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
        defaults = PutReversionWriterConfig().__dict__
        merged = {**defaults, **cfg_dict}
        self.config = PutReversionWriterConfig(**merged)
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg_dict,
            env=dict(os.environ),
        )

        self.product_type = str(cfg_dict.get("product_type", "INTRADAY")).upper()
        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()
        self.timeframe_5m = int(cfg_dict.get("timeframe_seconds_5m", 300))
        self.timeframe_15m = int(cfg_dict.get("timeframe_seconds_15m", 900))

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

        self._strategy_id = PUT_REVERSION_WRITER_ID
        self.state = ReversionInstrumentState(
            bars_5m=[],
            last_rsi=[],
            last_macd_hist=[],
        )
        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        retry_cfg = self._cfg.get_float("PUT_REV_EXIT_RETRY_COOLDOWN_SECONDS", 5.0)
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_int("PUT_REV_EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("PUT_REV_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("PUT_REV_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

        logger.info(
            "[%s] PutReversionWriter init | underlying=%s",
            self.env_prefix,
            self.underlying_label,
        )
        logger.info(
            "[%s] PUT_REV exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )

    # helpers
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
            return time(10, 0)

    # Return current IST time.
    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    # Reset state for a new trading day.
    def _reset_if_new_day(self, now: datetime) -> None:
        d = now.date()
        if self.state.last_reset_date == d:
            return
        self.state = ReversionInstrumentState(
            bars_5m=[],
            last_rsi=[],
            last_macd_hist=[],
            last_reset_date=d,
        )
        self._reset_exit_retry_state()
        logger.info("[%s] Daily reset %s", self.env_prefix, d)

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

    # Check if time is within allowed entry windows.
    def _within_entry_window(self, t: time) -> bool:
        m_start = self._parse_time(self.config.morning_entry_start)
        m_end = self._parse_time(self.config.morning_entry_end)
        a_start = self._parse_time(self.config.afternoon_entry_start)
        a_end = self._parse_time(self.config.afternoon_entry_end)
        return (m_start <= t <= m_end) or (a_start <= t <= a_end)

    # Extract strike price from label.
    def _parse_strike(self, label: str) -> Optional[float]:
        try:
            return float(label.rsplit("_", 1)[-1])
        except Exception:
            return None

    # Select the PE option label based on spot and strikes.
    def _select_put_option(self, spot: float) -> Optional[str]:
        candidates = []
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            if "PE" not in str(meta.get("kind", "")).upper():
                continue
            strike = self._parse_strike(lbl)
            if strike is None:
                continue
            candidates.append((lbl, strike))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])
        strikes = [s for _, s in candidates]
        diffs = [b - a for a, b in zip(strikes, strikes[1:]) if (b - a) > 0]
        strike_step = diffs[0] if diffs else 50.0
        atm_label, atm_strike = min(candidates, key=lambda x: abs(x[1] - spot))
        target_strike = atm_strike - strike_step * max(0, self.config.otm_steps)
        chosen = min(candidates, key=lambda x: abs(x[1] - target_strike))
        return chosen[0]

    # Resolve quantity to trade for a label.
    def _qty_for_label(self, label: str) -> int:
        return max(1, self.config.lots_per_trade)

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
        )
        routing_kwargs = self._routing_kwargs()
        return place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            **routing_kwargs,
        )

    # hooks
    # Handle tick updates and manage exits.
    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        now = self._now_ist()
        self._reset_if_new_day(now)
        if self.state.position and label == self.state.option_label:
            if self.state.stop_price and ltp >= self.state.stop_price:
                logger.info("[%s] SL hit for %s ltp=%.3f stop=%.3f", self.env_prefix, label, ltp, self.state.stop_price)
                self._exit_position("SL")
            elif self.state.tp_price and ltp <= self.state.tp_price:
                logger.info("[%s] TP hit for %s ltp=%.3f tp=%.3f", self.env_prefix, label, ltp, self.state.tp_price)
                self._exit_position("TP")
        if self.state.position and now.time() >= time(15, 15):
            self._exit_position("EOD")

    # Handle bar-close updates for 5m logic.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label:
            return
        ist_dt = candle.start_ts.astimezone(IST)
        self._reset_if_new_day(ist_dt)
        if timeframe_seconds == self.timeframe_5m:
            self._handle_5m_bar(candle, indicators, ist_dt)

    # internals
    # Evaluate entry conditions on 5m bars.
    def _handle_5m_bar(self, candle: Any, indicators: Dict[str, Any], ist_dt: datetime) -> None:
        state = self.state
        state.bars_5m.append({"h": candle.h, "l": candle.low, "c": candle.c})
        if len(state.bars_5m) > 200:
            state.bars_5m = state.bars_5m[-200:]

        rsi = indicators.get("rsi")
        macd_hist = indicators.get("macd_hist")
        vwap = indicators.get("vwap") or indicators.get("vwap_5m")
        ema20 = indicators.get("ema_20")

        if rsi is not None:
            state.last_rsi.append(float(rsi))
            state.last_rsi = state.last_rsi[-5:]
        if macd_hist is not None:
            state.last_macd_hist.append(float(macd_hist))
            state.last_macd_hist = state.last_macd_hist[-5:]

        self._update_support(candle)

        if state.position:
            self._maybe_invalidate(candle, vwap, ema20)
            self._maybe_time_stop()
            return

        if not self._within_entry_window(ist_dt.time()):
            return
        if not self._has_fake_breakdown_and_recovery(candle, indicators, vwap, ema20):
            return
        self._enter_position(candle)

    # Update support level from recent bars.
    def _update_support(self, candle: Any) -> None:
        state = self.state
        # support from first M bars or rolling N-bar low
        if len(state.bars_5m) >= self.config.initial_window_bars:
            initial_window = state.bars_5m[: self.config.initial_window_bars]
            lows_initial = [b["l"] for b in initial_window]
            rolling = state.bars_5m[-self.config.support_lookback_bars :]
            lows_rolling = [b["l"] for b in rolling]
            candidate = min(lows_initial + lows_rolling) if (lows_initial or lows_rolling) else None
            if candidate:
                state.support_price = candidate

    # Detect a fake breakdown followed by recovery.
    def _has_fake_breakdown_and_recovery(
        self, candle: Any, indicators: Dict[str, Any], vwap: Optional[float], ema20: Optional[float]
    ) -> bool:
        state = self.state
        support = state.support_price
        if support is None:
            return False
        breakdown_level = support * (1 - self.config.breakdown_buffer_pct)
        # detect breakdown
        if candle.low < breakdown_level:
            state.last_breakdown_low = candle.low
            state.pending_recovery = True
            state.breakdown_index = len(state.bars_5m)
            logger.info("[%s] Breakdown detected | support=%.3f low=%.3f", self.env_prefix, support, candle.low)
            return False
        if not state.pending_recovery:
            return False
        # recovery within 2 bars
        if state.breakdown_index is not None and (len(state.bars_5m) - state.breakdown_index) > 2:
            state.pending_recovery = False
            return False
        # recovery conditions
        if candle.c <= support:
            return False
        if candle.c <= candle.o:
            return False
        if vwap is not None and candle.c < vwap:
            return False
        if ema20 is not None and candle.c < ema20:
            return False
        if len(state.last_rsi) < 2 or len(state.last_macd_hist) < 2:
            return False
        rsi_now = state.last_rsi[-1]
        rsi_prev = state.last_rsi[-2]
        if rsi_prev < self.config.rsi_oversold_level and not (rsi_now > self.config.rsi_recovery_level):
            return False
        if not (rsi_now > rsi_prev):
            return False
        if not (state.last_macd_hist[-1] > state.last_macd_hist[-2]):
            return False
        state.pending_recovery = False
        logger.info(
            "[%s] Fake breakdown recovery | close=%.3f support=%.3f rsi=%.2f",
            self.env_prefix,
            candle.c,
            support,
            rsi_now,
        )
        return True

    # Enter a new short put position.
    def _enter_position(self, candle: Any) -> None:
        option_label = self._select_put_option(candle.c)
        if not option_label:
            logger.warning("[%s] No PE found for entry", self.env_prefix)
            return
        entry_price = self.last_price.get(option_label)
        if entry_price is None:
            logger.info("[%s] No LTP for %s yet, skip", self.env_prefix, option_label)
            return
        qty = self._qty_for_label(option_label)
        stop_price = entry_price * (1 + self.config.sl_pct)
        tp_price = entry_price * (1 - self.config.tp_decay_pct)
        resp = self._place_order(
            label=option_label,
            side="SELL",
            qty=qty,
            price=entry_price,
            tag="PUT_REV_ENTRY",
            purpose=OrderPurpose.ENTRY,
        )
        status = str(getattr(resp, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            logger.warning("[%s] Entry rejected for %s: %s", self.env_prefix, option_label, getattr(resp, "message", ""))
            return
        self._remember_order_identity(option_label)
        broker_symbol, exchange, symbol_token = self._resolve_order_identity(option_label)
        self.state.position = True
        self.state.option_label = option_label
        self.state.qty = qty
        self.state.entry_price = entry_price
        self.state.stop_price = stop_price
        self.state.tp_price = tp_price
        self.state.broker_symbol = broker_symbol
        self.state.exchange = exchange
        self.state.symbol_token = symbol_token
        self.state.entry_time = self._now_ist()
        self.state.entry_bar_index = len(self.state.bars_5m)
        self._reset_exit_retry_state()
        logger.info(
            "[%s] ENTER PUT_REV | opt=%s qty=%d entry=%.3f tp=%.3f sl=%.3f",
            self.env_prefix,
            option_label,
            qty,
            entry_price,
            tp_price,
            stop_price,
        )

    # Exit if underlying invalidates the setup.
    def _maybe_invalidate(self, candle: Any, vwap: Optional[float], ema20: Optional[float]) -> None:
        if not self.state.position:
            return
        buffer_level = None
        if self.state.last_breakdown_low is not None:
            buffer_level = self.state.last_breakdown_low * (1 - self.config.invalidation_buffer_pct)
        if buffer_level and candle.c < buffer_level:
            self._exit_position("UNDERLYING_BREAK")
            return
        if vwap is not None and ema20 is not None and candle.c < vwap and candle.c < ema20:
            if self.state.last_rsi and self.state.last_rsi[-1] < self.config.rsi_recovery_level:
                self._exit_position("TREND_FAIL")

    # Exit if the trade exceeds max bars in trade.
    def _maybe_time_stop(self) -> None:
        if not self.state.position or self.state.entry_bar_index is None:
            return
        bars_since_entry = len(self.state.bars_5m) - self.state.entry_bar_index
        if bars_since_entry >= self.config.max_bars_in_trade:
            self._exit_position("TIME_STOP")

    # Exit the current position with a reason.
    def _exit_position(self, reason: str) -> None:
        if not self.state.position or not self.state.option_label:
            return
        if self._exit_in_flight:
            return
        now_mono = monotonic()
        if now_mono < self._exit_circuit_open_until_mono:
            if now_mono - self._exit_last_alert_mono >= self._exit_circuit_alert_interval_seconds:
                self._exit_last_alert_mono = now_mono
                remaining = max(0.0, self._exit_circuit_open_until_mono - now_mono)
                logger.error(
                    "[%s][ALERT] PUT_REV exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    self.state.option_label,
                    self.state.broker_symbol or self.state.option_label,
                    self._exit_failure_count,
                    self._exit_max_retries,
                    remaining,
                )
            return
        if now_mono < self._next_exit_retry_mono:
            return
        ltp = self.last_price.get(self.state.option_label, self.state.entry_price or 0.0)
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=self.state.option_label,
                side="BUY",
                qty=self.state.qty,
                price=ltp,
                tag=f"PUT_REV_EXIT_{reason}",
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] PUT_REV exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    self.state.option_label,
                    self.state.broker_symbol or self.state.option_label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] Exit exception for %s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    self.state.option_label,
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
                    "[%s][ALERT] PUT_REV exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    self.state.option_label,
                    self.state.broker_symbol or self.state.option_label,
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
                    self.state.option_label,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state()
        logger.info(
            "[%s] EXIT PUT_REV | opt=%s qty=%d price=%.3f reason=%s",
            self.env_prefix,
            self.state.option_label,
            self.state.qty,
            ltp,
            reason,
        )
        # reset position fields
        self.state.position = False
        self.state.option_label = None
        self.state.qty = 0
        self.state.entry_price = None
        self.state.stop_price = None
        self.state.tp_price = None
        self.state.broker_symbol = None
        self.state.exchange = None
        self.state.symbol_token = None
        self.state.entry_time = None
        self.state.entry_bar_index = None

    # Force exit any open position.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.state.position:
            return
        if submit_orders:
            self._exit_position(reason)
        else:
            self.state.position = False
            self.state.option_label = None
            self.state.qty = 0
            self.state.entry_price = None
            self.state.stop_price = None
            self.state.tp_price = None
            self.state.broker_symbol = None
            self.state.exchange = None
            self.state.symbol_token = None
            self.state.entry_time = None
            self.state.entry_bar_index = None
            self._reset_exit_retry_state()


__all__ = [
    "PutReversionWriterStrategy",
    "PutReversionWriterConfig",
    "ReversionInstrumentState",
]
