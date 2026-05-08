"""
Nifty weekly credit spreads strategy (directional and iron condor) on NSE weekly options.
"""

from __future__ import annotations

import logging
import math
import os
from time import monotonic
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, date
from typing import Any, Dict, List, Optional, Tuple

from app.strategies.base import BaseStrategy
from app.config.boot_config import StrategyValueResolver
from app.config.settings import get_settings
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import NIFTY_WEEKLY_CREDIT_SPREADS_ID
from app.strategies.naming import canonicalize_strategy_name
from app.strategies.restart_state import (
    parse_bool,
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


# Parse a HH:MM time string with a default fallback.
def _parse_time(val: str, default: time) -> time:
    try:
        h, m = (int(x) for x in str(val).split(":"))
        return time(hour=h, minute=m)
    except Exception:
        return default


# Round down to the nearest 50-point strike.
def round_down_50(x: float) -> float:
    return math.floor(x / 50.0) * 50.0


# Round up to the nearest 50-point strike.
def round_up_50(x: float) -> float:
    return math.ceil(x / 50.0) * 50.0


# Round to the nearest 50-point strike.
def round_to_nearest_50(x: float) -> float:
    return round(x / 50.0) * 50.0


# Configuration for a spread leg.
@dataclass
class SpreadLeg:
    label: str
    side: str  # BUY / SELL
    strike: float
    qty: int
    entry_price: float = 0.0
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# State for an open credit spread or condor.
@dataclass
class OpenSpread:
    spread_id: str
    spread_type: str  # PUT_SPREAD / CALL_SPREAD / IRON_CONDOR
    short_leg: SpreadLeg
    long_leg: SpreadLeg
    entry_credit: float
    width_pts: float
    max_loss_rupees: float
    entry_time: datetime
    expiry: date
    extra_legs: List[SpreadLeg] = field(default_factory=list)  # for condor other side
    short_exit_done: bool = False
    long_exit_done: bool = False


# Weekly credit spreads strategy for NIFTY options.
class NiftyWeeklyCreditSpreadStrategy(BaseStrategy):
    """
    Defined-risk weekly credit spreads on NIFTY options using 5m indicators.
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
        cfg = params or {}
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params=cfg,
            env=dict(os.environ),
        )

        # Config defaults
        self.lot_size = int(cfg.get("lot_size", 1))
        self.entry_start_time = _parse_time(cfg.get("entry_start_time", "10:10"), time(10, 10))
        self.entry_end_time = _parse_time(cfg.get("entry_end_time", "13:30"), time(13, 30))
        self.min_days_to_expiry = int(cfg.get("min_days_to_expiry", 1))
        self.max_days_to_expiry = int(cfg.get("max_days_to_expiry", 5))
        self.force_flatten_on_expiry = bool(cfg.get("force_flatten_on_expiry", True))
        self.exit_time_eod = _parse_time(cfg.get("exit_time_eod", "15:15"), time(15, 15))

        self.atm_straddle_min_pct = float(cfg.get("atm_straddle_min_pct", 0.014))
        self.atm_straddle_max_pct = float(cfg.get("atm_straddle_max_pct", 0.030))
        self.atr_max_pct_5m = float(cfg.get("atr_max_pct_5m", 0.0045))

        self.ema_period_5m = int(cfg.get("ema_period_5m", 20))
        self.rsi_period_5m = int(cfg.get("rsi_period_5m", 14))
        self.bullish_rsi_min = float(cfg.get("bullish_rsi_min", 55.0))
        self.bullish_rsi_max = float(cfg.get("bullish_rsi_max", 70.0))
        self.bearish_rsi_min = float(cfg.get("bearish_rsi_min", 30.0))
        self.bearish_rsi_max = float(cfg.get("bearish_rsi_max", 45.0))
        self.neutral_rsi_min = float(cfg.get("neutral_rsi_min", 45.0))
        self.neutral_rsi_max = float(cfg.get("neutral_rsi_max", 55.0))
        self.macd_hist_flat_factor = float(cfg.get("macd_hist_flat_factor", 0.0002))
        self.flat_range_lookback_bars = int(cfg.get("flat_range_lookback_bars", 10))
        self.flat_range_max_pct = float(cfg.get("flat_range_max_pct", 0.008))

        self.short_otm_pct = float(cfg.get("short_otm_pct", 0.008))
        self.condor_short_otm_pct = float(cfg.get("condor_short_otm_pct", 0.012))
        self.spread_width_pct = float(cfg.get("spread_width_pct", 0.015))
        self.min_width_pts = float(cfg.get("min_width_pts", 150.0))
        self.min_credit_pct_of_width = float(cfg.get("min_credit_pct_of_width", 0.30))
        self.min_condor_total_credit_pct = float(cfg.get("min_condor_total_credit_pct", 0.35))

        self.risk_per_trade_pct = float(cfg.get("risk_per_trade_pct", 0.01))
        self.max_total_risk_pct = float(cfg.get("max_total_risk_pct", 0.05))
        self.max_open_spreads = int(cfg.get("max_open_spreads", 3))

        self.take_profit_pct_of_credit = float(cfg.get("take_profit_pct_of_credit", 0.60))
        self.stop_loss_mult_credit = float(cfg.get("stop_loss_mult_credit", 1.8))

        acct_eq = cfg.get("account_equity")
        if acct_eq is None:
            raise ValueError("account_equity must be provided for NiftyWeeklyCreditSpreadStrategy")
        self.account_equity = float(acct_eq)

        self._strategy_id = NIFTY_WEEKLY_CREDIT_SPREADS_ID
        self.product_type = "INTRADAY"
        self.trade_mode = "PAPER"
        settings = get_settings()
        self._use_hub_router_flag = settings.use_hub_router_for_nifty_options

        self.last_price: Dict[str, float] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self.last_5m_closes: List[float] = []
        self.open_spreads: Dict[str, OpenSpread] = {}
        self.timeframe_seconds = 300  # 5m
        self._exit_in_flight_spreads: set[str] = set()
        self._next_exit_retry_mono_by_spread: Dict[str, float] = {}
        retry_cfg = self._cfg.get_float(
            "NIFTY_SPREAD_EXIT_RETRY_COOLDOWN_SECONDS",
            5.0,
        )
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(
            1,
            self._cfg.get_int("NIFTY_SPREAD_EXIT_MAX_RETRIES", 12),
        )
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_float("NIFTY_SPREAD_EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_float("NIFTY_SPREAD_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count_by_spread: Dict[str, int] = {}
        self._exit_circuit_open_until_mono_by_spread: Dict[str, float] = {}
        self._exit_last_alert_mono_by_spread: Dict[str, float] = {}

        logger.info(
            "[NIFTY_SPREAD] Exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        self._restore_spreads_from_risk_manager()

    # ---- base hooks ----
    # Handle tick updates and manage exits.
    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        now = datetime.now(tz=IST)
        if label == self.underlying_label:
            self._maybe_manage_exits(now, underlying_close=ltp)

    # Handle bar-close updates for entries and exits.
    def on_bar(self, label: str, timeframe_seconds: int, candle: Any, indicators: Dict[str, Any]) -> None:
        if label != self.underlying_label or timeframe_seconds != self.timeframe_seconds:
            return
        close = candle.c
        self.last_price[label] = close
        self.last_5m_closes.append(close)
        if len(self.last_5m_closes) > max(50, self.flat_range_lookback_bars):
            self.last_5m_closes = self.last_5m_closes[-50:]
        self._maybe_manage_exits(candle.start_ts.astimezone(IST), underlying_close=close)
        self._maybe_enter(candle, indicators)

    # ---- helpers ----
    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    @staticmethod
    def _normalize_underlying_key(value: Any) -> str:
        return str(value or "").strip().upper().replace("_IDX", "")

    def _strategy_matches_restored_entry(self, entry: Dict[str, Any]) -> bool:
        for raw_name in (
            str(entry.get("strategy_name") or "").strip(),
            str(entry.get("template_name") or "").strip(),
        ):
            if not raw_name:
                continue
            if raw_name == self._strategy_id:
                return True
            if (
                canonicalize_strategy_name(
                    raw_name,
                    source="nifty_weekly_credit_spreads.restore",
                    warn_alias=False,
                )
                == "nifty_weekly_credit_spreads"
            ):
                return True
        return False

    @staticmethod
    def _parse_expiry(value: Any) -> Optional[date]:
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text).date()
        except Exception:
            pass
        for fmt in ("%d%b%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except Exception:
                continue
        return None

    def _infer_option_type(
        self,
        label: str,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        resolved_meta = meta or self.instrument_meta.get(label, {})
        kind = str(resolved_meta.get("kind") or "").strip().upper()
        label_upper = str(label or "").strip().upper()
        if "CE" in kind or label_upper.endswith("CE") or "_CE_" in label_upper:
            return "CE"
        if "PE" in kind or label_upper.endswith("PE") or "_PE_" in label_upper:
            return "PE"
        return None

    def _spread_type_for_option_type(self, option_type: Optional[str]) -> str:
        return "CALL_SPREAD" if str(option_type or "").upper() == "CE" else "PUT_SPREAD"

    def _build_spread_leg(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        entry_price: float,
        meta: Optional[Dict[str, Any]] = None,
        broker_symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol_token: Optional[str] = None,
    ) -> SpreadLeg:
        meta or self.instrument_meta.get(label, {})
        resolved_symbol, resolved_exchange, resolved_token = self._resolve_order_identity(
            label
        )
        return SpreadLeg(
            label=label,
            side=str(side or "").strip().upper(),
            strike=self._strike_from_label(label) or 0.0,
            qty=max(1, int(qty)),
            entry_price=float(entry_price),
            broker_symbol=broker_symbol or resolved_symbol,
            exchange=(
                str(exchange)
                if exchange not in (None, "")
                else resolved_exchange
            ),
            symbol_token=(
                str(symbol_token)
                if symbol_token not in (None, "")
                else resolved_token
            ),
        )

    @staticmethod
    def _serialize_spread_leg(leg: SpreadLeg) -> Dict[str, Any]:
        return {
            "label": leg.label,
            "side": leg.side,
            "strike": float(leg.strike),
            "qty": int(leg.qty),
            "entry_price": float(leg.entry_price),
            "broker_symbol": leg.broker_symbol,
            "exchange": leg.exchange,
            "symbol_token": leg.symbol_token,
        }

    def _deserialize_spread_leg(self, raw: Any) -> Optional[SpreadLeg]:
        if not isinstance(raw, dict):
            return None
        label = str(raw.get("label") or "").strip()
        side = str(raw.get("side") or "").strip().upper()
        qty = parse_positive_int(raw.get("qty"))
        strike = parse_non_negative_float(raw.get("strike"))
        entry_price = parse_non_negative_float(raw.get("entry_price"))
        if not label or side not in {"BUY", "SELL"} or qty is None:
            return None
        return SpreadLeg(
            label=label,
            side=side,
            strike=float(strike or self._strike_from_label(label) or 0.0),
            qty=int(qty),
            entry_price=float(entry_price or 0.0),
            broker_symbol=str(raw.get("broker_symbol") or "").strip() or None,
            exchange=str(raw.get("exchange") or "").strip() or None,
            symbol_token=str(raw.get("symbol_token") or "").strip() or None,
        )

    def _placeholder_spread_leg(
        self,
        *,
        spread_id: str,
        role: str,
        side: str,
        reference_leg: Optional[SpreadLeg] = None,
    ) -> SpreadLeg:
        reference_qty = reference_leg.qty if reference_leg else 1
        reference_strike = reference_leg.strike if reference_leg else 0.0
        return SpreadLeg(
            label=f"{spread_id}_{role}_MISSING",
            side=side,
            strike=reference_strike,
            qty=max(1, int(reference_qty)),
            entry_price=0.0,
        )

    def _spread_leg_strategy_context(
        self,
        spread: OpenSpread,
        *,
        leg_role: str,
    ) -> Dict[str, Any]:
        return {
            "spread_state_version": 1,
            "spread_id": spread.spread_id,
            "spread_type": spread.spread_type,
            "spread_leg_role": leg_role,
            "entry_credit": float(spread.entry_credit),
            "width_pts": float(spread.width_pts),
            "max_loss_rupees": float(spread.max_loss_rupees),
            "entry_time": spread.entry_time.astimezone(timezone.utc).isoformat(),
            "expiry": spread.expiry.isoformat(),
            "short_leg": self._serialize_spread_leg(spread.short_leg),
            "long_leg": self._serialize_spread_leg(spread.long_leg),
            "extra_legs": [
                self._serialize_spread_leg(extra_leg) for extra_leg in spread.extra_legs
            ],
            "short_exit_done": bool(spread.short_exit_done),
            "long_exit_done": bool(spread.long_exit_done),
            "managed_exit_mode": "strategy",
            "broker_brackets_enabled": False,
        }

    def _sync_spread_state_to_risk_manager(self, spread: OpenSpread) -> None:
        if self.risk_manager is None:
            return
        open_positions = getattr(self.risk_manager, "open_positions", None)
        tracked_labels = set(open_positions.keys()) if isinstance(open_positions, dict) else set()
        for leg_role, leg, exit_done in (
            ("short", spread.short_leg, spread.short_exit_done),
            ("long", spread.long_leg, spread.long_exit_done),
        ):
            if exit_done and leg.label not in tracked_labels:
                continue
            sync_open_position_state(
                risk_manager=self.risk_manager,
                label=leg.label,
                side=leg.side,
                qty=max(1, int(leg.qty)),
                entry_price=float(leg.entry_price),
                strategy_context=self._spread_leg_strategy_context(
                    spread,
                    leg_role=leg_role,
                ),
            )

    def _restore_spreads_from_risk_manager(self) -> None:
        if self.open_spreads or self.risk_manager is None:
            return
        restored_positions = getattr(self.risk_manager, "restored_positions", None)
        if not isinstance(restored_positions, dict) or not restored_positions:
            return

        context_groups: Dict[str, List[Dict[str, Any]]] = {}
        legacy_entries: List[Dict[str, Any]] = []
        normalized_underlying = self._normalize_underlying_key(self.underlying_label)

        for raw_label, raw_entry in restored_positions.items():
            label = str(raw_label or "").strip()
            if not label or not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            if not self._strategy_matches_restored_entry(entry):
                continue
            meta = self.instrument_meta.get(label, {})
            restored_underlying = self._normalize_underlying_key(
                entry.get("underlying") or meta.get("underlying")
            )
            if restored_underlying and restored_underlying != normalized_underlying:
                continue
            side = str(entry.get("side") or "").strip().upper()
            qty = parse_positive_int(entry.get("qty"))
            entry_price = parse_non_negative_float(entry.get("entry_price"))
            if side not in {"BUY", "SELL"} or qty is None or entry_price is None:
                continue
            strategy_context = (
                dict(entry.get("strategy_context"))
                if isinstance(entry.get("strategy_context"), dict)
                else {}
            )
            payload = {
                "label": label,
                "entry": entry,
                "meta": meta,
                "strategy_context": strategy_context,
            }
            spread_id = str(strategy_context.get("spread_id") or "").strip()
            if spread_id:
                context_groups.setdefault(spread_id, []).append(payload)
            else:
                legacy_entries.append(payload)

        for spread_id, entries in context_groups.items():
            restored = self._restore_spread_from_context(
                spread_id=spread_id,
                entries=entries,
            )
            if restored is not None:
                self.open_spreads[spread_id] = restored

        for restored in self._restore_legacy_spreads(legacy_entries):
            self.open_spreads[restored.spread_id] = restored

        if self.open_spreads:
            logger.info(
                "[NIFTY_SPREAD] Restored weekly credit spreads after restart | count=%d spread_ids=%s",
                len(self.open_spreads),
                sorted(self.open_spreads.keys()),
            )

    def _restore_spread_from_context(
        self,
        *,
        spread_id: str,
        entries: List[Dict[str, Any]],
    ) -> Optional[OpenSpread]:
        if not entries:
            return None

        base_entry = entries[0]
        base_context = dict(base_entry.get("strategy_context") or {})
        short_leg = self._deserialize_spread_leg(base_context.get("short_leg"))
        long_leg = self._deserialize_spread_leg(base_context.get("long_leg"))
        extra_legs = [
            leg
            for leg in (
                self._deserialize_spread_leg(raw_leg)
                for raw_leg in (base_context.get("extra_legs") or [])
            )
            if leg is not None
        ]
        short_exit_done = parse_bool(base_context.get("short_exit_done"), default=False)
        long_exit_done = parse_bool(base_context.get("long_exit_done"), default=False)
        entry_time_candidates = [
            parse_datetime(item.get("entry", {}).get("entry_ts")) for item in entries
        ]
        entry_time = (
            parse_datetime(base_context.get("entry_time"))
            or min(
                (candidate for candidate in entry_time_candidates if candidate is not None),
                default=None,
            )
            or datetime.now(timezone.utc)
        )
        expiry = (
            self._parse_expiry(base_context.get("expiry"))
            or self._parse_expiry(
                base_entry.get("entry", {}).get("expiry")
                or base_entry.get("meta", {}).get("expiry")
                or base_entry.get("meta", {}).get("expiry_dt")
            )
            or entry_time.astimezone(IST).date()
        )
        present_roles: set[str] = set()

        for item in entries:
            label = str(item.get("label") or "").strip()
            entry = dict(item.get("entry") or {})
            meta = dict(item.get("meta") or {})
            strategy_context = dict(item.get("strategy_context") or {})
            qty = parse_positive_int(entry.get("qty"))
            entry_price = parse_non_negative_float(entry.get("entry_price"))
            side = str(entry.get("side") or "").strip().upper()
            if not label or qty is None or entry_price is None or side not in {"BUY", "SELL"}:
                continue
            restored_leg = self._build_spread_leg(
                label=label,
                side=side,
                qty=qty,
                entry_price=entry_price,
                meta=meta,
                broker_symbol=str(
                    entry.get("tradingsymbol")
                    or self._resolve_broker_symbol(meta, label)
                ).strip()
                or None,
                exchange=str(
                    entry.get("exchange")
                    or meta.get("exchange")
                    or meta.get("exch_seg")
                    or ""
                ).strip()
                or None,
                symbol_token=str(
                    entry.get("symboltoken")
                    or meta.get("token")
                    or self._resolve_symbol_token(meta)
                    or ""
                ).strip()
                or None,
            )
            role_hint = str(strategy_context.get("spread_leg_role") or "").strip().lower()
            role = role_hint if role_hint in {"short", "long"} else ("short" if side == "SELL" else "long")
            if role == "short":
                if short_leg is not None and short_leg.qty != restored_leg.qty:
                    logger.warning(
                        "[NIFTY_SPREAD] Restored short-leg qty reconciled to broker state | spread_id=%s label=%s stored_qty=%s broker_qty=%s",
                        spread_id,
                        label,
                        short_leg.qty,
                        restored_leg.qty,
                    )
                short_leg = restored_leg
                short_exit_done = False
            else:
                if long_leg is not None and long_leg.qty != restored_leg.qty:
                    logger.warning(
                        "[NIFTY_SPREAD] Restored long-leg qty reconciled to broker state | spread_id=%s label=%s stored_qty=%s broker_qty=%s",
                        spread_id,
                        label,
                        long_leg.qty,
                        restored_leg.qty,
                    )
                long_leg = restored_leg
                long_exit_done = False
            present_roles.add(role)
            self._order_identity_by_label[label] = {
                "broker_symbol": restored_leg.broker_symbol,
                "exchange": restored_leg.exchange,
                "symbol_token": restored_leg.symbol_token,
            }

        if short_leg is None and long_leg is None:
            return None
        if short_leg is None:
            short_leg = self._placeholder_spread_leg(
                spread_id=spread_id,
                role="SHORT",
                side="SELL",
                reference_leg=long_leg,
            )
        if long_leg is None:
            long_leg = self._placeholder_spread_leg(
                spread_id=spread_id,
                role="LONG",
                side="BUY",
                reference_leg=short_leg,
            )

        if "short" not in present_roles:
            short_exit_done = True
        if "long" not in present_roles:
            long_exit_done = True

        width_pts = parse_non_negative_float(base_context.get("width_pts"))
        if width_pts is None:
            width_pts = abs(float(short_leg.strike) - float(long_leg.strike))
        entry_credit = parse_non_negative_float(base_context.get("entry_credit"))
        if entry_credit is None:
            entry_credit = max(
                0.0,
                float(short_leg.entry_price) - float(long_leg.entry_price),
            )
        max_loss_rupees = parse_non_negative_float(base_context.get("max_loss_rupees"))
        if max_loss_rupees is None:
            max_loss_rupees = max(
                0.0,
                float(width_pts) - float(entry_credit),
            ) * max(int(short_leg.qty), int(long_leg.qty))

        spread_type = str(base_context.get("spread_type") or "").strip().upper()
        if not spread_type:
            spread_type = self._spread_type_for_option_type(
                self._infer_option_type(short_leg.label)
                or self._infer_option_type(long_leg.label)
            )

        self._reset_exit_retry_state(spread_id)
        return OpenSpread(
            spread_id=spread_id,
            spread_type=spread_type,
            short_leg=short_leg,
            long_leg=long_leg,
            entry_credit=float(entry_credit),
            width_pts=float(width_pts),
            max_loss_rupees=float(max_loss_rupees),
            entry_time=entry_time.astimezone(IST),
            expiry=expiry,
            extra_legs=extra_legs,
            short_exit_done=bool(short_exit_done),
            long_exit_done=bool(long_exit_done),
        )

    def _restore_legacy_spreads(self, entries: List[Dict[str, Any]]) -> List[OpenSpread]:
        if not entries:
            return []

        grouped: Dict[Tuple[str, date], Dict[str, List[Dict[str, Any]]]] = {}
        for item in entries:
            label = str(item.get("label") or "").strip()
            entry = dict(item.get("entry") or {})
            meta = dict(item.get("meta") or {})
            side = str(entry.get("side") or "").strip().upper()
            qty = parse_positive_int(entry.get("qty"))
            entry_price = parse_non_negative_float(entry.get("entry_price"))
            entry_time = parse_datetime(entry.get("entry_ts"))
            option_type = self._infer_option_type(label, meta=meta)
            expiry = self._parse_expiry(
                entry.get("expiry") or meta.get("expiry") or meta.get("expiry_dt")
            )
            if (
                not label
                or side not in {"BUY", "SELL"}
                or qty is None
                or entry_price is None
                or entry_time is None
                or option_type not in {"CE", "PE"}
                or expiry is None
            ):
                continue
            grouped.setdefault((option_type, expiry), {"SELL": [], "BUY": []})[side].append(item)

        restored_spreads: List[OpenSpread] = []
        legacy_index = 0
        for (option_type, expiry), side_groups in grouped.items():
            sells = sorted(
                side_groups.get("SELL") or [],
                key=lambda item: parse_datetime(item["entry"].get("entry_ts")) or datetime.now(timezone.utc),
            )
            buys = list(
                sorted(
                    side_groups.get("BUY") or [],
                    key=lambda item: parse_datetime(item["entry"].get("entry_ts")) or datetime.now(timezone.utc),
                )
            )
            while sells:
                sell_item = sells.pop(0)
                buy_item = None
                if buys:
                    sell_label = str(sell_item.get("label") or "")
                    sell_strike = self._strike_from_label(sell_label) or 0.0
                    best_buy_idx = min(
                        range(len(buys)),
                        key=lambda idx: (
                            abs((self._strike_from_label(str(buys[idx].get("label") or "")) or 0.0) - sell_strike),
                            abs(
                                (
                                    (
                                        parse_datetime(buys[idx]["entry"].get("entry_ts"))
                                        or datetime.now(timezone.utc)
                                    )
                                    - (
                                        parse_datetime(sell_item["entry"].get("entry_ts"))
                                        or datetime.now(timezone.utc)
                                    )
                                ).total_seconds()
                            ),
                        ),
                    )
                    buy_item = buys.pop(best_buy_idx)
                restored = self._restore_legacy_spread_pair(
                    option_type=option_type,
                    expiry=expiry,
                    sell_item=sell_item,
                    buy_item=buy_item,
                    legacy_index=legacy_index,
                )
                legacy_index += 1
                if restored is not None:
                    restored_spreads.append(restored)
            for buy_item in buys:
                restored = self._restore_legacy_spread_pair(
                    option_type=option_type,
                    expiry=expiry,
                    sell_item=None,
                    buy_item=buy_item,
                    legacy_index=legacy_index,
                )
                legacy_index += 1
                if restored is not None:
                    restored_spreads.append(restored)
        return restored_spreads

    def _restore_legacy_spread_pair(
        self,
        *,
        option_type: str,
        expiry: date,
        sell_item: Optional[Dict[str, Any]],
        buy_item: Optional[Dict[str, Any]],
        legacy_index: int,
    ) -> Optional[OpenSpread]:
        if sell_item is None and buy_item is None:
            return None

        spread_type = self._spread_type_for_option_type(option_type)
        spread_id = f"RESTORED_LEGACY_{spread_type}_{expiry.isoformat()}_{legacy_index}"
        short_leg = None
        long_leg = None
        entry_times: List[datetime] = []

        if sell_item is not None:
            sell_label = str(sell_item.get("label") or "").strip()
            sell_entry = dict(sell_item.get("entry") or {})
            sell_meta = dict(sell_item.get("meta") or {})
            sell_qty = parse_positive_int(sell_entry.get("qty"))
            sell_price = parse_non_negative_float(sell_entry.get("entry_price"))
            sell_entry_time = parse_datetime(sell_entry.get("entry_ts"))
            if sell_label and sell_qty is not None and sell_price is not None:
                short_leg = self._build_spread_leg(
                    label=sell_label,
                    side="SELL",
                    qty=sell_qty,
                    entry_price=sell_price,
                    meta=sell_meta,
                    broker_symbol=str(
                        sell_entry.get("tradingsymbol")
                        or self._resolve_broker_symbol(sell_meta, sell_label)
                    ).strip()
                    or None,
                    exchange=str(
                        sell_entry.get("exchange")
                        or sell_meta.get("exchange")
                        or sell_meta.get("exch_seg")
                        or ""
                    ).strip()
                    or None,
                    symbol_token=str(
                        sell_entry.get("symboltoken")
                        or sell_meta.get("token")
                        or self._resolve_symbol_token(sell_meta)
                        or ""
                    ).strip()
                    or None,
                )
                self._order_identity_by_label[sell_label] = {
                    "broker_symbol": short_leg.broker_symbol,
                    "exchange": short_leg.exchange,
                    "symbol_token": short_leg.symbol_token,
                }
                if sell_entry_time is not None:
                    entry_times.append(sell_entry_time)

        if buy_item is not None:
            buy_label = str(buy_item.get("label") or "").strip()
            buy_entry = dict(buy_item.get("entry") or {})
            buy_meta = dict(buy_item.get("meta") or {})
            buy_qty = parse_positive_int(buy_entry.get("qty"))
            buy_price = parse_non_negative_float(buy_entry.get("entry_price"))
            buy_entry_time = parse_datetime(buy_entry.get("entry_ts"))
            if buy_label and buy_qty is not None and buy_price is not None:
                long_leg = self._build_spread_leg(
                    label=buy_label,
                    side="BUY",
                    qty=buy_qty,
                    entry_price=buy_price,
                    meta=buy_meta,
                    broker_symbol=str(
                        buy_entry.get("tradingsymbol")
                        or self._resolve_broker_symbol(buy_meta, buy_label)
                    ).strip()
                    or None,
                    exchange=str(
                        buy_entry.get("exchange")
                        or buy_meta.get("exchange")
                        or buy_meta.get("exch_seg")
                        or ""
                    ).strip()
                    or None,
                    symbol_token=str(
                        buy_entry.get("symboltoken")
                        or buy_meta.get("token")
                        or self._resolve_symbol_token(buy_meta)
                        or ""
                    ).strip()
                    or None,
                )
                self._order_identity_by_label[buy_label] = {
                    "broker_symbol": long_leg.broker_symbol,
                    "exchange": long_leg.exchange,
                    "symbol_token": long_leg.symbol_token,
                }
                if buy_entry_time is not None:
                    entry_times.append(buy_entry_time)

        if short_leg is None and long_leg is None:
            return None
        if short_leg is None:
            short_leg = self._placeholder_spread_leg(
                spread_id=spread_id,
                role="SHORT",
                side="SELL",
                reference_leg=long_leg,
            )
        if long_leg is None:
            long_leg = self._placeholder_spread_leg(
                spread_id=spread_id,
                role="LONG",
                side="BUY",
                reference_leg=short_leg,
            )

        entry_credit = max(
            0.0,
            float(short_leg.entry_price) - float(long_leg.entry_price),
        )
        width_pts = abs(float(short_leg.strike) - float(long_leg.strike))
        max_loss_rupees = max(
            0.0,
            float(width_pts) - float(entry_credit),
        ) * max(int(short_leg.qty), int(long_leg.qty))
        self._reset_exit_retry_state(spread_id)
        return OpenSpread(
            spread_id=spread_id,
            spread_type=spread_type,
            short_leg=short_leg,
            long_leg=long_leg,
            entry_credit=float(entry_credit),
            width_pts=float(width_pts),
            max_loss_rupees=float(max_loss_rupees),
            entry_time=(
                min(entry_times).astimezone(IST)
                if entry_times
                else datetime.now(tz=IST)
            ),
            expiry=expiry,
            short_exit_done=sell_item is None,
            long_exit_done=buy_item is None,
        )

    # Find the nearest valid expiry date for NIFTY options.
    def _current_expiry_and_chain(self) -> Optional[date]:
        today = datetime.now(tz=IST).date()
        expiries = []
        for meta in self.instrument_meta.values():
            if str(meta.get("underlying", "")).upper() != "NIFTY":
                continue
            if str(meta.get("kind", "")).upper() not in {"ATM_CE", "ATM_PE", "OTM_CE", "OTM_PE", "ITM_CE", "ITM_PE", "OPTION"}:
                continue
            exp_raw = meta.get("expiry") or meta.get("expiry_dt")
            if not exp_raw:
                continue
            try:
                if isinstance(exp_raw, date):
                    exp_dt = exp_raw
                else:
                    exp_dt = datetime.fromisoformat(str(exp_raw)).date()
            except Exception:
                try:
                    exp_dt = datetime.strptime(str(exp_raw), "%d%b%Y").date()
                except Exception:
                    continue
            if exp_dt >= today:
                expiries.append(exp_dt)
        if not expiries:
            return None
        return min(expiries)

    # Extract strike price from an option label.
    def _strike_from_label(self, label: str) -> Optional[float]:
        try:
            return float(label.rsplit("_", 1)[-1])
        except Exception:
            return None

    # Find the closest option label for a strike/type/expiry.
    def _find_option_by_strike(self, strike: float, option_type: str, expiry: date) -> Optional[str]:
        chosen = None
        best_dist = None
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != "NIFTY":
                continue
            kind = str(meta.get("kind", "")).upper()
            if option_type == "CE" and "CE" not in kind:
                continue
            if option_type == "PE" and "PE" not in kind:
                continue
            meta_exp = meta.get("expiry") or meta.get("expiry_dt")
            if meta_exp:
                try:
                    if isinstance(meta_exp, date):
                        exp_dt = meta_exp
                    else:
                        exp_dt = datetime.fromisoformat(str(meta_exp)).date()
                except Exception:
                    try:
                        exp_dt = datetime.strptime(str(meta_exp), "%d%b%Y").date()
                    except Exception:
                        exp_dt = None
                if exp_dt and exp_dt != expiry:
                    continue
            lbl_strike = self._strike_from_label(lbl)
            if lbl_strike is None:
                continue
            dist = abs(lbl_strike - strike)
            if best_dist is None or dist < best_dist:
                best_dist = dist
                chosen = lbl
        return chosen

    # Return the latest cached price for a label.
    def _latest_price(self, label: str) -> Optional[float]:
        return self.last_price.get(label)

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
    def _reset_exit_retry_state(self, spread_id: str) -> None:
        self._next_exit_retry_mono_by_spread.pop(spread_id, None)
        self._exit_failure_count_by_spread.pop(spread_id, None)
        self._exit_circuit_open_until_mono_by_spread.pop(spread_id, None)
        self._exit_last_alert_mono_by_spread.pop(spread_id, None)

    # Register a failed exit attempt and schedule either retry or circuit open.
    def _register_exit_failure(
        self, *, spread_id: str, now_mono: float
    ) -> tuple[int, float, bool]:
        attempt = int(self._exit_failure_count_by_spread.get(spread_id, 0)) + 1
        self._exit_failure_count_by_spread[spread_id] = attempt
        if attempt >= self._exit_max_retries:
            self._exit_circuit_open_until_mono_by_spread[spread_id] = (
                now_mono + self._exit_circuit_open_seconds
            )
            self._next_exit_retry_mono_by_spread[spread_id] = (
                now_mono + self._exit_circuit_open_seconds
            )
            self._exit_last_alert_mono_by_spread[spread_id] = now_mono
            return attempt, self._exit_circuit_open_seconds, True
        self._next_exit_retry_mono_by_spread[spread_id] = (
            now_mono + self._exit_retry_cooldown_seconds
        )
        return attempt, self._exit_retry_cooldown_seconds, False

    # Place an order via hub or legacy bridge.
    def _place_order(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: float,
        tag: str,
        purpose: Optional[OrderPurpose] = None,
        broker_symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol_token: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> OrderResponse:
        meta = self.instrument_meta.get(label)
        if not meta and label not in self._order_identity_by_label:
            return OrderResponse(
                broker_order_id=label,
                status="REJECTED",
                message="meta_missing",
                filled_quantity=0,
                average_price=None,
            )
        resolved_symbol, resolved_exchange, resolved_token = self._resolve_order_identity(label)
        order_req = OrderRequest(
            symbol=broker_symbol or resolved_symbol,
            quantity=qty,
            side=OrderSide(side.upper()),
            order_type=OrderType.MARKET,
            product_type=ProductType(self.product_type),
            time_in_force=TimeInForce.DAY,
            limit_price=None,
            stop_price=None,
            tag=tag,
            purpose=purpose,
            exchange=exchange or resolved_exchange,
            symbol_token=(
                str(symbol_token)
                if symbol_token not in (None, "")
                else (str(resolved_token) if resolved_token not in (None, "") else None)
            ),
            position_label=label,
            strategy_context=dict(strategy_context) if strategy_context else None,
        )
        routing_kwargs = self._routing_kwargs()
        return place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            **routing_kwargs,
        )

    # ---- core logic ----
    # Evaluate entry conditions and open spreads if signals align.
    def _log_signal_evaluation(self, reason: str, **details: Any) -> None:
        """Emit a structured ``signal_evaluated_with_reason=<reason>`` line at
        DEBUG. This makes per-bar gate-rejection visible without flooding
        normal INFO logs.

        Enable for an investigation by setting the ``PHOENIX_DEBUG_LOGGERS``
        env var (comma-separated list of dotted logger names) on the
        backend container before start::

            PHOENIX_DEBUG_LOGGERS=app.strategies.nifty_weekly_credit_spreads

        The handler is implemented in ``app.main._configure_logging`` -- each
        named logger is set to DEBUG independently of the global
        ``LOG_LEVEL``, so the rest of the stack stays at INFO. Removing the
        env var on the next restart returns the logger to its default level.
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return
        parts = [f"signal_evaluated_with_reason={reason}"]
        for key, value in details.items():
            if isinstance(value, float):
                parts.append(f"{key}={value:.6g}")
            else:
                parts.append(f"{key}={value}")
        logger.debug("[NIFTY_SPREAD] %s", " ".join(parts))

    def _maybe_enter(self, candle: Any, indicators: Dict[str, Any]) -> None:
        now = candle.start_ts.astimezone(IST)
        if not (self.entry_start_time <= now.time() <= self.entry_end_time):
            self._log_signal_evaluation(
                "outside_entry_window",
                ist_time=now.time().isoformat(timespec="seconds"),
                window=f"{self.entry_start_time}-{self.entry_end_time}",
            )
            return

        expiry = self._current_expiry_and_chain()
        if not expiry:
            self._log_signal_evaluation("no_current_expiry_or_chain")
            return
        days_to_exp = (expiry - now.date()).days
        if days_to_exp < self.min_days_to_expiry or days_to_exp > self.max_days_to_expiry:
            self._log_signal_evaluation(
                "expiry_out_of_dte_range",
                days_to_exp=days_to_exp,
                min_dte=self.min_days_to_expiry,
                max_dte=self.max_days_to_expiry,
                expiry=expiry.isoformat(),
            )
            return

        close = candle.c
        ema = indicators.get(f"ema_{self.ema_period_5m}") or indicators.get("ema_20")
        rsi = indicators.get("rsi")
        macd_hist = indicators.get("macd_hist")
        atr = indicators.get("atr")
        if None in (ema, rsi, macd_hist, atr):
            self._log_signal_evaluation(
                "indicators_not_ready",
                ema_set=ema is not None,
                rsi_set=rsi is not None,
                macd_hist_set=macd_hist is not None,
                atr_set=atr is not None,
            )
            return

        atr_pct = atr / close if close else 0.0
        if atr_pct > self.atr_max_pct_5m:
            self._log_signal_evaluation(
                "atr_too_high",
                atr_pct=atr_pct,
                atr_max_pct_5m=self.atr_max_pct_5m,
                atr=atr,
                close=close,
            )
            return

        # regime classification
        bullish = close > ema and self.bullish_rsi_min <= rsi <= self.bullish_rsi_max and macd_hist > 0
        bearish = close < ema and self.bearish_rsi_min <= rsi <= self.bearish_rsi_max and macd_hist < 0
        sideways = False
        flat_range_pct: Optional[float] = None
        flat_range_lookback_ready = (
            len(self.last_5m_closes) >= self.flat_range_lookback_bars
        )
        if self.neutral_rsi_min <= rsi <= self.neutral_rsi_max and abs(macd_hist) <= self.macd_hist_flat_factor * close:
            if flat_range_lookback_ready:
                window = self.last_5m_closes[-self.flat_range_lookback_bars :]
                if window:
                    rng = max(window) - min(window)
                    flat_range_pct = rng / close if close else 0.0
                    if rng <= self.flat_range_max_pct * close:
                        sideways = True

        regime = "none"
        if bullish:
            regime = "bullish"
        elif bearish:
            regime = "bearish"
        elif sideways:
            regime = "sideways"

        if regime == "none":
            self._log_signal_evaluation(
                "regime_none",
                close=close,
                ema=ema,
                rsi=rsi,
                macd_hist=macd_hist,
                bullish_eligible=bullish,
                bearish_eligible=bearish,
                sideways_eligible=sideways,
                flat_range_lookback_ready=flat_range_lookback_ready,
                flat_range_pct=flat_range_pct,
            )
            return

        # ATM straddle check
        atm_ce, atm_pe = self._atm_options(expiry)
        if not atm_ce or not atm_pe:
            self._log_signal_evaluation(
                "atm_options_unavailable",
                atm_ce_found=bool(atm_ce),
                atm_pe_found=bool(atm_pe),
                expiry=expiry.isoformat(),
                regime=regime,
            )
            return
        atm_ce_price_raw = self._latest_price(atm_ce)
        atm_pe_price_raw = self._latest_price(atm_pe)
        # Codex round-3 #3: distinguish "data unavailable" from "out of range".
        # Without this, a missing quote silently coerced (None -> 0.0) and the
        # straddle pct came out as 0% -> reported as out_of_range, sending ops
        # to tune ratio bands when the real blocker is the LTP feed.
        if atm_ce_price_raw is None or atm_pe_price_raw is None:
            self._log_signal_evaluation(
                "atm_quote_unavailable",
                atm_ce=atm_ce,
                atm_pe=atm_pe,
                atm_ce_price_set=atm_ce_price_raw is not None,
                atm_pe_price_set=atm_pe_price_raw is not None,
                regime=regime,
            )
            return
        atm_ce_price = float(atm_ce_price_raw)
        atm_pe_price = float(atm_pe_price_raw)
        atm_straddle_pct = (atm_ce_price + atm_pe_price) / close if close else 0.0
        if atm_straddle_pct < self.atm_straddle_min_pct or atm_straddle_pct > self.atm_straddle_max_pct:
            self._log_signal_evaluation(
                "atm_straddle_pct_out_of_range",
                atm_straddle_pct=atm_straddle_pct,
                min=self.atm_straddle_min_pct,
                max=self.atm_straddle_max_pct,
                regime=regime,
                atm_ce_price=atm_ce_price,
                atm_pe_price=atm_pe_price,
                close=close,
            )
            return

        # Top-level gates passed; dispatch to the spread builder. We do NOT
        # log "entry passed" here because the builder may still reject on
        # legs/credit/risk -- a "gates passed" line followed by a downstream
        # rejection on the same bar is misleading for ops counts/alerts.
        # The success signal is emitted only from _enter_spread when the
        # order actually lands (signal_evaluated_with_reason=spread_opened).
        if regime == "bullish":
            self._try_put_spread(close, expiry)
        elif regime == "bearish":
            self._try_call_spread(close, expiry)
        elif regime == "sideways":
            self._try_iron_condor(close, expiry)

    # Find ATM CE/PE labels for a given expiry.
    def _atm_options(self, expiry: date) -> Tuple[Optional[str], Optional[str]]:
        best_ce = None
        best_pe = None
        ce_dist = None
        pe_dist = None
        last_price_under = self.last_price.get(self.underlying_label)
        if last_price_under is None:
            return None, None
        for lbl, meta in self.instrument_meta.items():
            if str(meta.get("underlying", "")).upper() != "NIFTY":
                continue
            kind = str(meta.get("kind", "")).upper()
            meta_exp = meta.get("expiry") or meta.get("expiry_dt")
            if meta_exp:
                try:
                    exp_dt = meta_exp if isinstance(meta_exp, date) else datetime.fromisoformat(str(meta_exp)).date()
                except Exception:
                    try:
                        exp_dt = datetime.strptime(str(meta_exp), "%d%b%Y").date()
                    except Exception:
                        continue
                if exp_dt != expiry:
                    continue
            strike = self._strike_from_label(lbl)
            if strike is None:
                continue
            if "CE" in kind:
                dist = abs(strike - last_price_under)
                if ce_dist is None or dist < ce_dist:
                    ce_dist = dist
                    best_ce = lbl
            if "PE" in kind:
                dist = abs(strike - last_price_under)
                if pe_dist is None or dist < pe_dist:
                    pe_dist = dist
                    best_pe = lbl
        return best_ce, best_pe

    # Try to open a bullish put spread.
    def _try_put_spread(self, spot: float, expiry: date) -> None:
        width = max(self.spread_width_pct * spot, self.min_width_pts)
        width = round_to_nearest_50(width)
        short_strike = round_down_50(spot * (1 - self.short_otm_pct))
        long_strike = short_strike - width
        self._open_vertical(
            spread_type="PUT_SPREAD",
            short_label=self._find_option_by_strike(short_strike, "PE", expiry),
            long_label=self._find_option_by_strike(long_strike, "PE", expiry),
            width=abs(short_strike - long_strike),
            expiry=expiry,
        )

    # Try to open a bearish call spread.
    def _try_call_spread(self, spot: float, expiry: date) -> None:
        width = max(self.spread_width_pct * spot, self.min_width_pts)
        width = round_to_nearest_50(width)
        short_strike = round_up_50(spot * (1 + self.short_otm_pct))
        long_strike = short_strike + width
        self._open_vertical(
            spread_type="CALL_SPREAD",
            short_label=self._find_option_by_strike(short_strike, "CE", expiry),
            long_label=self._find_option_by_strike(long_strike, "CE", expiry),
            width=abs(short_strike - long_strike),
            expiry=expiry,
        )

    # Try to open an iron condor spread.
    def _try_iron_condor(self, spot: float, expiry: date) -> None:
        width = max(self.spread_width_pct * spot, self.min_width_pts)
        width = round_to_nearest_50(width)
        sp_short_put = round_down_50(spot * (1 - self.condor_short_otm_pct))
        sp_long_put = sp_short_put - width
        sp_short_call = round_up_50(spot * (1 + self.condor_short_otm_pct))
        sp_long_call = sp_short_call + width
        put_short_label = self._find_option_by_strike(sp_short_put, "PE", expiry)
        put_long_label = self._find_option_by_strike(sp_long_put, "PE", expiry)
        call_short_label = self._find_option_by_strike(sp_short_call, "CE", expiry)
        call_long_label = self._find_option_by_strike(sp_long_call, "CE", expiry)
        if not all([put_short_label, put_long_label, call_short_label, call_long_label]):
            self._log_signal_evaluation(
                "condor_legs_unresolved",
                put_short=bool(put_short_label),
                put_long=bool(put_long_label),
                call_short=bool(call_short_label),
                call_long=bool(call_long_label),
                spot=spot,
                width=width,
            )
            return

        # Build both spreads and check credits
        put_credit, put_width, put_qty, put_loss = self._estimate_credit(put_short_label, put_long_label)
        call_credit, call_width, call_qty, call_loss = self._estimate_credit(call_short_label, call_long_label)
        if put_credit is None or call_credit is None:
            self._log_signal_evaluation(
                "condor_credit_estimate_failed",
                put_credit=put_credit,
                call_credit=call_credit,
            )
            return
        if put_credit < self.min_credit_pct_of_width * put_width:
            self._log_signal_evaluation(
                "condor_put_credit_below_min",
                put_credit=put_credit,
                put_width=put_width,
                min_credit=self.min_credit_pct_of_width * put_width,
                min_credit_pct_of_width=self.min_credit_pct_of_width,
            )
            return
        if call_credit < self.min_credit_pct_of_width * call_width:
            self._log_signal_evaluation(
                "condor_call_credit_below_min",
                call_credit=call_credit,
                call_width=call_width,
                min_credit=self.min_credit_pct_of_width * call_width,
                min_credit_pct_of_width=self.min_credit_pct_of_width,
            )
            return
        if (put_credit + call_credit) < self.min_condor_total_credit_pct * width:
            self._log_signal_evaluation(
                "condor_total_credit_below_min",
                total_credit=put_credit + call_credit,
                min_total_credit=self.min_condor_total_credit_pct * width,
                width=width,
                min_condor_total_credit_pct=self.min_condor_total_credit_pct,
            )
            return
        max_loss_rupees = put_loss + call_loss
        if not self._risk_allows(max_loss_rupees):
            self._log_signal_evaluation(
                "condor_risk_disallowed",
                max_loss_rupees=max_loss_rupees,
                per_trade_cap=self.risk_per_trade_pct * self.account_equity,
                total_cap=self.max_total_risk_pct * self.account_equity,
                open_spreads=len(self.open_spreads),
                max_open_spreads=self.max_open_spreads,
            )
            return
        # place orders: put spread then call spread
        if not self._enter_spread("IRON_CONDOR_PUT", put_short_label, put_long_label, put_credit, put_width, expiry, extra=None):
            return
        if not self._enter_spread("IRON_CONDOR_CALL", call_short_label, call_long_label, call_credit, call_width, expiry, extra=None):
            # rollback put side
            self._force_exit_leg(put_short_label, "BUY", self._qty_for_label(put_short_label))
            self._force_exit_leg(put_long_label, "SELL", self._qty_for_label(put_long_label))
            self._log_signal_evaluation(
                "condor_call_side_failed_rolled_back_put",
                put_short=put_short_label,
                put_long=put_long_label,
                call_short=call_short_label,
                call_long=call_long_label,
            )
            return
        # Full condor (all 4 legs) is in place -- emit the unambiguous
        # full-spread-opened signal. (Codex P2 round-3 #1.)
        self._log_signal_evaluation(
            "spread_opened",
            spread_type="IRON_CONDOR",
            put_credit=put_credit,
            call_credit=call_credit,
            total_credit=put_credit + call_credit,
            width=width,
            put_short=put_short_label,
            put_long=put_long_label,
            call_short=call_short_label,
            call_long=call_long_label,
        )

    # Estimate credit, width, and max loss for a vertical spread.
    def _estimate_credit(self, short_label: Optional[str], long_label: Optional[str]) -> Tuple[Optional[float], float, int, float]:
        if not short_label or not long_label:
            return None, 0.0, 0, 0.0
        short_price = self._latest_price(short_label)
        long_price = self._latest_price(long_label)
        if short_price is None or long_price is None:
            return None, 0.0, 0, 0.0
        strike_short = self._strike_from_label(short_label) or 0.0
        strike_long = self._strike_from_label(long_label) or 0.0
        width = abs(strike_short - strike_long)
        credit = short_price - long_price
        qty = self._qty_for_label(short_label)
        max_loss_rupees = (width - credit) * qty
        return credit, width, qty, max_loss_rupees

    # Resolve quantity to trade for a label.
    def _qty_for_label(self, label: str) -> int:
        return max(1, self.lot_size)

    # Check if capital limits allow the spread.
    def _risk_allows(self, max_loss_rupees: float) -> bool:
        per_trade_cap = self.risk_per_trade_pct * self.account_equity
        total_cap = self.max_total_risk_pct * self.account_equity
        current_book = sum(sp.max_loss_rupees for sp in self.open_spreads.values())
        if max_loss_rupees > per_trade_cap:
            return False
        if (current_book + max_loss_rupees) > total_cap:
            return False
        if len(self.open_spreads) >= self.max_open_spreads:
            return False
        return True

    # Open a vertical spread if credit/limits allow.
    def _open_vertical(self, spread_type: str, short_label: Optional[str], long_label: Optional[str], width: float, expiry: date) -> None:
        if not short_label or not long_label:
            self._log_signal_evaluation(
                "vertical_legs_unresolved",
                spread_type=spread_type,
                short=bool(short_label),
                long=bool(long_label),
                width=width,
            )
            return
        credit, width_pts, qty, max_loss_rupees = self._estimate_credit(short_label, long_label)
        if credit is None:
            self._log_signal_evaluation(
                "vertical_credit_estimate_failed",
                spread_type=spread_type,
                short_label=short_label,
                long_label=long_label,
            )
            return
        if credit < self.min_credit_pct_of_width * width_pts:
            self._log_signal_evaluation(
                "vertical_credit_below_min",
                spread_type=spread_type,
                credit=credit,
                width_pts=width_pts,
                min_credit=self.min_credit_pct_of_width * width_pts,
                min_credit_pct_of_width=self.min_credit_pct_of_width,
            )
            return
        if not self._risk_allows(max_loss_rupees):
            self._log_signal_evaluation(
                "vertical_risk_disallowed",
                spread_type=spread_type,
                max_loss_rupees=max_loss_rupees,
                per_trade_cap=self.risk_per_trade_pct * self.account_equity,
                total_cap=self.max_total_risk_pct * self.account_equity,
                open_spreads=len(self.open_spreads),
                max_open_spreads=self.max_open_spreads,
            )
            return
        if self._enter_spread(
            spread_type, short_label, long_label, credit, width_pts, expiry, extra=None
        ):
            # Both legs of the vertical landed -- emit the unambiguous
            # full-spread-opened signal. (Codex P2 round-3 #1.)
            self._log_signal_evaluation(
                "spread_opened",
                spread_type=spread_type,
                credit=credit,
                width_pts=width_pts,
                short_label=short_label,
                long_label=long_label,
            )

    # Enter a spread and record it in state.
    def _enter_spread(
        self,
        spread_type: str,
        short_label: str,
        long_label: str,
        credit: float,
        width_pts: float,
        expiry: date,
        extra: Optional[List[SpreadLeg]] = None,
    ) -> bool:
        qty = self._qty_for_label(short_label)
        short_price = self._latest_price(short_label)
        long_price = self._latest_price(long_label)
        if short_price is None or long_price is None:
            # Codex round-3 #4: a fresh-LTP gap between gate evaluation and
            # order placement leaves no diagnostic without this.
            self._log_signal_evaluation(
                "entry_leg_price_unavailable",
                spread_type=spread_type,
                short_label=short_label,
                long_label=long_label,
                short_price_set=short_price is not None,
                long_price_set=long_price is not None,
            )
            return False
        entry_time = datetime.now(tz=IST)
        spread_id = f"{spread_type}_{short_label}_{long_label}_{int(entry_time.timestamp())}"
        short_symbol, short_exchange, short_token = self._resolve_order_identity(short_label)
        long_symbol, long_exchange, long_token = self._resolve_order_identity(long_label)
        max_loss_rupees = (width_pts - credit) * qty
        open_spread = OpenSpread(
            spread_id=spread_id,
            spread_type=spread_type,
            short_leg=SpreadLeg(
                short_label,
                "SELL",
                self._strike_from_label(short_label) or 0.0,
                qty,
                entry_price=short_price,
                broker_symbol=short_symbol,
                exchange=short_exchange,
                symbol_token=short_token,
            ),
            long_leg=SpreadLeg(
                long_label,
                "BUY",
                self._strike_from_label(long_label) or 0.0,
                qty,
                entry_price=long_price,
                broker_symbol=long_symbol,
                exchange=long_exchange,
                symbol_token=long_token,
            ),
            entry_credit=credit,
            width_pts=width_pts,
            max_loss_rupees=max_loss_rupees,
            entry_time=entry_time,
            expiry=expiry,
            extra_legs=extra or [],
        )
        # place short then long; if long fails, unwind short
        resp_short = self._place_order(
            label=short_label,
            side="SELL",
            qty=qty,
            price=short_price,
            tag=f"{spread_type}_SHORT",
            purpose=OrderPurpose.ENTRY,
            strategy_context=self._spread_leg_strategy_context(
                open_spread,
                leg_role="short",
            ),
        )
        if str(getattr(resp_short, "status", "")).upper() not in {"ACCEPTED", "FILLED", "COMPLETE"}:
            # Codex round-3 #4: surface the broker-side rejection so ops
            # can correlate it with the bar that triggered it.
            self._log_signal_evaluation(
                "entry_short_leg_order_rejected",
                spread_type=spread_type,
                short_label=short_label,
                response_status=str(getattr(resp_short, "status", "") or ""),
                response_message=str(getattr(resp_short, "message", "") or "")[:120],
            )
            return False
        self._remember_order_identity(short_label)
        resp_long = self._place_order(
            label=long_label,
            side="BUY",
            qty=qty,
            price=long_price,
            tag=f"{spread_type}_LONG",
            purpose=OrderPurpose.ENTRY,
            strategy_context=self._spread_leg_strategy_context(
                open_spread,
                leg_role="long",
            ),
        )
        if str(getattr(resp_long, "status", "")).upper() not in {"ACCEPTED", "FILLED", "COMPLETE"}:
            # unwind short
            self._force_exit_leg(short_label, "BUY", qty)
            # Codex round-3 #4: surface the long-leg rejection + indicate
            # that the short-leg unwind has fired.
            self._log_signal_evaluation(
                "entry_long_leg_order_rejected_unwound_short",
                spread_type=spread_type,
                long_label=long_label,
                short_label_unwound=short_label,
                response_status=str(getattr(resp_long, "status", "") or ""),
                response_message=str(getattr(resp_long, "message", "") or "")[:120],
            )
            return False
        self._remember_order_identity(long_label)
        self.open_spreads[spread_id] = open_spread
        self._reset_exit_retry_state(spread_id)
        self._sync_spread_state_to_risk_manager(open_spread)
        logger.info(
            "[NIFTY_SPREAD] ENTER %s id=%s credit=%.2f width=%.1f qty=%d",
            spread_type,
            spread_id,
            credit,
            width_pts,
            qty,
        )
        # NB: the `signal_evaluated_with_reason=spread_opened` success log is
        # NOT emitted here. _enter_spread is called twice for an iron condor
        # (PUT side, then CALL side); emitting per-call would produce a
        # false-positive "opened" log for a condor whose CALL side then
        # fails and rolls back the PUT side. The success log is fired by
        # the spread builder (_open_vertical / _try_iron_condor) AFTER
        # the full spread is in place. (Codex P2 round-3 #1, PR #208.)
        return True

    # Force exit a single leg at market.
    def _force_exit_leg(self, label: str, side: str, qty: int) -> None:
        price = self._latest_price(label) or 0.0
        self._place_order(
            label=label,
            side=side,
            qty=qty,
            price=price,
            tag="FORCE_EXIT",
            purpose=OrderPurpose.EXIT,
        )

    # Compute the current spread mark-to-market.
    def _current_spread_ltp(self, spread: OpenSpread) -> Optional[float]:
        sp = self._latest_price(spread.short_leg.label)
        lp = self._latest_price(spread.long_leg.label)
        if sp is None or lp is None:
            return None
        return sp - lp

    # Manage exits based on profit/stop targets and time.
    def _maybe_manage_exits(self, now: datetime, underlying_close: float) -> None:
        to_close: List[str] = []
        for sid, spread in self.open_spreads.items():
            if spread.short_exit_done or spread.long_exit_done:
                to_close.append(sid)
                continue
            # expiry day EOD flatten
            if self.force_flatten_on_expiry and now.date() == spread.expiry and now.time() >= self.exit_time_eod:
                to_close.append(sid)
                continue
            ltp = self._current_spread_ltp(spread)
            if ltp is None:
                continue
            target_ltp = (1 - self.take_profit_pct_of_credit) * spread.entry_credit
            stop_ltp = self.stop_loss_mult_credit * spread.entry_credit
            if ltp <= target_ltp:
                to_close.append(sid)
            elif ltp >= stop_ltp:
                to_close.append(sid)
        for sid in to_close:
            self._exit_spread(sid)

    # Exit a spread by closing both legs.
    def _exit_spread(self, spread_id: str) -> None:
        spread = self.open_spreads.get(spread_id)
        if not spread:
            return
        if spread_id in self._exit_in_flight_spreads:
            return
        now_mono = monotonic()
        circuit_until = self._exit_circuit_open_until_mono_by_spread.get(spread_id, 0.0)
        if now_mono < circuit_until:
            last_alert = self._exit_last_alert_mono_by_spread.get(spread_id, 0.0)
            if now_mono - last_alert >= self._exit_circuit_alert_interval_seconds:
                self._exit_last_alert_mono_by_spread[spread_id] = now_mono
                remaining = max(0.0, circuit_until - now_mono)
                logger.error(
                    "[NIFTY_SPREAD][ALERT] Exit circuit active id=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    spread_id,
                    self._exit_failure_count_by_spread.get(spread_id, 0),
                    self._exit_max_retries,
                    remaining,
                )
            return
        next_retry = self._next_exit_retry_mono_by_spread.get(spread_id, 0.0)
        if now_mono < next_retry:
            return
        self._exit_in_flight_spreads.add(spread_id)
        status_short = "SKIPPED"
        status_long = "SKIPPED"
        state_changed = False
        try:
            if not spread.short_exit_done:
                sp_price = self._latest_price(spread.short_leg.label) or 0.0
                resp_short = self._place_order(
                    label=spread.short_leg.label,
                    side="BUY",
                    qty=max(1, int(spread.short_leg.qty)),
                    price=sp_price,
                    tag="EXIT",
                    purpose=OrderPurpose.EXIT,
                    broker_symbol=spread.short_leg.broker_symbol,
                    exchange=spread.short_leg.exchange,
                    symbol_token=spread.short_leg.symbol_token,
                )
                status_short = str(getattr(resp_short, "status", "")).upper()
                if status_short not in {"REJECTED", "FAILED"}:
                    spread.short_exit_done = True
                    state_changed = True
            if not spread.long_exit_done:
                lp_price = self._latest_price(spread.long_leg.label) or 0.0
                resp_long = self._place_order(
                    label=spread.long_leg.label,
                    side="SELL",
                    qty=max(1, int(spread.long_leg.qty)),
                    price=lp_price,
                    tag="EXIT",
                    purpose=OrderPurpose.EXIT,
                    broker_symbol=spread.long_leg.broker_symbol,
                    exchange=spread.long_leg.exchange,
                    symbol_token=spread.long_leg.symbol_token,
                )
                status_long = str(getattr(resp_long, "status", "")).upper()
                if status_long not in {"REJECTED", "FAILED"}:
                    spread.long_exit_done = True
                    state_changed = True
        except Exception:
            if state_changed:
                self._sync_spread_state_to_risk_manager(spread)
            attempt, wait_s, opened = self._register_exit_failure(
                spread_id=spread_id,
                now_mono=monotonic(),
            )
            if opened:
                logger.exception(
                    "[NIFTY_SPREAD][ALERT] Exit circuit opened after exception id=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    spread_id,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[NIFTY_SPREAD] Exit exception id=%s attempt=%d/%d retry_in=%.2fs",
                    spread_id,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        finally:
            self._exit_in_flight_spreads.discard(spread_id)
        if not spread.short_exit_done or not spread.long_exit_done:
            if state_changed:
                self._sync_spread_state_to_risk_manager(spread)
            attempt, wait_s, opened = self._register_exit_failure(
                spread_id=spread_id,
                now_mono=monotonic(),
            )
            if opened:
                logger.error(
                    "[NIFTY_SPREAD][ALERT] Exit circuit opened after broker rejection id=%s short=%s long=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    spread_id,
                    status_short,
                    status_long,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.warning(
                    "[NIFTY_SPREAD] Exit rejected id=%s short=%s long=%s attempt=%d/%d retry_in=%.2fs",
                    spread_id,
                    status_short,
                    status_long,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return
        self._reset_exit_retry_state(spread_id)
        logger.info("[NIFTY_SPREAD] EXIT %s id=%s", spread.spread_type, spread.spread_id)
        self.open_spreads.pop(spread_id, None)

    # Force exit all open spreads.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.open_spreads:
            return
        if not submit_orders:
            for spread_id in list(self.open_spreads.keys()):
                self._reset_exit_retry_state(spread_id)
            self.open_spreads.clear()
            return
        for spread_id in list(self.open_spreads.keys()):
            self._exit_spread(spread_id)


__all__ = ["NiftyWeeklyCreditSpreadStrategy"]
