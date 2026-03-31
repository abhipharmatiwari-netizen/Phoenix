"""Strategy layer hooks (§5 Order Routing/Strategies) currently wired for single-tenant env configs.

Tenancy note:
- Strategies pass tenant_id and broker_account_id resolved from Settings into
  strategy_bridge so hub routing always has explicit IDs.
- Adding more tenants does not require more env vars; new tenants live in Firestore.
"""

from __future__ import annotations

# option_strategy.py
# Generic options strategy driven by an underlying's indicators (ATR/RSI/MACD)
# plus a flexible "spread template" framework. Instantiated per underlying with
# env prefixes like NG_, NIFTY_, or BANKNIFTY_.

import os
import logging
from time import monotonic
from dataclasses import dataclass
from typing import Dict, Any, Optional, List
from collections import Counter, deque
from datetime import datetime, timezone

from app.core.order_client import AngelOrderClient
from app.core.risk_manager import RiskManager
from app.strategies.base import BaseStrategy
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.identifiers import (
    OPTION_STRATEGY_ID,
)
from app.config.settings import get_settings
from app.config.boot_config import StrategyValueResolver
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


# ---------- Data structures for the "options framework" ----------


# Template definition for a single options leg.
@dataclass
class LegTemplate:
    """Abstract leg definition inside a spread template."""

    role: str  # e.g. "CE", "PE", "CE_LONG", "CE_SHORT", "STRANGLE_CE"
    side: str  # "BUY" or "SELL"
    qty_mult: float = 1.0


# Template definition for a multi-leg options spread.
@dataclass
class SpreadTemplate:
    """Definition of a multi-leg options structure (spread)."""

    name: str
    legs: List[LegTemplate]


