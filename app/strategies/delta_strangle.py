"""Strategy module for delta strangles (§5 bridge into hub risk/execution).

Tenancy note:
- Strategies now pass tenant_id and broker_account_id resolved from Settings into
  strategy_bridge so hub routing always has explicit IDs.
- Adding more tenants does not require more env vars; new tenants live in Firestore.
"""

from __future__ import annotations

import logging
import os
from time import monotonic
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import Any, Dict, Optional

from app.core.order_client import AngelOrderClient
from app.core.risk_manager import RiskManager
from app.strategies.base import BaseStrategy
from app.config.settings import get_settings
from app.config.boot_config import StrategyValueResolver
from app.orders.strategy_bridge import place_order_via_bridge
from app.strategies.naming import canonicalize_strategy_name
from app.strategies.identifiers import DELTA_STRANGLE_ID
from app.strategies.restart_state import (
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


# Convert a datetime to IST.
def _to_ist(dt: datetime) -> datetime:
    return dt.astimezone(IST)


# State for an open delta strangle leg.
@dataclass
class OpenLeg:
    label: str
    side: str
    qty_in_lots: int
    qty: int
    entry_price: float
    sl_price: float
    tp_price: float
    entry_time: datetime
    role: Optional[str] = None
    broker_symbol: Optional[str] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None


# Daily delta-based strangle strategy.
class DeltaStrangleStrategy(BaseStrategy):
    """
    Simple daily strangle: sell a fixed qty of CE and PE targeting ~0.2 delta.

    - No indicator dependency; fires once per day inside the entry window (IST).
    - CE/PE labels can be overridden via env (<PREFIX>DELTA_CE_LABEL / DELTA_PE_LABEL).
    - Delta targeting is best-effort: prefers far OTM strikes available in instrument_meta.
    - Uses existing RiskManager for execution/persistence if provided; otherwise falls back to AngelOrderClient.
    """

    # Initialize strategy configuration and state.
    def __init__(
        self,
        instrument_meta: Dict[str, Dict[str, Any]],
        *,
        env_prefix: str = "NIFTY_",
        underlying_label: str = "NIFTY_IDX",
        ce_label_prefix: str = "NIFTY",
        pe_label_prefix: str = "NIFTY",
        risk_manager: Optional[RiskManager] = None,
        order_client: Optional[AngelOrderClient] = None,
        delta_qty_per_leg: int = 1,
        delta_qty_per_leg_by_underlying: Optional[Dict[str, Any]] = None,
        entry_start_ist: str = "09:20",
        entry_end_ist: str = "15:00",
        sl_pct: float = 0.30,
        tp_pct: float = 0.30,
        config_resolver: Optional[StrategyValueResolver] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.env_prefix = env_prefix
        self.underlying_label = underlying_label
        self.underlying_key = self._normalize_underlying_key(self.underlying_label)
        self.ce_label_prefix = ce_label_prefix
        self.pe_label_prefix = pe_label_prefix
        self.risk_manager = risk_manager
        self.order_client = order_client
        self._cfg = config_resolver or StrategyValueResolver(
            env_prefix=self.env_prefix,
            params={},
            env=dict(os.environ),
        )

        self.trade_mode = (
            risk_manager.current_trade_mode if risk_manager else None
        ) or "PAPER"
        self.product_type = "INTRADAY"  # intraday only
        settings = get_settings()
        self._strategy_id = DELTA_STRANGLE_ID
        if self.env_prefix == "NIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_nifty_delta
        elif self.env_prefix == "BANKNIFTY_":
            self._use_hub_router_flag = settings.use_hub_router_for_banknifty_delta
        else:
            self._use_hub_router_flag = False

        self.entry_start = self._parse_time(entry_start_ist) or time(9, 20)
        self.entry_end = self._parse_time(entry_end_ist) or time(15, 0)

        self.ce_override = self._env("DELTA_CE_LABEL")
        self.pe_override = self._env("DELTA_PE_LABEL")
        self.sl_pct = float(sl_pct)
        self.tp_pct = float(tp_pct)

        self.delta_qty_per_leg = self._parse_delta_qty(
            self._env("DELTA_QTY_PER_LEG"),
            fallback=delta_qty_per_leg or 1,
        )
        self.delta_qty_per_leg_by_underlying: Dict[str, int] = {}
        for key, val in (delta_qty_per_leg_by_underlying or {}).items():
            norm_key = self._normalize_underlying_key(key)
            if not norm_key:
                continue
            qty_val = self._parse_delta_qty(val, fallback=self.delta_qty_per_leg)
            self.delta_qty_per_leg_by_underlying[norm_key] = qty_val

        self.last_price: Dict[str, float] = {}
        self.last_entry_date: Optional[datetime.date] = None
        self.open_legs: Dict[str, OpenLeg] = {}
        self._order_identity_by_label: Dict[str, Dict[str, Optional[str]]] = {}
        self._exit_in_flight_labels: set[str] = set()
        self._next_exit_retry_mono_by_label: Dict[str, float] = {}
        try:
            retry_cfg = float(
                self._env("DELTA_EXIT_RETRY_COOLDOWN_SECONDS", "5") or "5"
            )
        except (TypeError, ValueError):
            retry_cfg = 5.0
        self._exit_retry_cooldown_seconds = max(0.5, retry_cfg)
        self._exit_max_retries = max(
            1,
            self._parse_delta_qty(
                self._env("DELTA_EXIT_MAX_RETRIES"),
                fallback=12,
            ),
        )
        try:
            circuit_open = float(
                self._env("DELTA_EXIT_CIRCUIT_OPEN_SECONDS", "300") or "300"
            )
        except (TypeError, ValueError):
            circuit_open = 300.0
        self._exit_circuit_open_seconds = max(
            self._exit_retry_cooldown_seconds,
            circuit_open,
        )
        try:
            alert_interval = float(
                self._env("DELTA_EXIT_CIRCUIT_ALERT_INTERVAL_SECONDS", "60") or "60"
            )
        except (TypeError, ValueError):
            alert_interval = 60.0
        self._exit_circuit_alert_interval_seconds = max(15.0, alert_interval)
        self._exit_failure_count_by_label: Dict[str, int] = {}
        self._exit_circuit_open_until_mono_by_label: Dict[str, float] = {}
        self._exit_last_alert_mono_by_label: Dict[str, float] = {}

        logger.info(
            "[%s] DeltaStrangle init | underlying=%s delta_qty=%d entry=%s-%s",
            self.env_prefix,
            self.underlying_label,
            self._resolve_delta_qty(),
            self.entry_start,
            self.entry_end,
        )
        logger.info(
            "[%s] DELTA exit protection enabled | max_retries=%d circuit_open_seconds=%.1f alert_interval_seconds=%.1f",
            self.env_prefix,
            self._exit_max_retries,
            self._exit_circuit_open_seconds,
            self._exit_circuit_alert_interval_seconds,
        )
        self._restore_open_legs_from_risk_manager()

    # Decide hub routing kwargs for order placement.
    def _routing_kwargs(self) -> dict:
        """Route selection is handled centrally in strategy_bridge."""
        return {}

    # Refresh instrument metadata.
    def refresh_meta(self, instrument_meta: Dict[str, Dict[str, Any]]) -> None:
        self.instrument_meta = instrument_meta

    # Read an env var using the strategy prefix.
    def _env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        raw = self._cfg.get(key, default)
        if raw is None:
            return None
        text = str(raw)
        if not text and default is None:
            return None
        return text

    # Normalize an underlying name for map lookup.
    def _normalize_underlying_key(self, name: Any) -> str:
        return str(name or "").upper().replace("_IDX", "")

    # Parse delta quantity with a fallback.
    def _parse_delta_qty(self, val: Any, *, fallback: int = 1) -> int:
        if val is None:
            return fallback
        try:
            qty_val = int(val)
            if qty_val > 0:
                return qty_val
        except (TypeError, ValueError):
            pass
        logger.warning(
            "[%s] Invalid delta qty=%s, using %d", self.env_prefix, val, fallback
        )
        return fallback

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

    # Resolve per-underlying quantity override.
    def _resolve_delta_qty(self) -> int:
        qty = self.delta_qty_per_leg_by_underlying.get(
            self.underlying_key, self.delta_qty_per_leg
        )
        if qty <= 0:
            logger.warning(
                "[%s] Non-positive delta qty for %s -> defaulting to 1",
                self.env_prefix,
                self.underlying_key,
            )
            return 1
        return qty

    # Convert lot-based quantity into absolute quantity.
    def _resolve_leg_qty(self, label: str, qty_in_lots: int) -> Optional[int]:
        meta = self.instrument_meta.get(label)
        if not meta:
            return None
        lot_size = meta.get("lot_size")
        if not lot_size:
            return None
        try:
            return int(lot_size) * int(qty_in_lots)
        except (TypeError, ValueError):
            return None

    def _infer_leg_role(
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

    def _leg_strategy_context(self, leg: OpenLeg) -> Dict[str, Any]:
        return {
            "leg_state_version": 1,
            "leg_role": leg.role,
            "qty_in_lots": int(leg.qty_in_lots),
            "position_qty": int(leg.qty),
            "sl_price": float(leg.sl_price),
            "tp_price": float(leg.tp_price),
            "entry_time": leg.entry_time.astimezone(timezone.utc).isoformat(),
            "managed_exit_mode": "strategy",
            "broker_brackets_enabled": False,
        }

    def _sync_leg_state_to_risk_manager(self, leg: OpenLeg) -> None:
        sync_open_position_state(
            risk_manager=self.risk_manager,
            label=leg.label,
            side=leg.side,
            qty=max(1, int(leg.qty)),
            entry_price=float(leg.entry_price),
            strategy_context=self._leg_strategy_context(leg),
        )

    def _restore_open_legs_from_risk_manager(self) -> None:
        if self.open_legs or self.risk_manager is None:
            return
        restored_positions = getattr(self.risk_manager, "restored_positions", None)
        if not isinstance(restored_positions, dict) or not restored_positions:
            return

        restored_dates: list[datetime.date] = []
        for raw_label, raw_entry in restored_positions.items():
            label = str(raw_label or "").strip()
            if not label or not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            if str(entry.get("side") or "").strip().upper() != "SELL":
                continue

            strategy_match = False
            for raw_name in (
                str(entry.get("strategy_name") or "").strip(),
                str(entry.get("template_name") or "").strip(),
            ):
                if not raw_name:
                    continue
                if raw_name == self._strategy_id:
                    strategy_match = True
                    break
                if (
                    canonicalize_strategy_name(
                        raw_name,
                        source="delta_strangle.restore",
                        warn_alias=False,
                    )
                    == "delta_strangle"
                ):
                    strategy_match = True
                    break
            if not strategy_match:
                continue

            meta = self.instrument_meta.get(label, {})
            restored_underlying = str(
                entry.get("underlying") or meta.get("underlying") or ""
            ).strip().upper()
            if restored_underlying and restored_underlying != self.underlying_key:
                continue

            strategy_context = (
                dict(entry.get("strategy_context"))
                if isinstance(entry.get("strategy_context"), dict)
                else {}
            )
            entry_price = parse_non_negative_float(entry.get("entry_price"))
            broker_qty = parse_positive_int(entry.get("qty"))
            if entry_price is None or broker_qty is None:
                continue
            stored_qty = parse_positive_int(strategy_context.get("position_qty"))
            if stored_qty is not None and stored_qty != broker_qty:
                logger.warning(
                    "[%s] Delta restore qty reconciled to broker state | label=%s stored_qty=%s broker_qty=%s",
                    self.env_prefix,
                    label,
                    stored_qty,
                    broker_qty,
                )
            lot_size = parse_positive_int(meta.get("lot_size")) or 1
            qty_in_lots = parse_positive_int(strategy_context.get("qty_in_lots"))
            if qty_in_lots is None:
                qty_in_lots = max(1, int(round(float(broker_qty) / float(lot_size))))
            role = (
                str(
                    strategy_context.get("leg_role")
                    or strategy_context.get("role")
                    or self._infer_leg_role(label, meta=meta)
                    or ""
                )
                .strip()
                .upper()
                or None
            )
            entry_time = (
                parse_datetime(strategy_context.get("entry_time"))
                or parse_datetime(entry.get("entry_ts"))
                or datetime.now(timezone.utc)
            )
            broker_symbol = str(
                entry.get("tradingsymbol")
                or self._resolve_broker_symbol(meta, label)
            ).strip()
            exchange = str(
                entry.get("exchange") or meta.get("exchange") or meta.get("exch_seg") or ""
            ).strip() or None
            symbol_token = str(
                entry.get("symboltoken") or meta.get("token") or ""
            ).strip() or None
            leg = OpenLeg(
                label=label,
                side="SELL",
                qty_in_lots=int(qty_in_lots),
                qty=int(broker_qty),
                entry_price=float(entry_price),
                sl_price=(
                    parse_non_negative_float(strategy_context.get("sl_price"))
                    or float(entry_price) * (1.0 + self.sl_pct)
                ),
                tp_price=(
                    parse_non_negative_float(strategy_context.get("tp_price"))
                    or float(entry_price) * (1.0 - self.tp_pct)
                ),
                entry_time=entry_time,
                role=role,
                broker_symbol=broker_symbol or None,
                exchange=exchange,
                symbol_token=symbol_token,
            )
            self.open_legs[label] = leg
            self._order_identity_by_label[label] = {
                "broker_symbol": leg.broker_symbol,
                "exchange": leg.exchange,
                "symbol_token": leg.symbol_token,
            }
            self._reset_exit_retry_state(label)
            restored_dates.append(entry_time.astimezone(IST).date())

        if restored_dates:
            self.last_entry_date = max(restored_dates)
            logger.info(
                "[%s] Restored delta strangle legs after restart | count=%d labels=%s",
                self.env_prefix,
                len(self.open_legs),
                sorted(self.open_legs.keys()),
            )

    # Parse time strings (HH:MM).
    def _parse_time(self, val: str) -> Optional[time]:
        try:
            h, m = (int(p) for p in val.split(":"))
            return time(hour=h, minute=m)
        except Exception:
            return None

    # Handle tick updates and manage entries/exits.
    def on_tick(self, label: str, ltp: float) -> None:
        if not hasattr(self, "last_price"):
            self.last_price = {}
        self.last_price[label] = ltp
        if self.open_legs and label in self.open_legs:
            self._maybe_exit_leg(label, ltp)
        if label != self.underlying_label:
            return
        self._maybe_fire_strangle()

    # No bar-based logic for this strategy.
    def on_bar(
        self, label: str, timeframe_seconds: int, candle, indicators: Dict[str, Any]
    ) -> None:
        # No indicator-driven logic; only uses ticks to fire once per day.
        return

    # ---- Core logic ----

    # Try to enter the strangle within the entry window.
    def _maybe_fire_strangle(self) -> None:
        now_ist = _to_ist(datetime.now(timezone.utc))
        if not (self.entry_start <= now_ist.time() <= self.entry_end):
            return
        if self.last_entry_date == now_ist.date():
            return
        if self.open_legs:
            return

        ce_label = self._pick_leg("CE")
        pe_label = self._pick_leg("PE")
        if not ce_label or not pe_label:
            logger.warning(
                "[%s] No CE/PE labels resolved for delta strangle", self.env_prefix
            )
            return
        ce_price = self.last_price.get(ce_label)
        pe_price = self.last_price.get(pe_label)
        if ce_price is None or pe_price is None:
            # Wait until we have both option ticks
            return
        qty_in_lots = self._resolve_delta_qty()
        ce_qty = self._resolve_leg_qty(ce_label, qty_in_lots)
        pe_qty = self._resolve_leg_qty(pe_label, qty_in_lots)
        if ce_qty is None or pe_qty is None:
            logger.warning(
                "[%s] Unable to resolve leg quantity for CE=%s PE=%s",
                self.env_prefix,
                ce_label,
                pe_label,
            )
            return
        entry_time = datetime.now(timezone.utc)
        ce_symbol, ce_exchange, ce_token = self._resolve_order_identity(ce_label)
        pe_symbol, pe_exchange, pe_token = self._resolve_order_identity(pe_label)
        ce_leg = OpenLeg(
            label=ce_label,
            side="SELL",
            qty_in_lots=qty_in_lots,
            qty=ce_qty,
            entry_price=ce_price,
            sl_price=ce_price * (1.0 + self.sl_pct),
            tp_price=ce_price * (1.0 - self.tp_pct),
            entry_time=entry_time,
            role="CE",
            broker_symbol=ce_symbol,
            exchange=ce_exchange,
            symbol_token=ce_token,
        )
        pe_leg = OpenLeg(
            label=pe_label,
            side="SELL",
            qty_in_lots=qty_in_lots,
            qty=pe_qty,
            entry_price=pe_price,
            sl_price=pe_price * (1.0 + self.sl_pct),
            tp_price=pe_price * (1.0 - self.tp_pct),
            entry_time=entry_time,
            role="PE",
            broker_symbol=pe_symbol,
            exchange=pe_exchange,
            symbol_token=pe_token,
        )
        ok_ce = self._place_order(
            ce_label,
            "SELL",
            qty_in_lots,
            ce_price,
            role="CE",
            purpose=OrderPurpose.ENTRY,
            ml_info=None,
            broker_symbol=ce_symbol,
            exchange=ce_exchange,
            symbol_token=ce_token,
            strategy_context=self._leg_strategy_context(ce_leg),
        )
        ok_pe = self._place_order(
            pe_label,
            "SELL",
            qty_in_lots,
            pe_price,
            role="PE",
            purpose=OrderPurpose.ENTRY,
            ml_info=None,
            broker_symbol=pe_symbol,
            exchange=pe_exchange,
            symbol_token=pe_token,
            strategy_context=self._leg_strategy_context(pe_leg),
        )
        if ok_ce or ok_pe:
            self.last_entry_date = now_ist.date()
        if ok_ce:
            self.open_legs[ce_label] = ce_leg
            self._order_identity_by_label[ce_label] = {
                "broker_symbol": ce_leg.broker_symbol,
                "exchange": ce_leg.exchange,
                "symbol_token": ce_leg.symbol_token,
            }
            self._reset_exit_retry_state(ce_label)
            self._sync_leg_state_to_risk_manager(ce_leg)
        if ok_pe:
            self.open_legs[pe_label] = pe_leg
            self._order_identity_by_label[pe_label] = {
                "broker_symbol": pe_leg.broker_symbol,
                "exchange": pe_leg.exchange,
                "symbol_token": pe_leg.symbol_token,
            }
            self._reset_exit_retry_state(pe_label)
            self._sync_leg_state_to_risk_manager(pe_leg)
        if ok_ce and ok_pe:
            logger.info(
                "[%s] Entered intraday delta strangle | CE=%s@%.2f PE=%s@%.2f qty_in_lots=%d",
                self.env_prefix,
                ce_label,
                ce_price,
                pe_label,
                pe_price,
                qty_in_lots,
            )

    # Pick the CE/PE leg label based on config and universe.
    def _pick_leg(self, side: str) -> Optional[str]:
        # Env overrides first
        if side == "CE" and self.ce_override:
            return self.ce_override
        if side == "PE" and self.pe_override:
            return self.pe_override

        # New logic: find pre-selected OTM strikes from universe
        candidates = []
        for label, meta in self.instrument_meta.items():
            if meta.get("underlying") != self.underlying_label.replace("_IDX", ""):
                continue

            kind = meta.get("kind", "")
            if side == "CE" and "OTM_CE" in kind:
                candidates.append((float(meta.get("strike", 0.0)), label))
            elif side == "PE" and "OTM_PE" in kind:
                candidates.append((float(meta.get("strike", 0.0)), label))

        if not candidates:
            logger.warning(
                f"[{self.env_prefix}] No OTM candidates found for side={side}"
            )
            return None

        # For a strangle, we want far OTM strikes.
        # Pick the highest strike for CE and the lowest strike for PE.
        if side == "CE":
            return max(candidates, key=lambda c: c[0])[1]
        else:  # PE
            return min(candidates, key=lambda c: c[0])[1]

    # Submit an order through the strategy bridge.
    def _place_order(
        self,
        label: str,
        side: str,
        qty_in_lots: int,
        price: float,
        *,
        role: Optional[str] = None,
        tag: Optional[str] = None,
        purpose: Optional[OrderPurpose] = None,
        ml_info: Optional[Dict[str, Any]] = None,
        broker_symbol: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol_token: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        meta = self.instrument_meta.get(label)
        if not meta:
            logger.error(
                "[%s] No meta for %s, cannot place order", self.env_prefix, label
            )
            return False

        lot_size = meta.get("lot_size")
        if not lot_size:
            logger.error(
                "[%s] Lot size not found for %s in instrument_meta, skipping order",
                self.env_prefix,
                label,
            )
            return False

        qty = int(qty_in_lots)

        logger.debug(
            "[DELTA] placing order: underlying=%s, symbol=%s, qty=%d",
            meta.get("underlying") or self.underlying_key,
            label,
            qty,
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
            tag=tag or role,
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
        response = place_order_via_bridge(
            strategy_id=self._strategy_id,
            order_req=order_req,
            **routing_kwargs,
        )
        if response is None:
            logger.warning(
                "[%s] Order placement returned None for %s", self.env_prefix, label
            )
            return False

        status = str(getattr(response, "status", "")).upper()
        if status in {"REJECTED", "FAILED"}:
            logger.warning(
                "[%s] Order rejected for %s: %s",
                self.env_prefix,
                label,
                getattr(response, "message", "unknown_reason"),
            )
            return False

        return True

    # Exit a leg and remove it from state.
    def _exit_leg(self, leg: OpenLeg, *, reason: str, ltp: float) -> bool:
        if leg.label in self._exit_in_flight_labels:
            return False
        now_mono = monotonic()
        circuit_until = self._exit_circuit_open_until_mono_by_label.get(leg.label, 0.0)
        if now_mono < circuit_until:
            last_alert = self._exit_last_alert_mono_by_label.get(leg.label, 0.0)
            if now_mono - last_alert >= self._exit_circuit_alert_interval_seconds:
                self._exit_last_alert_mono_by_label[leg.label] = now_mono
                remaining = max(0.0, circuit_until - now_mono)
                logger.error(
                    "[%s][ALERT] DELTA exit circuit active | label=%s broker_symbol=%s attempts=%d/%d next_retry_in=%.1fs | manual intervention required",
                    self.env_prefix,
                    leg.label,
                    leg.broker_symbol or leg.label,
                    self._exit_failure_count_by_label.get(leg.label, 0),
                    self._exit_max_retries,
                    remaining,
                )
            return False
        next_retry = self._next_exit_retry_mono_by_label.get(leg.label, 0.0)
        if now_mono < next_retry:
            return False
        self._exit_in_flight_labels.add(leg.label)
        try:
            ok = self._place_order(
                leg.label,
                "BUY",
                leg.qty_in_lots,
                ltp,
                role=leg.role,
                tag=f"DELTA_STRANGLE_EXIT_{reason}",
                purpose=OrderPurpose.EXIT,
                broker_symbol=leg.broker_symbol,
                exchange=leg.exchange,
                symbol_token=leg.symbol_token,
            )
        except Exception:
            attempt, wait_s, opened = self._register_exit_failure(
                label=leg.label,
                now_mono=monotonic(),
            )
            if opened:
                logger.exception(
                    "[%s][ALERT] DELTA exit circuit opened after exception | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    leg.label,
                    leg.broker_symbol or leg.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.exception(
                    "[%s] Exit exception for %s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    leg.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return False
        finally:
            self._exit_in_flight_labels.discard(leg.label)
        if not ok:
            attempt, wait_s, opened = self._register_exit_failure(
                label=leg.label,
                now_mono=monotonic(),
            )
            if opened:
                logger.error(
                    "[%s][ALERT] DELTA exit circuit opened after broker rejection | label=%s broker_symbol=%s reason=%s attempts=%d/%d pause=%.1fs | manual intervention required",
                    self.env_prefix,
                    leg.label,
                    leg.broker_symbol or leg.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            else:
                logger.warning(
                    "[%s] Exit rejected for %s reason=%s attempt=%d/%d retry_in=%.2fs",
                    self.env_prefix,
                    leg.label,
                    reason,
                    attempt,
                    self._exit_max_retries,
                    wait_s,
                )
            return False
        self._reset_exit_retry_state(leg.label)
        if ok:
            self.open_legs.pop(leg.label, None)
        return ok

    # Check SL/TP conditions for an open leg.
    def _maybe_exit_leg(self, label: str, ltp: float) -> None:
        leg = self.open_legs.get(label)
        if not leg:
            return
        if leg.side != "SELL":
            return
        if ltp >= leg.sl_price:
            self._exit_leg(leg, reason="SL", ltp=ltp)
        elif ltp <= leg.tp_price:
            self._exit_leg(leg, reason="TP", ltp=ltp)

    # Force exit all open legs.
    def force_exit_all(
        self, reason: str = "FORCED_EXIT", *, submit_orders: bool = True
    ) -> None:
        if not self.open_legs:
            return
        for leg in list(self.open_legs.values()):
            if submit_orders:
                ltp = self.last_price.get(leg.label, leg.entry_price)
                ok = self._exit_leg(leg, reason=reason, ltp=ltp)
                if not ok:
                    continue
            else:
                self.open_legs.pop(leg.label, None)
            self._reset_exit_retry_state(leg.label)


# Strategy-hub integration summary:
# - Orders route via strategy_bridge.place_order_via_bridge.
# - Strategy toggles come from strategy_env.yaml.
