"""
Live Put Buy strategy integration.
Implements a bar-driven version of the strategy_put_buy logic with order placement.
"""

from __future__ import annotations

import logging
import math
import os
from time import monotonic
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.config.settings import get_settings
from app.config.boot_config import StrategyValueResolver
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.base import BaseStrategy
from app.strategies.identifiers import PUT_BUY_ID
from app.strategies.restart_state import (
    find_restored_position,
    parse_bool,
    parse_datetime,
    parse_non_negative_float,
    parse_positive_int,
    sync_open_position_state,
)
from app.strategies.strategy_config import StrategyConfig

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class PendingEntry:
    entry_at: datetime
    pullback_high: float
    atr: Optional[float]


@dataclass
class PositionState:
    option_label: str
    qty: int
    entry_time: datetime
    entry_underlying_price: float
    entry_option_price: float
    initial_stop: float
    stop_price: float
    R: float
    target1: float
    target2: float
    remaining_qty: int
    tp1_done: bool = False
    trail_active: bool = False
    realized_R: float = 0.0
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


class PutBuyLiveStrategy(BaseStrategy):
    """
    Intraday `put_buy` strategy using 5m/15m bars and hub order routing.
    """

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
        self.underlying_key = (
            self.underlying_label.replace("_IDX", "").replace("_FUT", "")
        )

        cfg_dict = params or {}
        defaults = StrategyConfig().__dict__
        config_overrides = {k: cfg_dict[k] for k in defaults if k in cfg_dict}
        self.config = StrategyConfig(**{**defaults, **config_overrides})
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg_dict,
            env=dict(os.environ),
        )

        self.timeframe_5m = int(cfg_dict.get("timeframe_seconds_5m", 300))
        self.timeframe_15m = int(cfg_dict.get("timeframe_seconds_15m", 900))
        self.lots_per_trade = int(cfg_dict.get("lots_per_trade", 1))
        self.product_type = str(cfg_dict.get("product_type", "INTRADAY")).upper()
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

        self._strategy_id = PUT_BUY_ID

        self._session_start = self._parse_time(self.config.session_start)
        self._session_end = self._parse_time(self.config.session_end)
        self._no_trade_until = self._parse_time(self.config.no_trade_until)
        self._last_entry_time = self._parse_time(self.config.last_entry_time)
        self._eod_exit_time = self._parse_time(self.config.eod_exit_time)

        self.position: Optional[PositionState] = None
        self.pending_entry: Optional[PendingEntry] = None
        self.pullback_active = False
        self.pullback_high: Optional[float] = None
        self.stall_count = 0

        self.prev_rsi: Optional[float] = None
        self.prev_macd_hist: Optional[float] = None
        self.prev_high: Optional[float] = None

        self.current_day: Optional[date] = None
        self.trades_taken_today = 0
        self.realized_R_today = 0.0
        self.consecutive_losses = 0
        self.stop_trading_today = False

        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}

        self._closes_5m = deque(maxlen=600)
        self._closes_15m = deque(maxlen=600)
        self._atr_15m_history = deque(
            maxlen=max(1, int(self.config.atr_percentile_lookback))
        )
        self._last_15m_close: Optional[float] = None
        self._last_15m_rsi: Optional[float] = None
        self._last_15m_atr: Optional[float] = None
        self._last_15m_ema50: Optional[float] = None
        self._last_15m_atr_threshold: Optional[float] = None

        self._warned_volume_filter = False
        self._exit_in_flight = False
        self._next_exit_retry_mono = 0.0
        retry_cfg = self._cfg.get_float("PUT_BUY_EXIT_RETRY_COOLDOWN_SECONDS", 5.0)
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_int("PUT_BUY_EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("PUT_BUY_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("PUT_BUY_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

        logger.info(
            "[%s] PutBuyLive init | underlying=%s tf5=%ds tf15=%ds",
            self.env_prefix,
            self.underlying_label,
            self.timeframe_5m,
            self.timeframe_15m,
        )
        logger.info(
            "[%s] PUT_BUY exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        self._restore_position_from_risk_manager()

    # ---- helpers ----

    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    def _parse_time(self, value: str) -> time:
        try:
            h, m = (int(p) for p in str(value).split(":"))
            return time(hour=h, minute=m)
        except Exception:
            return time(9, 30)

    def _now_ist(self) -> datetime:
        return datetime.now(timezone.utc).astimezone(IST)

    def _compute_ema(self, values: deque[float], period: int) -> Optional[float]:
        if period <= 0 or len(values) < period:
            return None
        k = 2.0 / (period + 1.0)
        ema_val = values[0]
        for price in values:
            ema_val = price * k + ema_val * (1.0 - k)
        return ema_val

    def _percentile(self, values: deque[float], pct: float) -> Optional[float]:
        if not values:
            return None
        pct = max(0.0, min(100.0, float(pct)))
        sorted_vals = sorted(values)
        if len(sorted_vals) == 1:
            return sorted_vals[0]
        k = (len(sorted_vals) - 1) * (pct / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_vals[int(k)]
        return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)

    def _reset_if_new_day(self, ts: datetime) -> None:
        day = ts.date()
        if self.current_day == day:
            return
        self.current_day = day
        self.trades_taken_today = 0
        self.realized_R_today = 0.0
        self.consecutive_losses = 0
        self.stop_trading_today = False
        self.pullback_active = False
        self.pullback_high = None
        self.stall_count = 0
        self.pending_entry = None
        self.prev_high = None
        if self.position:
            logger.warning("[%s] Resetting open position on new day", self.env_prefix)
            self.position = None
        self._reset_exit_retry_state()

    @staticmethod
    def _resolve_broker_symbol(meta: Dict[str, Any], fallback_label: str) -> str:
        for key in ("symbol", "tradingsymbol", "trading_symbol", "tsym"):
            value = meta.get(key)
            if value not in (None, ""):
                return str(value)
        return fallback_label

    @staticmethod
    def _resolve_symbol_token(meta: Dict[str, Any]) -> Optional[str]:
        for key in ("token", "symboltoken", "instrument_token"):
            value = meta.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _remember_order_identity(self, label: str) -> None:
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange") or meta.get("exch_seg")
        self._order_identity_by_label[label] = {
            "broker_symbol": self._resolve_broker_symbol(meta, label),
            "exchange": str(exchange) if exchange not in (None, "") else None,
            "symbol_token": self._resolve_symbol_token(meta),
        }

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
            "initial_stop": float(pos.initial_stop),
            "stop_price": float(pos.stop_price),
            "R": float(pos.R),
            "target1": float(pos.target1),
            "target2": float(pos.target2),
            "position_qty": int(pos.qty),
            "remaining_qty": int(pos.remaining_qty),
            "tp1_done": bool(pos.tp1_done),
            "trail_active": bool(pos.trail_active),
            "realized_R": float(pos.realized_R),
            "prev_high": float(self.prev_high) if self.prev_high is not None else None,
        }

    def _sync_position_state_to_risk_manager(self) -> None:
        pos = self.position
        if not pos:
            return
        sync_open_position_state(
            risk_manager=self.risk_manager,
            label=pos.option_label,
            side="BUY",
            qty=max(1, int(pos.remaining_qty)),
            entry_price=float(pos.entry_option_price),
            strategy_context=self._position_strategy_context(pos),
        )

    def _restore_position_from_risk_manager(self) -> None:
        if self.position is not None or self.risk_manager is None:
            return
        match = find_restored_position(
            risk_manager=self.risk_manager,
            instrument_meta=self.instrument_meta,
            strategy_id=self._strategy_id,
            canonical_strategy_name="put_buy",
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
        initial_stop = parse_non_negative_float(strategy_context.get("initial_stop"))
        stop_price = parse_non_negative_float(strategy_context.get("stop_price"))
        risk_r = parse_non_negative_float(strategy_context.get("R"))
        target1 = parse_non_negative_float(strategy_context.get("target1"))
        target2 = parse_non_negative_float(strategy_context.get("target2"))
        total_qty = parse_positive_int(strategy_context.get("position_qty")) or parse_positive_int(
            entry.get("qty")
        )
        remaining_qty = parse_positive_int(
            strategy_context.get("remaining_qty")
        ) or parse_positive_int(entry.get("qty"))
        if (
            entry_option_price is None
            or entry_underlying_price is None
            or initial_stop is None
            or stop_price is None
            or risk_r is None
            or target1 is None
            or target2 is None
            or total_qty is None
            or remaining_qty is None
        ):
            return

        entry_time = parse_datetime(entry.get("entry_ts")) or datetime.now(timezone.utc)
        broker_symbol = str(entry.get("tradingsymbol") or "").strip() or self._resolve_broker_symbol(
            self.instrument_meta.get(label, {}),
            label,
        )
        exchange = str(
            entry.get("exchange")
            or self.instrument_meta.get(label, {}).get("exchange")
            or self.instrument_meta.get(label, {}).get("exch_seg")
            or ""
        ).strip() or None
        symbol_token = str(
            entry.get("symboltoken")
            or self.instrument_meta.get(label, {}).get("token")
            or ""
        ).strip() or None

        self._order_identity_by_label[label] = {
            "broker_symbol": broker_symbol,
            "exchange": exchange,
            "symbol_token": symbol_token,
        }
        self.position = PositionState(
            option_label=label,
            qty=total_qty,
            entry_time=entry_time.astimezone(IST),
            entry_underlying_price=entry_underlying_price,
            entry_option_price=entry_option_price,
            initial_stop=initial_stop,
            stop_price=stop_price,
            R=risk_r,
            target1=target1,
            target2=target2,
            remaining_qty=remaining_qty,
            tp1_done=parse_bool(strategy_context.get("tp1_done"), default=remaining_qty < total_qty),
            trail_active=parse_bool(strategy_context.get("trail_active"), default=remaining_qty < total_qty),
            realized_R=float(strategy_context.get("realized_R") or 0.0),
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        restored_prev_high = parse_non_negative_float(strategy_context.get("prev_high"))
        self.prev_high = restored_prev_high
        self.current_day = self.position.entry_time.date()
        self.trades_taken_today = max(int(self.trades_taken_today), 1)
        self.pending_entry = None
        self.pullback_active = False
        self.pullback_high = None
        self._reset_exit_retry_state()

    def _reset_exit_retry_state(self) -> None:
        self._next_exit_retry_mono = 0.0
        self._exit_failure_count = 0
        self._exit_circuit_open_until_mono = 0.0
        self._exit_last_alert_mono = 0.0

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

    def _is_exit_circuit_active(self, *, label: str) -> bool:
        now_mono = monotonic()
        if now_mono < self._exit_circuit_open_until_mono:
            if now_mono - self._exit_last_alert_mono >= self._exit_circuit_alert_interval_seconds:
                self._exit_last_alert_mono = now_mono
                remaining = max(0.0, self._exit_circuit_open_until_mono - now_mono)
                logger.error(
                    "[%s][ALERT] PUT_BUY exit circuit active | label=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    label,
                    self._exit_failure_count,
                    self._exit_max_retries,
                    remaining,
                )
            return True
        return False

    def _in_session(self, ts: datetime) -> bool:
        t = ts.time()
        return self._session_start <= t <= self._session_end

    def _can_generate_signal(self, ts: datetime) -> bool:
        if not self._in_session(ts):
            return False
        if ts.time() < self._no_trade_until:
            return False
        if ts.time() > self._last_entry_time:
            return False
        return True

    def _can_enter(self, ts: datetime) -> bool:
        if not self._in_session(ts):
            return False
        if ts.time() < self._no_trade_until:
            return False
        if ts.time() > self._last_entry_time:
            return False
        if self.stop_trading_today:
            return False
        if self.trades_taken_today >= self.config.max_trades_per_day:
            return False
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            return False
        if self.realized_R_today <= -abs(self.config.daily_loss_limit_R):
            return False
        return True

    def _bear_filter(self) -> bool:
        if self._last_15m_ema50 is None or self._last_15m_rsi is None:
            return False
        if self._last_15m_close is None:
            return False
        cond = (self._last_15m_close < self._last_15m_ema50) and (self._last_15m_rsi < 50)
        if not cond:
            return False
        if self.config.use_atr_filter:
            if self._last_15m_atr_threshold is None or self._last_15m_atr is None:
                return False
            return self._last_15m_atr >= self._last_15m_atr_threshold
        return True

    def _pullback_approach(self, close_price: float, ema21: Optional[float], atr: Optional[float]) -> bool:
        if ema21 is None or atr is None:
            return False
        band = self.config.pullback_band * atr
        return abs(close_price - ema21) <= band

    def _entry_trigger(
        self,
        close_price: float,
        ema9: Optional[float],
        rsi_val: Optional[float],
        macd_hist: Optional[float],
    ) -> bool:
        if ema9 is None or rsi_val is None or macd_hist is None:
            return False
        if close_price >= ema9:
            return False
        if macd_hist >= 0:
            return False
        if self.prev_macd_hist is None or macd_hist >= self.prev_macd_hist:
            return False
        if self.config.strict_rsi_cross:
            if self.prev_rsi is None:
                return False
            rsi_cross = self.prev_rsi >= self.config.rsi_entry and rsi_val < self.config.rsi_entry
        else:
            rsi_cross = rsi_val < self.config.rsi_entry
        if not rsi_cross:
            return False
        if self.config.use_volume_filter:
            if not self._warned_volume_filter:
                logger.warning("[%s] Volume filter enabled but no volume data in live feed", self.env_prefix)
                self._warned_volume_filter = True
            return False
        return True

    def _calculate_stop_price(self, entry_price: float, pullback_high: float, atr_val: float) -> float:
        volatility_sl = entry_price + (self.config.atr_mult * atr_val)
        return max(pullback_high, volatility_sl)

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

    def _select_expiry(self, expiries: list[date], entry_ts: datetime) -> Optional[date]:
        if not expiries:
            return None
        entry_date = entry_ts.astimezone(IST).date()
        mode = str(self.config.expiry_mode or "NEAREST").upper()
        if mode == "0DTE":
            candidates = [e for e in expiries if e == entry_date]
        elif mode == "1DTE":
            target = entry_date + timedelta(days=1)
            candidates = [e for e in expiries if e == target]
        else:
            candidates = [e for e in expiries if e >= entry_date]
        if not candidates:
            return min(expiries)
        return min(candidates)

    def _select_put_option(self, spot: float, ts: datetime) -> Optional[str]:
        candidates = []
        expiries = []
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != self.underlying_key:
                continue
            kind = str(meta.get("kind", "")).upper()
            if "PE" not in kind and "PUT" not in kind:
                continue
            exp_dt = self._parse_expiry(meta.get("expiry") or meta.get("expiry_dt"))
            if exp_dt:
                expiries.append(exp_dt)
            strike = self._parse_strike(lbl)
            if strike is None:
                continue
            candidates.append((lbl, strike, exp_dt))

        if not candidates:
            return None

        expiry = self._select_expiry([e for e in expiries if e], ts)
        if expiry is not None:
            candidates = [c for c in candidates if c[2] == expiry]
            if not candidates:
                return None

        strikes = sorted({c[1] for c in candidates})
        strike_step = self.config.strike_step if self.config.strike_step > 0 else None
        if strike_step is None and len(strikes) >= 2:
            diffs = [b - a for a, b in zip(strikes[:-1], strikes[1:]) if (b - a) > 0]
            strike_step = diffs[0] if diffs else None
        if strike_step is None:
            strike_step = 50.0

        atm_label, atm_strike, _ = min(candidates, key=lambda x: abs(x[1] - spot))
        target_strike = atm_strike
        if str(self.config.option_moneyness).upper() == "ITM1":
            target_strike = atm_strike + strike_step
        chosen = min(candidates, key=lambda x: abs(x[1] - target_strike))
        return chosen[0]

    def _parse_strike(self, label: str) -> Optional[float]:
        try:
            return float(label.rsplit("_", 1)[-1])
        except Exception:
            return None

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
            quantity=qty,  # qty is lots; OrderRouter preflight expands to broker quantity
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

    # ---- lifecycle ----

    def on_tick(self, label: str, ltp: float) -> None:
        self.last_price[label] = ltp
        now = self._now_ist()
        if label == self.underlying_label:
            self._reset_if_new_day(now)
            if self.position and now.time() >= self._eod_exit_time:
                self._exit_position(reason="EOD", exit_underlying_price=ltp)
                return
            if self.pending_entry and now >= self.pending_entry.entry_at:
                if self._can_enter(now):
                    self._enter_position(
                        entry_underlying_price=ltp,
                        ts=now,
                        pullback_high=self.pending_entry.pullback_high,
                        atr=self.pending_entry.atr,
                    )
                self.pending_entry = None

    def on_bar(
        self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]
    ) -> None:
        if label != self.underlying_label:
            return

        ist_dt = candle.start_ts.astimezone(IST)
        self._reset_if_new_day(ist_dt)

        if timeframe_seconds == self.timeframe_15m:
            self._handle_15m_bar(candle, indicators)
            return
        if timeframe_seconds != self.timeframe_5m:
            return

        self._handle_5m_bar(candle, indicators, ist_dt)

    # ---- bar handlers ----

    def _handle_15m_bar(self, candle: Any, indicators: Dict[str, Any]) -> None:
        self._closes_15m.append(float(candle.c))
        self._last_15m_close = float(candle.c)
        self._last_15m_rsi = indicators.get("rsi")
        self._last_15m_atr = indicators.get("atr")
        self._last_15m_ema50 = self._compute_ema(self._closes_15m, self.config.ema_15m_period)

        if self.config.use_atr_filter and self._last_15m_atr is not None:
            self._atr_15m_history.append(float(self._last_15m_atr))
            if len(self._atr_15m_history) >= max(1, self.config.atr_percentile_lookback):
                self._last_15m_atr_threshold = self._percentile(
                    self._atr_15m_history, self.config.atr_min_percentile
                )

    def _handle_5m_bar(self, candle: Any, indicators: Dict[str, Any], ist_dt: datetime) -> None:
        self._closes_5m.append(float(candle.c))
        ema9 = self._compute_ema(self._closes_5m, self.config.ema_5m_fast)
        ema21 = self._compute_ema(self._closes_5m, self.config.ema_5m_slow)

        rsi_val = indicators.get("rsi")
        macd_hist = indicators.get("macd_hist")
        atr_val = indicators.get("atr")

        if self.position:
            if ist_dt.time() >= self._eod_exit_time:
                self._exit_position(reason="EOD", exit_underlying_price=float(candle.c))
            else:
                self._process_exits(
                    candle=candle,
                    ema9=ema9,
                )

        if self.position is None and self.pending_entry is None:
            if self._can_generate_signal(ist_dt):
                bearish_mode = self._bear_filter()
                if not bearish_mode:
                    self.pullback_active = False
                    self.pullback_high = None
                    self.stall_count = 0
                else:
                    approach = self._pullback_approach(float(candle.c), ema21, atr_val)
                    if approach and not self.pullback_active:
                        self.pullback_active = True
                        self.pullback_high = float(candle.h)
                        self.stall_count = 0
                    if self.pullback_active:
                        if self.pullback_high is None:
                            self.pullback_high = float(candle.h)
                        else:
                            self.pullback_high = max(self.pullback_high, float(candle.h))
                        if ema21 is not None and float(candle.c) > ema21:
                            self.stall_count = 0
                        else:
                            self.stall_count += 1

                        if not approach:
                            self.pullback_active = False
                            self.pullback_high = None
                            self.stall_count = 0

                    if (
                        self.pullback_active
                        and self.stall_count >= self.config.stall_candles
                        and self._entry_trigger(float(candle.c), ema9, rsi_val, macd_hist)
                    ):
                        entry_at = candle.end_ts or (candle.start_ts + timedelta(seconds=self.timeframe_5m))
                        delay_bars = max(1, int(self.config.entry_delay_bars))
                        entry_at = entry_at + timedelta(seconds=self.timeframe_5m * (delay_bars - 1))
                        if self.pullback_high is not None:
                            self.pending_entry = PendingEntry(
                                entry_at=entry_at,
                                pullback_high=self.pullback_high,
                                atr=atr_val,
                            )

        if rsi_val is not None:
            self.prev_rsi = float(rsi_val)
        if macd_hist is not None:
            self.prev_macd_hist = float(macd_hist)
        self.prev_high = float(candle.h)
        if self.position:
            self._sync_position_state_to_risk_manager()

    # ---- trade management ----

    def _enter_position(
        self,
        *,
        entry_underlying_price: float,
        ts: datetime,
        pullback_high: float,
        atr: Optional[float],
    ) -> None:
        if atr is None:
            logger.info("[%s] ATR missing for entry, skip", self.env_prefix)
            return
        stop_price = self._calculate_stop_price(entry_underlying_price, pullback_high, atr)
        R = stop_price - entry_underlying_price
        if R <= 0:
            return

        option_label = self._select_put_option(entry_underlying_price, ts)
        if not option_label:
            logger.warning("[%s] No PE option found for entry", self.env_prefix)
            return
        option_price = self.last_price.get(option_label)
        if option_price is None:
            logger.info("[%s] No LTP for %s yet, skipping entry", self.env_prefix, option_label)
            return

        qty = max(1, int(self.lots_per_trade))
        position_context = {
            "entry_underlying_price": float(entry_underlying_price),
            "entry_option_price": float(option_price),
            "initial_stop": float(stop_price),
            "stop_price": float(stop_price),
            "R": float(R),
            "target1": float(entry_underlying_price - (self.config.tp1_R * R)),
            "target2": float(entry_underlying_price - (self.config.tp2_R * R)),
            "position_qty": int(qty),
            "remaining_qty": int(qty),
            "tp1_done": False,
            "trail_active": False,
            "realized_R": 0.0,
        }
        resp = self._place_order(
            label=option_label,
            side="BUY",
            qty=qty,
            price=float(option_price),
            tag="PUT_BUY_ENTRY",
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
        self.position = PositionState(
            option_label=option_label,
            qty=qty,
            entry_time=ts,
            entry_underlying_price=entry_underlying_price,
            entry_option_price=float(option_price),
            initial_stop=stop_price,
            stop_price=stop_price,
            R=R,
            target1=entry_underlying_price - (self.config.tp1_R * R),
            target2=entry_underlying_price - (self.config.tp2_R * R),
            remaining_qty=qty,
            broker_symbol=broker_symbol,
            exchange=exchange,
            symbol_token=symbol_token,
        )
        self._reset_exit_retry_state()
        self.trades_taken_today += 1
        self.pullback_active = False
        self.pullback_high = None
        self.stall_count = 0
        self._sync_position_state_to_risk_manager()
        logger.info(
            "[%s] ENTER PUT_BUY | opt=%s qty=%d entry=%.3f stop=%.3f t1=%.3f t2=%.3f",
            self.env_prefix,
            option_label,
            qty,
            entry_underlying_price,
            stop_price,
            self.position.target1,
            self.position.target2,
        )

    def _process_exits(self, *, candle: Any, ema9: Optional[float]) -> None:
        pos = self.position
        if not pos:
            return
        high = float(candle.h)
        low = float(candle.low)

        stop_hit = high >= pos.stop_price
        tp2_hit = low <= pos.target2
        tp1_hit = (not pos.tp1_done) and (low <= pos.target1)

        if stop_hit:
            reason = "TRAIL" if pos.trail_active else "STOP"
            self._exit_position(reason=reason, exit_underlying_price=pos.stop_price)
            return
        if tp2_hit:
            self._exit_position(reason="TP2", exit_underlying_price=pos.target2)
            return
        if tp1_hit:
            self._partial_exit(exit_underlying_price=pos.target1)

        if pos.tp1_done and pos.remaining_qty > 0:
            ref_high = self.prev_high if self.prev_high is not None else high
            trail_ref = max(ref_high, ema9 if ema9 is not None else ref_high)
            updated_stop = min(pos.stop_price, trail_ref)
            if updated_stop < pos.stop_price:
                pos.stop_price = updated_stop
                self._sync_position_state_to_risk_manager()

    def _partial_exit(self, *, exit_underlying_price: float) -> None:
        pos = self.position
        if not pos:
            return
        if self._exit_in_flight:
            return
        if self._is_exit_circuit_active(label=pos.option_label):
            return
        now_mono = monotonic()
        if now_mono < self._next_exit_retry_mono:
            return
        total_qty = pos.qty
        if total_qty <= 0 or pos.remaining_qty <= 0:
            return
        target_qty = max(1, int(round(total_qty * self.config.partial_take_pct)))
        target_qty = min(target_qty, pos.remaining_qty)
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=pos.option_label,
                side="SELL",
                qty=target_qty,
                price=float(self.last_price.get(pos.option_label, pos.entry_option_price)),
                tag="PUT_BUY_TP1",
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] PUT_BUY TP1 exit circuit opened after exception | label=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] TP1 exit exception for %s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.option_label,
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
                    "[%s][ALERT] PUT_BUY TP1 exit circuit opened after broker rejection | label=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
                    status,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.warning(
                    "[%s] TP1 rejected for %s: %s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    pos.option_label,
                    getattr(resp, "message", ""),
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state()
        fraction = target_qty / float(total_qty)
        exit_R = (pos.entry_underlying_price - exit_underlying_price) / pos.R
        pos.realized_R += exit_R * fraction
        pos.remaining_qty -= target_qty
        pos.tp1_done = True
        pos.trail_active = True
        pos.stop_price = pos.entry_underlying_price
        self._sync_position_state_to_risk_manager()
        logger.info(
            "[%s] TP1 EXIT | opt=%s qty=%d price=%.3f",
            self.env_prefix,
            pos.option_label,
            target_qty,
            exit_underlying_price,
        )
        if pos.remaining_qty <= 0:
            self._finalize_exit(exit_underlying_price=exit_underlying_price, reason="TP1_FULL")

    def _exit_position(self, *, reason: str, exit_underlying_price: float) -> None:
        pos = self.position
        if not pos:
            return
        qty = pos.remaining_qty
        if qty <= 0:
            self._finalize_exit(exit_underlying_price=exit_underlying_price, reason=reason)
            return
        if self._exit_in_flight:
            return
        if self._is_exit_circuit_active(label=pos.option_label):
            return
        now_mono = monotonic()
        if now_mono < self._next_exit_retry_mono:
            return
        self._exit_in_flight = True
        try:
            resp = self._place_order(
                label=pos.option_label,
                side="SELL",
                qty=qty,
                price=float(self.last_price.get(pos.option_label, pos.entry_option_price)),
                tag=f"PUT_BUY_EXIT_{reason}",
                purpose=OrderPurpose.EXIT,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(now_mono=monotonic())
            if opened:
                logger.exception(
                    "[%s][ALERT] PUT_BUY exit circuit opened after exception | label=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
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
                    "[%s][ALERT] PUT_BUY exit circuit opened after broker rejection | label=%s reason=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    pos.option_label,
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
        self._finalize_exit(exit_underlying_price=exit_underlying_price, reason=reason)

    def _finalize_exit(self, *, exit_underlying_price: float, reason: str) -> None:
        pos = self.position
        if not pos:
            return
        remaining_fraction = 0.0
        if pos.qty > 0:
            remaining_fraction = pos.remaining_qty / float(pos.qty)
        exit_R = (pos.entry_underlying_price - exit_underlying_price) / pos.R
        pos.realized_R += exit_R * remaining_fraction

        self.realized_R_today += pos.realized_R
        if pos.realized_R < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.realized_R_today <= -abs(self.config.daily_loss_limit_R):
            self.stop_trading_today = True
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.stop_trading_today = True

        logger.info(
            "[%s] EXIT PUT_BUY | opt=%s reason=%s R=%.2f",
            self.env_prefix,
            pos.option_label,
            reason,
            pos.realized_R,
        )
        self.position = None
        self._reset_exit_retry_state()

    # ---- admin ----

    def force_exit_all(self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True) -> None:
        if not self.position:
            return
        if submit_orders:
            self._exit_position(reason=reason, exit_underlying_price=self.last_price.get(self.underlying_label, 0.0))
        else:
            self.position = None
            self._reset_exit_retry_state()


__all__ = ["PutBuyLiveStrategy"]