# Runtime state for an open leg position.
@dataclass
class OpenLegPosition:
    label: str  # instrument label, e.g. "NG_ATM_CE_415.00"
    role: str  # logical role, e.g. "CE"
    side: str  # "BUY" or "SELL"
    qty: int
    entry_price: float
    sl_price: float
    tp_price: float
    trail_buffer_pct: float
    best_price: float  # for short: lowest price seen (favourable move)
    template_name: Optional[str] = None
    underlying: Optional[str] = None
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# Generic options strategy with configurable spreads and signals.
class OptionStrategy(BaseStrategy):
    """
    Generic options strategy with:
      - Spread templates (Short CE/PE, Short Straddle, basic verticals, Short Strangle)
      - Per-leg SL/TP/trailing for short options
      - ATR/RSI/MACD-based entry signals
      - Volatility regime filter based on ATR/Close

    It is parameterised by:
      - env_prefix:        e.g. "NG_" or "NIFTY_"
      - underlying_label:  e.g. "NG_FUT" or "NIFTY_IDX"
      - ce_label_prefix:   prefix for ATM CE labels (e.g. "NG_ATM_CE", "NIFTY_ATM_CE")
      - pe_label_prefix:   prefix for ATM PE labels (e.g. "NG_ATM_PE", "NIFTY_ATM_PE")
    """

    # Initialize strategy configuration, templates, and state.
    def __init__(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        order_client: Optional[AngelOrderClient] = None,
        *,
        env_prefix: str = "NG_",
        underlying_label: str = "NG_FUT",
        ce_label_prefix: str = "NG_ATM_CE",
        pe_label_prefix: str = "NG_ATM_PE",
        risk_manager: Optional[RiskManager] = None,
        vol_thresholds: Optional[Dict[str, Dict[str, Any]]] = None,
        model_store: Optional[Any] = None,
        feature_assembler: Optional[Any] = None,
        ml_env: Optional[Dict[str, Any]] = None,
        recent_close_cache: int = 300,
        config_resolver: Optional[StrategyValueResolver] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.order_client = order_client
        self.risk_manager = risk_manager

        # config namespace prefix (NG_ / NIFTY_ / etc.)
        self.env_prefix = env_prefix
        self.underlying_label = underlying_label
        self.ce_label_prefix = ce_label_prefix
        self.pe_label_prefix = pe_label_prefix
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params={},
            env=dict(os.environ),
        )
        settings = get_settings()
        self._strategy_id = OPTION_STRATEGY_ID
        if self.env_prefix == "NG_":
            self._use_hub_router_flag = settings.use_hub_router_for_ng_options
        elif self.env_prefix == "NIFTY_":
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

        # ------- Config from env -------
        self.trade_mode = self._cfg.get_str("TRADE_MODE", "PAPER").upper()
        # TODO: legacy single-tenant path; route to Settings/Control Plane when hub routing is universal.
        self.no_trade_counts: Counter[str] = Counter()

        # helper to read env with prefix
        # Read an environment variable with the strategy prefix.
        def _env(key: str, default: str) -> str:
            return self._cfg.get_prefixed_str(key, default)

        self._env = _env  # store helper for later use

        # Signal frame: default 5m bars
        self.signal_timeframe = int(self._env("SIGNAL_TIMEFRAME", "300"))  # seconds
        # Backward-compatible constructor args retained; ML gating is intentionally disabled.
        self._ml_env = ml_env or {}
        self.model_store = model_store
        self.feature_assembler = feature_assembler
        self._recent_closes: deque[float] = deque(
            maxlen=max(50, int(recent_close_cache or 0))
        )

        # ATR & RSI thresholds (per underlying)
        self.min_atr = float(self._env("MIN_ATR", "1.0"))
        self.rsi_upper = float(self._env("RSI_UPPER", "60"))  # for short CE
        self.rsi_lower = float(self._env("RSI_LOWER", "40"))  # for short PE
        self.use_rsi_filter = self._env("USE_RSI_FILTER", "1").lower() not in {
            "0",
            "false",
            "no",
        }
        self.ema_period = int(self._env("EMA_PERIOD", "20"))

        # Per-underlying product type
        self.product_type = self._env("PRODUCT_TYPE", "INTRADAY")

        # Risk parameters for short options
        self.sl_pct = float(self._env("SL_PCT", "0.30"))  # +30% from entry
        self.tp_pct = float(self._env("TP_PCT", "0.30"))  # -30% from entry
        self.trail_buffer_pct = float(self._env("TRAIL_BUFFER_PCT", "0.05"))  # 5%
        self.qty_per_trade = int(self._env("QTY_PER_TRADE", "1"))

        # Position constraints
        self.max_open_spreads = int(self._env("MAX_OPEN_SPREADS", "1"))

        # Volatility regime thresholds (ATR normalized by close)
        # atr_norm = ATR / close
        self.vol_low_atr_norm = float(self._env("VOL_LOW_ATR_NORM", "0.004"))  # 0.4%
        self.vol_high_atr_norm = float(self._env("VOL_HIGH_ATR_NORM", "0.010"))  # 1.0%
        self.vol_thresholds: Dict[str, Dict[str, float]] = {}
        for key, val in (vol_thresholds or {}).items():
            if not isinstance(val, dict):
                continue
            try:
                low = float(val.get("low_max"))
                med = float(val.get("med_max"))
            except (TypeError, ValueError):
                continue
            self.vol_thresholds[str(key).upper()] = {"low_max": low, "med_max": med}

        # Which templates to use for each regime
        self.neutral_template_name = self._env("NEUTRAL_TEMPLATE", "SHORT_STRADDLE")
        self.directional_template_ce = self._env("DIRECTIONAL_TEMPLATE_CE", "SHORT_CE")
        self.directional_template_pe = self._env("DIRECTIONAL_TEMPLATE_PE", "SHORT_PE")

        # Identify ATM CE/PE labels from instrument_meta
        self.fut_label = self.underlying_label
        self.ce_label = self._find_label_prefix(self.ce_label_prefix)
        self.pe_label = self._find_label_prefix(self.pe_label_prefix)
        self.strangle_ce_label = (
            self._find_label_prefix(self.ce_label_prefix.replace("ATM", "OTM"))
            or self._find_label_prefix(f"{self.env_prefix}OTM_CE")
            or self._find_label_prefix("OTM_CE")
        )
        self.strangle_pe_label = (
            self._find_label_prefix(self.pe_label_prefix.replace("ATM", "OTM"))
            or self._find_label_prefix(f"{self.env_prefix}OTM_PE")
            or self._find_label_prefix("OTM_PE")
        )

        # Optional role -> label overrides (for OTM/strangle/spread legs)
        # Try PREFIXED env first (e.g. NG_ROLE_CE_LABEL), then global ROLE_CE_LABEL.
        self.role_label_overrides: Dict[str, str] = {}
        for role in [
            "CE",
            "PE",
            "CE_LONG",
            "CE_SHORT",
            "PE_LONG",
            "PE_SHORT",
            "STRANGLE_CE",
            "STRANGLE_PE",
        ]:
            role_upper = role.upper()
            val = self._cfg.get_str(f"ROLE_{role_upper}_LABEL", "").strip()
            if val:
                self.role_label_overrides[role_upper] = val
                logger.info(
                    "[%s] Role override: %s -> %s", self.env_prefix, role_upper, val
                )

        logger.info(
            "OptionStrategy init | env_prefix=%s mode=%s tf=%ss underlying=%s ce_prefix=%s pe_prefix=%s",
            self.env_prefix,
            self.trade_mode,
            self.signal_timeframe,
            self.fut_label,
            self.ce_label_prefix,
            self.pe_label_prefix,
        )

        # ----- Spread templates (framework) -----
        # NOTE: roles are logical names; mapping to real labels is done in _resolve_role_to_label().
        self.templates: Dict[str, SpreadTemplate] = {
            # 1) Directional basic: Short ATM CE / Short ATM PE
            "SHORT_CE": SpreadTemplate(
                name="SHORT_CE",
                legs=[LegTemplate(role="CE", side="SELL", qty_mult=1.0)],
            ),
            "SHORT_PE": SpreadTemplate(
                name="SHORT_PE",
                legs=[LegTemplate(role="PE", side="SELL", qty_mult=1.0)],
            ),
            # 2) Neutral: ATM Straddle (short CE + short PE at same strike)
            "SHORT_STRADDLE": SpreadTemplate(
                name="SHORT_STRADDLE",
                legs=[
                    LegTemplate(role="CE", side="SELL", qty_mult=1.0),
                    LegTemplate(role="PE", side="SELL", qty_mult=1.0),
                ],
            ),
            # 3) Vertical spreads (Bull/Bear Call/Put)
            "BULL_CALL_SPREAD": SpreadTemplate(
                name="BULL_CALL_SPREAD",
                legs=[
                    LegTemplate(role="CE_LONG", side="BUY", qty_mult=1.0),
                    LegTemplate(role="CE_SHORT", side="SELL", qty_mult=1.0),
                ],
            ),
            "BEAR_CALL_SPREAD": SpreadTemplate(
                name="BEAR_CALL_SPREAD",
                legs=[
                    LegTemplate(role="CE_SHORT", side="SELL", qty_mult=1.0),
                    LegTemplate(role="CE_LONG", side="BUY", qty_mult=1.0),
                ],
            ),
            "BULL_PUT_SPREAD": SpreadTemplate(
                name="BULL_PUT_SPREAD",
                legs=[
                    LegTemplate(role="PE_LONG", side="BUY", qty_mult=1.0),
                    LegTemplate(role="PE_SHORT", side="SELL", qty_mult=1.0),
                ],
            ),
            "BEAR_PUT_SPREAD": SpreadTemplate(
                name="BEAR_PUT_SPREAD",
                legs=[
                    LegTemplate(role="PE_SHORT", side="SELL", qty_mult=1.0),
                    LegTemplate(role="PE_LONG", side="BUY", qty_mult=1.0),
                ],
            ),
            # 4) Short Strangle: short OTM CE + short OTM PE
            "SHORT_STRANGLE": SpreadTemplate(
                name="SHORT_STRANGLE",
                legs=[
                    LegTemplate(role="STRANGLE_CE", side="SELL", qty_mult=1.0),
                    LegTemplate(role="STRANGLE_PE", side="SELL", qty_mult=1.0),
                ],
            ),
        }

        # ----- State -----
        self.last_price: Dict[str, float] = {}  # latest LTP per label
        self.open_legs: Dict[str, OpenLegPosition] = {}  # label -> OpenLegPosition
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._exit_in_flight_labels: set[str] = set()
        self._next_exit_retry_mono_by_label: Dict[str, float] = {}
        retry_cfg = self._cfg.get_prefixed_float(
            "EXIT_RETRY_COOLDOWN_SECONDS",
            5.0,
        )
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(1, self._cfg.get_prefixed_int("EXIT_MAX_RETRIES", 12))
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            self._cfg.get_prefixed_float("EXIT_CIRCUIT_OPEN_SECONDS", 300.0),
        )
        self._exit_circuit_alert_interval_seconds = max(
            15.0,
            self._cfg.get_prefixed_float("EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", 60.0),
        )
        self._exit_failure_count_by_label: Dict[str, int] = {}
        self._exit_circuit_open_until_mono_by_label: Dict[str, float] = {}
        self._exit_last_alert_mono_by_label: Dict[str, float] = {}
        self.prev_rsi_prev: Optional[float] = None
        self.prev_rsi: Optional[float] = None
        self.prev_macd: Optional[float] = None
        self.prev_ema: Optional[float] = None  # EMA for crossover guard
        self.prev_close: Optional[float] = None

        # Restore open legs from risk manager state if present
        if self.risk_manager and hasattr(self.risk_manager, "restored_positions"):
            for label, pos in getattr(
                self.risk_manager, "restored_positions", {}
            ).items():
                if (
                    pos.get("underlying")
                    and pos.get("underlying") != self.underlying_label
                ):
                    continue
                self.open_legs[label] = OpenLegPosition(
                    label=label,
                    role=str(pos.get("role", "")),
                    side=str(pos.get("side", "SELL")),
                    qty=int(pos.get("qty", 0)),
                    entry_price=float(pos.get("entry_price", 0.0)),
                    sl_price=float(pos.get("entry_price", 0.0) * (1.0 + self.sl_pct)),
                    tp_price=float(pos.get("entry_price", 0.0) * (1.0 - self.tp_pct)),
                    trail_buffer_pct=self.trail_buffer_pct,
                    best_price=float(pos.get("entry_price", 0.0)),
                    template_name=str(pos.get("template_name", "")),
                    underlying=pos.get("underlying"),
                    broker_symbol=str(pos.get("broker_symbol", "")) or None,
                    exchange=str(pos.get("exchange", "")) or None,
                    symbol_token=str(pos.get("symbol_token", "")) or None,
                )
                self._remember_order_identity(
                    label,
                    broker_symbol=self.open_legs[label].broker_symbol,
                    exchange=self.open_legs[label].exchange,
                    symbol_token=self.open_legs[label].symbol_token,
                )

        logger.info(
            "[%s] OptionStrategy exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )

    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    # Refresh ATM/OTM labels after universe updates.
    def refresh_atm_labels(self, instrument_meta: Dict[str, Dict[str, Any]]) -> None:
        """
        Recompute ATM CE/PE labels when universe is refreshed.
        """
        self.instrument_meta = instrument_meta
        self.ce_label = self._find_label_prefix(self.ce_label_prefix)
        self.pe_label = self._find_label_prefix(self.pe_label_prefix)
        self.strangle_ce_label = (
            self._find_label_prefix(self.ce_label_prefix.replace("ATM", "OTM"))
            or self._find_label_prefix(f"{self.env_prefix}OTM_CE")
            or self._find_label_prefix("OTM_CE")
        )
        self.strangle_pe_label = (
            self._find_label_prefix(self.pe_label_prefix.replace("ATM", "OTM"))
            or self._find_label_prefix(f"{self.env_prefix}OTM_PE")
            or self._find_label_prefix("OTM_PE")
        )

    @staticmethod
    def _normalize_leg_identity_value(value: object) -> Optional[str]:
        if value in (None, ""):
            return None
        text = str(value).strip().upper()
        return text or None

    def _leg_identity_keys(
        self,
        label: str,
        *,
        pos: Optional[OpenLegPosition] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        source_meta = meta if isinstance(meta, dict) else {}
        keys: List[str] = []
        token = (
            (str(pos.symbol_token).strip() if pos and pos.symbol_token else None)
            or self._resolve_symbol_token(source_meta)
        )
        if token:
            keys.append(f"token:{token}")
        broker_symbol = (
            (str(pos.broker_symbol).strip() if pos and pos.broker_symbol else None)
            or self._resolve_broker_symbol(source_meta, label)
        )
        if broker_symbol:
            keys.append(f"symbol:{broker_symbol.upper()}")
        underlying = self._normalize_leg_identity_value(
            source_meta.get("underlying") or source_meta.get("underlying_name")
        )
        expiry = self._normalize_leg_identity_value(
            source_meta.get("expiry")
            or source_meta.get("expiry_dt")
            or source_meta.get("expiryDate")
        )
        strike = self._normalize_leg_identity_value(source_meta.get("strike"))
        option_kind = self._normalize_leg_identity_value(
            source_meta.get("kind")
            or source_meta.get("option_type")
            or source_meta.get("option_right")
        )
        product_type = self._normalize_leg_identity_value(self.product_type)
        if (
            underlying
            and expiry
            and strike
            and option_kind in {"CE", "PE"}
            and product_type
        ):
            keys.append(
                ":".join(
                    (
                        "contract",
                        underlying,
                        expiry,
                        strike,
                        option_kind,
                        product_type,
                    )
                )
            )
        deduped: List[str] = []
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped

    def reconcile_open_legs(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        *,
        previous_instrument_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, int]:
        if not hasattr(self, "open_legs"):
            self.open_legs = {}
        previous_meta = previous_instrument_meta or self.instrument_meta or {}
        open_legs_before = len(self.open_legs)
        if open_legs_before == 0:
            self.instrument_meta = instrument_meta
            return {
                "open_legs_before": 0,
                "open_legs_after": 0,
                "rebound": 0,
                "preserved": 0,
            }

        label_by_identity: Dict[str, str] = {}
        for new_label, new_meta in instrument_meta.items():
            for identity_key in self._leg_identity_keys(new_label, meta=new_meta):
                label_by_identity.setdefault(identity_key, new_label)

        reconciled_legs: Dict[str, OpenLegPosition] = {}
        updated_order_identity: Dict[str, Dict[str, Optional[str]]] = {}
        rebound = 0
        preserved = 0
        for old_label, pos in list(self.open_legs.items()):
            previous_meta_for_label = previous_meta.get(old_label, {})
            target_label: Optional[str] = None
            for identity_key in self._leg_identity_keys(
                old_label,
                pos=pos,
                meta=previous_meta_for_label,
            ):
                candidate = label_by_identity.get(identity_key)
                if candidate:
                    target_label = candidate
                    break
            if target_label is None:
                target_label = old_label
            target_meta = instrument_meta.get(target_label, {})
            rebound += int(target_label != old_label)
            preserved += int(target_label == old_label)
            if target_label in reconciled_legs:
                logger.warning(
                    "[%s] ATM refresh leg reconcile collision old_label=%s target_label=%s; keeping first leg binding",
                    self.env_prefix,
                    old_label,
                    target_label,
                )
                continue
            reconciled_legs[target_label] = OpenLegPosition(
                label=target_label,
                role=pos.role,
                side=pos.side,
                qty=pos.qty,
                entry_price=pos.entry_price,
                sl_price=pos.sl_price,
                tp_price=pos.tp_price,
                trail_buffer_pct=pos.trail_buffer_pct,
                best_price=pos.best_price,
                template_name=pos.template_name,
                underlying=pos.underlying,
                broker_symbol=pos.broker_symbol
                or self._resolve_broker_symbol(target_meta, target_label),
                exchange=pos.exchange
                or (
                    str(target_meta.get("exchange") or target_meta.get("exch_seg"))
                    if target_meta.get("exchange") or target_meta.get("exch_seg")
                    else None
                ),
                symbol_token=pos.symbol_token or self._resolve_symbol_token(target_meta),
            )
            updated_order_identity[target_label] = {
                "broker_symbol": reconciled_legs[target_label].broker_symbol,
                "exchange": reconciled_legs[target_label].exchange,
                "symbol_token": reconciled_legs[target_label].symbol_token,
            }

        self.instrument_meta = instrument_meta
        self.open_legs = reconciled_legs
        self._order_identity_by_label = updated_order_identity
        return {
            "open_legs_before": open_legs_before,
            "open_legs_after": len(self.open_legs),
            "rebound": rebound,
            "preserved": preserved,
        }

    # ========== Public hooks ==========

    # Handle per-tick updates and manage open leg risk.
    def on_tick(self, label: str, ltp: float) -> None:
        """
        Called on every tick from multi_instrument_stream.on_data().
        Used for:
          - recording latest LTP for entry
          - updating SL/TP/trailing for open short legs
        """
        # Defensive: protect against partially restored instances missing runtime state.
        if not hasattr(self, "last_price"):
            self.last_price = {}
        if not hasattr(self, "open_legs"):
            self.open_legs = {}

        self.last_price[label] = ltp
        # Opportunistically resolve missing ATM labels once we have a live underlying price
        if label == self.fut_label:
            if self.ce_label is None:
                self.ce_label = self._select_nearest_option(side="CE", target_price=ltp)
            if self.pe_label is None:
                self.pe_label = self._select_nearest_option(side="PE", target_price=ltp)
        self._update_open_leg_risk(label, ltp)

    # Handle bar-close signals for the strategy timeframe.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle, indicators: Dict[str, Any]
    ) -> None:
        """
        Called on every bar (all labels/timeframes) from the indicator engine.
        Strategy only reacts to *its own* underlying_label at its own timeframe.
        """
        if label != self.fut_label or timeframe_seconds != self.signal_timeframe:
            return

        close_val = getattr(candle, "c", None) or getattr(candle, "close", None)
        if close_val is not None:
            try:
                self._recent_closes.append(float(close_val))
            except Exception:
                pass

        atr = indicators.get("atr")
        rsi = indicators.get("rsi")
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")
        ema_key = f"ema_{self.ema_period}"
        ema_val = indicators.get(ema_key)

        logger.info(
            "[%s] signal bar | label=%s tf=%ss close=%.3f ATR=%s RSI=%s MACD=%s SIG=%s EMA=%s",
            self.env_prefix,
            label,
            timeframe_seconds,
            candle.c,
            atr,
            rsi,
            macd,
            macd_signal,
            ema_val,
        )

        # ----- Basic guards & warm-up state updates -----
        if atr is None or rsi is None or macd is None or macd_signal is None:
            self._record_no_trade(
                "NO_TRADE_INDICATOR_INCOMPLETE",
                "Missing indicator(s) atr=%s rsi=%s macd=%s macd_sig=%s",
                atr,
                rsi,
                macd,
                macd_signal,
            )
            self.prev_rsi_prev = self.prev_rsi
            self.prev_rsi = rsi
            self.prev_macd = macd
            self.prev_ema = ema_val
            self.prev_close = candle.c
            return

        if atr < self.min_atr:
            self._record_no_trade(
                "NO_TRADE_MIN_ATR",
                "ATR too low (%.3f < min %.3f)",
                atr,
                self.min_atr,
            )
            self.prev_rsi_prev = self.prev_rsi
            self.prev_rsi = rsi
            self.prev_macd = macd
            self.prev_ema = ema_val
            self.prev_close = candle.c
            return

        # ----- Roll previous values forward -----
        prev_rsi_prev = self.prev_rsi_prev
        prev_rsi = self.prev_rsi
        prev_macd = self.prev_macd
        self.prev_rsi_prev = prev_rsi
        self.prev_rsi = rsi
        self.prev_macd = macd
        prev_ema = self.prev_ema
        prev_close = self.prev_close
        self.prev_ema = ema_val
        self.prev_close = candle.c

        if prev_rsi is None or prev_rsi_prev is None or prev_macd is None:
            self._record_no_trade(
                "NO_TRADE_NEED_PRIOR_INDICATORS",
                "Waiting for previous RSI/MACD values (need 2 prior RSI points)",
            )
            return

        # ===== Volatility regime (using ATR / Close) =====
        atr_norm = (atr / candle.c) if (candle.c and candle.c > 0) else 0.0
        thresholds = self._resolve_vol_thresholds()
        low_thr = thresholds.get("low_max", self.vol_low_atr_norm)
        med_thr = thresholds.get("med_max", self.vol_high_atr_norm)
        if atr_norm <= low_thr:
            regime = "LOW"
        elif atr_norm <= med_thr:
            regime = "MEDIUM"
        else:
            regime = "HIGH"

        logger.info(
            "[%s] Vol regime: %s (atr_norm=%.5f low=%.4f med=%.4f)",
            self.env_prefix,
            regime,
            atr_norm,
            low_thr,
            med_thr,
        )

        # ===== EMA-based directional triggers =====
        ema_ready = ema_val is not None
        below_ema = bool(ema_ready and ema_val is not None and candle.c < ema_val)
        crossed_below_ema = False
        crossed_above_ema = False
        if ema_ready and prev_ema is not None and prev_close is not None:
            crossed_below_ema = (prev_close >= prev_ema) and (candle.c < ema_val)
            crossed_above_ema = (prev_close <= prev_ema) and (candle.c > ema_val)

        # ===== RSI / MACD derived features =====
        # Decreasing RSI trend: RSI(n-2) > RSI(n-1) > RSI(n)
        rsi_down_trend = (prev_rsi_prev > prev_rsi) and (prev_rsi > rsi)
        macd_bear = macd < macd_signal
        macd_bull = macd > macd_signal

        # Old composite signals kept for diagnostics
        bearish_call_signal = rsi_down_trend and macd_bear
        bullish_put_signal = rsi > prev_rsi and rsi < self.rsi_lower and macd_bull

        logger.info(
            "[%s] Signals | ema_cross_down=%s ema_cross_up=%s "
            "bearish_call_signal=%s bullish_put_signal=%s | "
            "rsi_trend=(%.2f,%.2f,%.2f) macd_bear=%s macd_bull=%s | open_legs=%s | below_ema=%s",
            self.env_prefix,
            crossed_below_ema,
            crossed_above_ema,
            bearish_call_signal,
            bullish_put_signal,
            prev_rsi_prev,
            prev_rsi,
            rsi,
            macd_bear,
            macd_bull,
            list(self.open_legs.keys()),
            below_ema,
        )

        # ----- Position cap guard -----
        open_spreads_estimate = len(
            {pos.role for pos in self.open_legs.values() if pos.side == "SELL"}
        )
        if open_spreads_estimate >= self.max_open_spreads:
            self._record_no_trade(
                "NO_TRADE_RISK_MAX_SPREADS",
                "Max open spreads reached (%d), not opening new ones",
                self.max_open_spreads,
            )
            return

        # ===== Regime + strategy selection =====
        if regime == "LOW":
            # Rangebound preference: prefer neutral when RSI is inside band,
            # but do not hard-block when outside. If filter disabled, always fire.
            is_range = self.rsi_lower <= rsi <= self.rsi_upper
            if self.use_rsi_filter and not is_range:
                logger.info(
                    "[%s][SOFT_FILTER] LOW vol, RSI out of neutral range "
                    "(rsi=%.2f range=%.2f-%.2f) -> still firing neutral",
                    self.env_prefix,
                    rsi,
                    self.rsi_lower,
                    self.rsi_upper,
                )
            else:
                logger.info(
                    "[%s] LOW vol & rangebound RSI -> firing neutral template %s",
                    self.env_prefix,
                    self.neutral_template_name,
                )
            self._fire_template_with_ml(
                self.neutral_template_name,
                reason=f"regime={regime},signal=neutral",
                candle=candle,
                indicators=indicators,
            )
            return

        # MEDIUM/HIGH vol: rely on RSI/MACD directional signals
        fired = False

        # 1) CALL shorts: use RSI down trend + MACD bear
        if bearish_call_signal:
            self._fire_template_with_ml(
                self.directional_template_ce,
                reason=(
                    f"regime={regime},signal=bearish_call,"
                    f"rsi={rsi:.2f},macd={macd:.4f},macd_sig={macd_signal:.4f}"
                ),
                candle=candle,
                indicators=indicators,
            )
            fired = True

        # 2) If no CALL trade, still allow original bullish PUT shorts
        if (not fired) and bullish_put_signal:
            logger.info(
                "[%s] Directional bullish (PUT short) -> template %s",
                self.env_prefix,
                self.directional_template_pe,
            )
            self._fire_template_with_ml(
                self.directional_template_pe,
                reason=f"regime={regime},signal=bullish_put",
                candle=candle,
                indicators=indicators,
            )
            fired = True

        if not fired:
            self._record_no_trade(
                "NO_TRADE_NO_SIGNAL",
                "No clean directional entry (bearish_call=%s bullish_put=%s)",
                bearish_call_signal,
                bullish_put_signal,
            )

    # ========== Internal helpers ==========

    # Find an instrument label matching a prefix.
    def _find_label_prefix(self, prefix: str) -> Optional[str]:
        """
        Find a label starting with prefix. If not found and the prefix looks like
        an ATM CE/PE placeholder, fall back to nearest-strike option (ITM/OTM)
        for this underlying.
        """
        for label in self.instrument_meta.keys():
            if label.startswith(prefix):
                return label
        # Fallback: derive nearest strike from available ITM/OTM options
        if "CE" in prefix:
            return self._select_nearest_option(side="CE")
        if "PE" in prefix:
            return self._select_nearest_option(side="PE")
        return None

    # Pick the option label with strike closest to target.
    def _select_nearest_option(
        self, *, side: str, target_price: Optional[float] = None
    ) -> Optional[str]:
        """
        Pick the option (CE/PE) with strike closest to the underlying price.
        Works when explicit ATM labels are absent (e.g., only ITM/OTM exist).
        """
        side = side.upper()
        base = self.underlying_label.split("_")[0]
        candidates: List[tuple[str, float]] = []
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

    # Parse strike price from an option label suffix.
    @staticmethod
    def _parse_strike_from_label(label: str) -> Optional[float]:
        try:
            part = label.rsplit("_", 1)[-1]
            return float(part)
        except Exception:
            return None

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
    def _remember_order_identity(
        self,
        label: str,
        *,
        broker_symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol_token: Optional[str] = None,
    ) -> None:
        meta = self.instrument_meta.get(label, {})
        resolved_exchange = exchange if exchange not in (None, "") else (
            meta.get("exchange") or meta.get("exch_seg")
        )
        self._order_identity_by_label[label] = {
            "broker_symbol": broker_symbol
            or self._resolve_broker_symbol(meta, label),
            "exchange": (
                str(resolved_exchange) if resolved_exchange not in (None, "") else None
            ),
            "symbol_token": symbol_token or self._resolve_symbol_token(meta),
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
    def _reset_exit_retry_state(self, label: str) -> None:
        self._next_exit_retry_mono_by_label.pop(label, None)
        self._exit_failure_count_by_label.pop(label, None)
        self._exit_circuit_open_until_mono_by_label.pop(label, None)
        self._exit_last_alert_mono_by_label.pop(label, None)

    # Register a failed exit attempt and schedule either retry or circuit open.
    def _register_exit_failure(
        self, *, label: str, now_mono: float
    ) -> tuple[int, float, bool]:
        attempt = int(self._exit_failure_count_by_label.get(label, 0)) + 1
        self._exit_failure_count_by_label[label] = attempt
        if attempt >= self._exit_max_retries:
            self._exit_circuit_open_until_mono_by_label[label] = (
                now_mono + self._exit_circuit_open_seconds
            )
            self._next_exit_retry_mono_by_label[label] = (
                now_mono + self._exit_circuit_open_seconds
            )
            self._exit_last_alert_mono_by_label[label] = now_mono
            return attempt, self._exit_circuit_open_seconds, True
        self._next_exit_retry_mono_by_label[label] = (
            now_mono + self._exit_retry_cooldown_seconds
        )
        return attempt, self._exit_retry_cooldown_seconds, False

    # Map a logical role (CE/PE/STRANGLE) to a label.
    def _resolve_role_to_label(self, role: str) -> Optional[str]:
        """
        Map a logical role (CE, PE, CE_LONG, STRANGLE_CE, etc.) to an actual instrument label.
        Priority:
          1) env_prefix-specific override: <ENV_PREFIX>ROLE_<ROLE>_LABEL
          2) global override: ROLE_<ROLE>_LABEL
          3) For simple CE/PE roles, fall back to ATM CE/PE labels
        """
        role_upper = role.upper()

        # Explicit override via env
        if role_upper in self.role_label_overrides:
            return self.role_label_overrides[role_upper]

        # Basic ATM CE/PE roles
        if role_upper in ("CE", "ATM_CE"):
            return self.ce_label
        if role_upper in ("PE", "ATM_PE"):
            return self.pe_label
        if role_upper in ("CE_LONG", "CE_SHORT"):
            return self.ce_label
        if role_upper in ("PE_LONG", "PE_SHORT"):
            return self.pe_label
        if role_upper in ("STRANGLE_CE", "OTM_CE"):
            return self.strangle_ce_label or self.ce_label
        if role_upper in ("STRANGLE_PE", "OTM_PE"):
            return self.strangle_pe_label or self.pe_label

        return None

    # Return per-underlying volatility thresholds if configured.
    def _resolve_vol_thresholds(self) -> Dict[str, float]:
        """
        Return per-underlying vol thresholds if provided; otherwise, empty dict
        so callers fall back to env-derived defaults.
        """
        return self.vol_thresholds.get(self.fut_label.upper(), {})

    # Record and log a no-trade decision.
    def _record_no_trade(self, reason: str, message: str, *args) -> None:
        """
        Log a structured NO_TRADE reason and track counts for diagnostics/backtests.
        """
        self.no_trade_counts[reason] += 1
        logger.info("[%s][NO_TRADE][%s] " + message, self.env_prefix, reason, *args)

    # ML gating/persistence is intentionally disabled; method kept as compatibility no-op.
    def _persist_ml_decision(
        self,
        prediction: Any,
        *,
        decision: str,
        no_trade_reason: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> None:
        _ = (prediction, decision, no_trade_reason, template_name)

    # ML gate is removed; keep wrapper so existing call sites remain stable.
    def _fire_template_with_ml(
        self, template_name: str, reason: Optional[str], candle, indicators: Dict[str, Any]
    ) -> None:
        _ = (candle, indicators)
        self._fire_template(template_name, reason=reason, ml_info=None)

    # Place an options order through the strategy bridge.
    def _place_option_order(
        self,
        label: str,
        side: str,
        qty: int,
        *,
        price: float,
        template_name: Optional[str] = None,
        role: Optional[str] = None,
        reason: Optional[str] = None,
        purpose: Optional[OrderPurpose] = None,
        ml_info: Optional[Dict[str, Any]] = None,
        broker_symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol_token: Optional[str] = None,
    ) -> bool:
        """
        Send BUY/SELL order for given label, respecting TRADE_MODE.
        """
        is_exit = purpose == OrderPurpose.EXIT
        if is_exit:
            if label in self._exit_in_flight_labels:
                return False
            now_mono = monotonic()
            circuit_until = self._exit_circuit_open_until_mono_by_label.get(label, 0.0)
            if now_mono < circuit_until:
                last_alert = self._exit_last_alert_mono_by_label.get(label, 0.0)
                if now_mono - last_alert >= self._exit_circuit_alert_interval_seconds:
                    self._exit_last_alert_mono_by_label[label] = now_mono
                    remaining = max(0.0, circuit_until - now_mono)
                    logger.error(
                        "[%s][ALERT] OPTION exit circuit active | label=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                        self.env_prefix,
                        label,
                        self._exit_failure_count_by_label.get(label, 0),
                        self._exit_max_retries,
                        remaining,
                    )
                return False
            next_retry = self._next_exit_retry_mono_by_label.get(label, 0.0)
            if now_mono < next_retry:
                return False
        meta = self.instrument_meta.get(label)
        if not meta and label not in self._order_identity_by_label:
            logger.error(
                "[%s] No instrument meta for %s, cannot place order",
                self.env_prefix,
                label,
            )
            return False

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
            tag=template_name or role,
            purpose=purpose,
            exchange=exchange or resolved_exchange,
            symbol_token=(
                str(symbol_token)
                if symbol_token not in (None, "")
                else (str(resolved_token) if resolved_token not in (None, "") else None)
            ),
        )

        if is_exit:
            self._exit_in_flight_labels.add(label)
        try:
            routing_kwargs = self._routing_kwargs()
            response = place_order_via_bridge(
                strategy_id=self._strategy_id,
                order_req=order_req,
                **routing_kwargs,
            )
        except Exception:
            if is_exit:
                attempt, wait_s, opened = self._register_exit_failure(
                    label=label,
                    now_mono=monotonic(),
                )
                if opened:
                    logger.exception(
                        "[%s][ALERT] OPTION exit circuit opened after exception | label=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                        self.env_prefix,
                        label,
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
                else:
                    logger.exception(
                        "[%s] Exit exception for %s attempt=%d/%d retry_in=%.2fs",
                        self.env_prefix,
                        label,
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
            return False
        finally:
            if is_exit:
                self._exit_in_flight_labels.discard(label)
        if response is None:
            if is_exit:
                attempt, wait_s, opened = self._register_exit_failure(
                    label=label,
                    now_mono=monotonic(),
                )
                if opened:
                    logger.error(
                        "[%s][ALERT] OPTION exit circuit opened after empty broker response | label=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                        self.env_prefix,
                        label,
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
                else:
                    logger.warning(
                        "[%s] Exit failed for %s due to empty broker response attempt=%d/%d retry_in=%.2fs",
                        self.env_prefix,
                        label,
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
            return False
        status = str(getattr(response, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            if is_exit:
                attempt, wait_s, opened = self._register_exit_failure(
                    label=label,
                    now_mono=monotonic(),
                )
                if opened:
                    logger.error(
                        "[%s][ALERT] OPTION exit circuit opened after broker rejection | label=%s status=%s msg=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                        self.env_prefix,
                        label,
                        status,
                        getattr(response, "message", ""),
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
                else:
                    logger.warning(
                        "[%s] Exit rejected for %s status=%s msg=%s attempt=%d/%d retry_in=%.2fs",
                        self.env_prefix,
                        label,
                        status,
                        getattr(response, "message", ""),
                        attempt,
                        self._exit_max_retries,
                        wait_s,
                    )
            return False
        if is_exit:
            self._reset_exit_retry_state(label)
        else:
            self._remember_order_identity(
                label,
                broker_symbol=broker_symbol or resolved_symbol,
                exchange=exchange or resolved_exchange,
                symbol_token=(
                    str(symbol_token)
                    if symbol_token not in (None, "")
                    else (str(resolved_token) if resolved_token not in (None, "") else None)
                ),
            )
        return True

    # ----- Template execution ("firing" a spread) -----

    # Execute a spread template by placing its legs.
    def _fire_template(
        self,
        template_name: str,
        reason: Optional[str] = None,
        *,
        ml_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        tpl = self.templates.get(template_name)
        if not tpl:
            logger.error("[%s] Unknown template: %s", self.env_prefix, template_name)
            return

        logger.info("[%s] Firing template: %s", self.env_prefix, tpl.name)

        for leg_tpl in tpl.legs:
            label = self._resolve_role_to_label(leg_tpl.role)
            if not label:
                logger.error(
                    "[%s] No label for role %s in template %s",
                    self.env_prefix,
                    leg_tpl.role,
                    tpl.name,
                )
                continue

            # Skip if already short this label (keep 1 short leg per label)
            existing = self.open_legs.get(label)
            if existing and existing.side == "SELL" and leg_tpl.side.upper() == "SELL":
                logger.info(
                    "[%s] Already short %s, skipping new entry", self.env_prefix, label
                )
                continue

            # Need latest price to set SL/TP
            ltp = self.last_price.get(label)
            if ltp is None:
                logger.info(
                    "[%s] No LTP yet for %s, cannot enter", self.env_prefix, label
                )
                continue

            meta = self.instrument_meta.get(label)
            if not meta:
                logger.error(
                    "[%s] No instrument meta for %s, cannot determine lot size",
                    self.env_prefix,
                    label,
                )
                continue

            lot_size = meta.get("lot_size")
            if not lot_size:
                logger.error(
                    "[%s] Lot size not found for %s in instrument_meta, skipping order",
                    self.env_prefix,
                    label,
                )
                continue

            qty = max(1, int(leg_tpl.qty_mult * self.qty_per_trade))
            side = leg_tpl.side.upper()

            if side == "SELL":
                # Enter short with SL/TP/trailing
                entry_price = ltp
                sl_price = entry_price * (1.0 + self.sl_pct)
                tp_price = entry_price * (1.0 - self.tp_pct)
                broker_symbol, exchange, symbol_token = self._resolve_order_identity(label)

                pos = OpenLegPosition(
                    label=label,
                    role=leg_tpl.role.upper(),
                    side="SELL",
                    qty=qty,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    trail_buffer_pct=self.trail_buffer_pct,
                    best_price=entry_price,
                    template_name=tpl.name,
                    underlying=self.underlying_label,
                    broker_symbol=broker_symbol,
                    exchange=exchange,
                    symbol_token=symbol_token,
                )
                success = self._place_option_order(
                    label,
                    "SELL",
                    qty,
                    price=entry_price,
                    template_name=tpl.name,
                    role=leg_tpl.role,
                    reason=reason,
                    purpose=OrderPurpose.ENTRY,
                    ml_info=ml_info,
                )
                if not success:
                    continue
                self.open_legs[label] = pos
                self._reset_exit_retry_state(label)

            elif side == "BUY":
                # For completeness (e.g. buying hedge leg in spreads)
                entry_price = ltp
                broker_symbol, exchange, symbol_token = self._resolve_order_identity(label)
                pos = OpenLegPosition(
                    label=label,
                    role=leg_tpl.role.upper(),
                    side="BUY",
                    qty=qty,
                    entry_price=entry_price,
                    sl_price=0.0,
                    tp_price=0.0,
                    trail_buffer_pct=0.0,
                    best_price=entry_price,
                    template_name=tpl.name,
                    underlying=self.underlying_label,
                    broker_symbol=broker_symbol,
                    exchange=exchange,
                    symbol_token=symbol_token,
                )
                success = self._place_option_order(
                    label,
                    "BUY",
                    qty,
                    price=entry_price,
                    template_name=tpl.name,
                    role=leg_tpl.role,
                    reason=reason,
                    purpose=OrderPurpose.ENTRY,
                    ml_info=ml_info,
                )
                if not success:
                    continue
                self.open_legs[label] = pos
                self._reset_exit_retry_state(label)

    # ----- Risk updates per tick -----

    # Update trailing SL/TP and close legs when thresholds hit.
    def _update_open_leg_risk(self, label: str, ltp: float) -> None:
        """
        For each tick in CE/PE/etc., check SL/TP/trailing and exit if needed.
        """
        pos = self.open_legs.get(label)
        if not pos:
            return

        # We mainly care about short options here
        if pos.side != "SELL":
            return

        # Update "best_price" (for a short, lowest seen price)
        if ltp < pos.best_price:
            pos.best_price = ltp

        # Trailing SL: keep SL at [best_price * (1 + trail_buffer_pct)]
        new_trail_sl = pos.best_price * (1.0 + pos.trail_buffer_pct)
        if new_trail_sl < pos.sl_price:
            logger.info(
                "[%s] Trailing SL for %s: old=%.3f new=%.3f (best_price=%.3f)",
                self.env_prefix,
                label,
                pos.sl_price,
                new_trail_sl,
                pos.best_price,
            )
            pos.sl_price = new_trail_sl

        # Check exits
        # Hard SL: premium has increased vs entry
        if ltp >= pos.sl_price:
            logger.info(
                "[%s] SL HIT | %s | entry=%.3f sl=%.3f ltp=%.3f, closing short",
                self.env_prefix,
                label,
                pos.entry_price,
                pos.sl_price,
                ltp,
            )
            if self._place_option_order(
                label,
                "BUY",
                pos.qty,
                price=ltp,
                template_name=pos.template_name,
                role=pos.role,
                purpose=OrderPurpose.EXIT,
                broker_symbol=pos.broker_symbol,
                exchange=pos.exchange,
                symbol_token=pos.symbol_token,
            ):
                del self.open_legs[label]
            return

        # Strategy-hub integration summary:
        # - All orders flow through strategy_bridge.place_order_via_bridge.
        # - Strategy enable toggles in strategy_env.yaml control whether each strategy is instantiated in multi_instrument_stream.
        # Take profit: premium dropped sufficiently
        if ltp <= pos.tp_price:
            logger.info(
                "[%s] TP HIT | %s | entry=%.3f tp=%.3f ltp=%.3f, closing short",
                self.env_prefix,
                label,
                pos.entry_price,
                pos.tp_price,
                ltp,
            )
            if self._place_option_order(
                label,
                "BUY",
                pos.qty,
                price=ltp,
                template_name=pos.template_name,
                role=pos.role,
                purpose=OrderPurpose.EXIT,
                broker_symbol=pos.broker_symbol,
                exchange=pos.exchange,
                symbol_token=pos.symbol_token,
            ):
                del self.open_legs[label]
            return

    # Force exit all open legs and clear state.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.open_legs:
            return
        for label, pos in list(self.open_legs.items()):
            exit_side = "BUY" if pos.side == "SELL" else "SELL"
            price = self.last_price.get(label, pos.entry_price)
            if submit_orders:
                ok = self._place_option_order(
                    label,
                    exit_side,
                    pos.qty,
                    price=price,
                    template_name=pos.template_name,
                    role=pos.role,
                    purpose=OrderPurpose.EXIT,
                    broker_symbol=pos.broker_symbol,
                    exchange=pos.exchange,
                    symbol_token=pos.symbol_token,
                )
                if not ok:
                    continue
            self.open_legs.pop(label, None)
            self._reset_exit_retry_state(label)
