"""Capital & risk controls (§5.2–§5.3) used by the execution hub AccountRunner."""

# Risk manager for order gating, PnL tracking, and kill switch enforcement.
# Tracks open positions, pending orders, and persistence for hub trading.
import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Any, Tuple
from datetime import datetime, timezone, timedelta, date
from app.core.dashboard_bus import dashboard_bus
from app.core.identifiers import BrokerAccountId, TenantId
from app.core.maintenance import MaintenanceManager
from app.core.instrument_control import InstrumentController
from app.core.order_client import (
    AngelHTTPBlockedError,
    AngelHTTPResponseError,
    AngelOrderClient,
)
from app.core.executed_tokens_tracker import executed_tokens_tracker
from app.core.trade_persister import TradePersister
from app.core.logging_utils import log_event
from app.risk.account_loss_guard import account_loss_guard
from app.core.p0_operational_guards import P0RuleViolation, assert_legacy_exit_allowed
from app.strategies.naming import canonicalize_strategy_name
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
    OrderPurpose,
)

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parent
CONFIG_DIR = APP_DIR / "config"
_LEGACY_STATE_PATH = CONFIG_DIR / "risk_positions.json"
_DEFAULT_STATE_PATH = REPO_ROOT / "logs" / "risk_positions.json"


def _resolve_risk_state_path(override: Optional[str] = None) -> Path:
    """Return the canonical risk state path, driven by RISK_STATE_PATH env var."""
    raw = override or os.getenv("RISK_STATE_PATH", "").strip()
    if raw:
        return Path(raw).resolve()
    return _DEFAULT_STATE_PATH
IST = timezone(timedelta(hours=5, minutes=30))


# Simple container for risk decision outcomes.
@dataclass
class RiskDecision:
    allowed: bool
    status: str
    reason: str

# Manage risk checks, order submission, and position state.
class RiskManager:
    # Initialize risk limits, dependencies, and persisted state.
    def __init__(
        self,
        *,
        instrument_meta: Dict[str, Dict[str, Any]],
        order_client: AngelOrderClient,
        max_open_spreads_per_underlying: int = 1,
        per_underlying_max_open_spreads: Optional[Dict[str, int]] = None,
        day_loss_limit: float = 0.0,
        max_daily_loss: float = 0.0,
        max_intraday_drawdown: float = 0.0,
        kill_switch_square_off_open_positions: bool = True,
        risk_limits: Optional[Dict[str, Any]] = None,
        state_path: Optional[str] = None,
        trade_persister: Optional[TradePersister] = None,
        instrument_controller: Optional[InstrumentController] = None,
        tenant_id: Optional[TenantId] = None,
        broker_account_id: Optional[BrokerAccountId] = None,
    ) -> None:
        self.instrument_meta = instrument_meta
        self.order_client = order_client
        self.max_open_spreads = max_open_spreads_per_underlying
        # Normalize overrides to uppercase underlying keys for consistent lookup.
        self.per_underlying_max_open_spreads = {
            (k or "").upper(): int(v)
            for k, v in (per_underlying_max_open_spreads or {}).items()
            if v is not None
        }
        self.day_loss_limit = day_loss_limit
        self.max_daily_loss = float(max_daily_loss or 0.0)
        self.max_intraday_drawdown = float(max_intraday_drawdown or 0.0)
        self.kill_switch_square_off_open_positions = (
            True if kill_switch_square_off_open_positions else False
        )
        self.state_path = _resolve_risk_state_path(state_path)
        self.trade_persister = trade_persister
        self.current_trade_mode = "PAPER"

        # Order confirmation (prevents phantom entry/exit when broker rejects/doesn't fill).
        self.order_confirm_timeout_seconds = float(
            os.getenv("ANGEL_ORDER_CONFIRM_TIMEOUT_SECONDS", "8")
        )
        self.order_confirm_poll_seconds = float(
            os.getenv("ANGEL_ORDER_CONFIRM_POLL_SECONDS", "0.8")
        )
        # Pending orders (order_id -> context). We block duplicate actions for the same
        # label until the broker order reaches a terminal state.
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self.tenant_id = str(tenant_id or "").strip() or None
        self.broker_account_id = str(broker_account_id or "").strip() or None
        self._forced_exit_suppression_seconds = max(
            5.0,
            float(os.getenv("RISK_FORCED_EXIT_SUPPRESSION_SECONDS", "45")),
        )
        self._forced_exit_suppression_until: Dict[str, float] = {}
        # Optional runtime hook used to route emergency/safety exits via hub OrderRouter.
        # When unset, legacy direct broker placement is used.
        self._hub_exit_submitter: Optional[Callable[..., bool]] = None
        # Operating mode string for authority-path enforcement.
        self._operating_mode: str = ""

        self.open_spreads_by_underlying: Dict[str, Dict[str, set[str]]] = {}
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.realized_pnl = 0.0
        self.max_equity = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_peak_equity = 0.0
        self.daily_peak_total_pnl = 0.0
        self.last_unrealized_pnl = 0.0
        self.last_total_pnl = 0.0
        self.kill_switch_activated = False
        self.kill_switch_date: Optional[date] = None
        self.risk_limits = risk_limits or {}
        self.instrument_controller = instrument_controller
        self._underlying_name_to_label: Dict[str, str] = {}
        self.maintenance: Optional[MaintenanceManager] = None
        self._include_unrealized_in_kill_switch = (
            str(
                os.getenv("RISK_INCLUDE_UNREALIZED_IN_DAILY_LOSS", "true") or "true"
            )
            .strip()
            .lower()
            not in {"0", "false", "no", "off"}
        )
        self._account_loss_eval_interval_seconds = max(
            0.2,
            float(os.getenv("RISK_ACCOUNT_LOSS_EVAL_INTERVAL_SECONDS", "1.0")),
        )
        self._state_persist_min_interval_seconds = max(
            1.0,
            float(os.getenv("RISK_STATE_PERSIST_MIN_INTERVAL_SECONDS", "5.0")),
        )
        self._last_account_loss_eval_mono = 0.0
        self._last_state_persist_mono = 0.0
        self._maybe_migrate_legacy_state()
        trade_mode = str(os.getenv("TRADE_MODE", "") or "").strip().upper()
        if trade_mode == "LIVE":
            try:
                self.state_path.resolve().relative_to(APP_DIR)
                logger.warning(
                    "startup.risk_state_path_warning: RISK_STATE_PATH=%s resolves inside "
                    "the app/ package directory — this file will not persist across "
                    "container image rebuilds. Set RISK_STATE_PATH to a mounted volume path.",
                    self.state_path,
                )
            except ValueError:
                pass
        self._load_state()
        if self._reset_daily_if_new_day(datetime.now(timezone.utc)):
            self._persist_state()
        self._publish_account_loss_guard(source="startup")
        self.restored_positions: Dict[str, Dict[str, Any]] = dict(self.open_positions)
        # Seed executed-token tracker from any restored open positions
        try:
            executed_tokens_tracker.bootstrap_from_positions(
                self.open_positions,
                self.instrument_meta,
            )
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker.bootstrap_from_positions failed: %s", exc
            )

    @staticmethod
    def _resolve_tick_size(meta: Dict[str, Any]) -> Optional[float]:
        for key in (
            "tick_size",
            "tickSize",
            "ticksize",
            "price_step",
            "priceStep",
            "pricestep",
            "min_tick",
        ):
            raw_value = meta.get(key)
            try:
                tick_size = float(raw_value)
            except (TypeError, ValueError):
                continue
            if tick_size <= 0:
                continue
            if tick_size >= 1.0:
                tick_size /= 100.0
            return tick_size
        return None

    @staticmethod
    def _normalize_strategy_ratio(value: Any) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return numeric
        return numeric / 100.0 if numeric >= 1.0 else numeric

    @staticmethod
    def _normalize_env_percent(value: Any) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric <= 0:
            return numeric
        return numeric / 100.0

    @staticmethod
    def _snap_order_price(
        price: float,
        *,
        tick_size: Optional[float],
        side: str,
    ) -> float:
        if not tick_size or tick_size <= 0:
            return float(price)
        units = float(price) / float(tick_size)
        side_upper = str(side or "").upper()
        if side_upper == "BUY":
            snapped = math.floor(units + 1e-9) * float(tick_size)
        elif side_upper == "SELL":
            snapped = math.ceil(units - 1e-9) * float(tick_size)
        else:
            snapped = round(units) * float(tick_size)
        if snapped <= 0:
            snapped = float(tick_size)
        precision_text = f"{float(tick_size):.8f}".rstrip("0").rstrip(".")
        if "." in precision_text:
            precision = max(2, len(precision_text.split(".", 1)[1]))
        else:
            precision = 0
        return round(snapped, precision)

    @staticmethod
    def _parse_strategy_flag(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return None

    @classmethod
    def _broker_brackets_enabled(
        cls,
        strategy_context: Optional[Dict[str, Any]],
    ) -> bool:
        if not strategy_context:
            return True
        managed_exit_mode = str(
            strategy_context.get("managed_exit_mode") or ""
        ).strip().lower()
        if managed_exit_mode == "strategy":
            return False
        explicit = cls._parse_strategy_flag(
            strategy_context.get("broker_brackets_enabled")
        )
        if explicit is not None:
            return explicit
        return True

    @staticmethod
    def _is_invalid_slm_ordertype_error(value: Any) -> bool:
        text = str(value or "").lower()
        return "invalid value for field : ordertype" in text or (
            "vnd1001" in text and "ordertype" in text
        )

    def _fallback_stoplimit_price(
        self,
        *,
        trigger_price: float,
        exit_side: OrderSide,
        tick_size: Optional[float],
    ) -> float:
        tick = float(tick_size) if tick_size not in (None, 0, 0.0) else 0.0
        raw_price = float(trigger_price)
        if tick > 0:
            if exit_side == OrderSide.BUY:
                raw_price = float(trigger_price) + tick
            else:
                raw_price = float(trigger_price) - tick
        if raw_price <= 0:
            raw_price = float(trigger_price)
        if raw_price <= 0 and tick > 0:
            raw_price = tick
        return self._snap_order_price(
            raw_price,
            tick_size=tick_size,
            side=exit_side.value,
        )

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            if value is None:
                return 0
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    def _publish_account_loss_guard(
        self,
        *,
        source: str,
        unrealized_pnl: Optional[float] = None,
        total_pnl: Optional[float] = None,
    ) -> None:
        with self._state_lock:
            realized_pnl = self.realized_pnl
            daily_realized_pnl = self.daily_realized_pnl
            resolved_unrealized = (
                self.last_unrealized_pnl
                if unrealized_pnl is None
                else float(unrealized_pnl)
            )
            resolved_total = (
                self.last_total_pnl if total_pnl is None else float(total_pnl)
            )
            max_daily_loss = self.max_daily_loss
            max_intraday_drawdown = self.max_intraday_drawdown
            kill_switch_activated = self.kill_switch_activated
        account_loss_guard.update_snapshot(
            tenant_id=self.tenant_id,
            broker_account_id=self.broker_account_id,
            realized_pnl=realized_pnl,
            daily_realized_pnl=daily_realized_pnl,
            unrealized_pnl=resolved_unrealized,
            daily_total_pnl=resolved_total,
            max_daily_loss=max_daily_loss,
            max_intraday_drawdown=max_intraday_drawdown,
            kill_switch_activated=kill_switch_activated,
            source=source,
        )

    @staticmethod
    def _is_recovery_owned_entry(
        *,
        template_name: Optional[str],
        strategy_name: Optional[str],
        reason: Optional[str],
    ) -> bool:
        markers = {
            str(template_name or "").strip().upper(),
            str(strategy_name or "").strip().upper(),
            str(reason or "").strip().upper(),
        }
        recovery_markers = {
            "BROKER_SYNC",
            "POSITION_SYNC",
            "RECOVERY",
            "MANUAL_RECOVERY",
            "UNKNOWN",
            "__UNKNOWN__",
        }
        return bool(markers & recovery_markers)

    def _prune_forced_exit_suppressions_locked(self) -> None:
        now = time.monotonic()
        expired = [
            label
            for label, until_ts in self._forced_exit_suppression_until.items()
            if float(until_ts) <= now
        ]
        for label in expired:
            self._forced_exit_suppression_until.pop(label, None)

    def _mark_forced_exit_suppression(self, label: str) -> None:
        target_label = str(label or "").strip()
        if not target_label:
            return
        with self._state_lock:
            self._prune_forced_exit_suppressions_locked()
            self._forced_exit_suppression_until[target_label] = (
                time.monotonic() + self._forced_exit_suppression_seconds
            )

    def _clear_forced_exit_suppression(self, label: str) -> None:
        target_label = str(label or "").strip()
        if not target_label:
            return
        with self._state_lock:
            self._forced_exit_suppression_until.pop(target_label, None)

    def _has_forced_exit_suppression(self, label: str) -> bool:
        target_label = str(label or "").strip()
        if not target_label:
            return False
        with self._state_lock:
            self._prune_forced_exit_suppressions_locked()
            until_ts = self._forced_exit_suppression_until.get(target_label)
            return bool(until_ts and float(until_ts) > time.monotonic())

    def should_suppress_broker_sync_entry(self, label: str) -> bool:
        with self._state_lock:
            kill_switch_active = bool(self.kill_switch_activated)
        return kill_switch_active or self._has_forced_exit_suppression(label)

    @staticmethod
    def _truncate_log_value(value: Any, limit: int = 512) -> Optional[str]:
        text = " ".join(str(value or "").split())
        if not text:
            return None
        if len(text) <= limit:
            return text
        return f"{text[: max(1, limit - 3)]}..."

    def _stoploss_skip_reason(
        self,
        *,
        label: str,
        require_position: bool,
    ) -> Optional[str]:
        with self._state_lock:
            position_exists = label in self.open_positions
            kill_switch_active = bool(self.kill_switch_activated)
        if kill_switch_active:
            return "kill_switch_active"
        if self._has_forced_exit_suppression(label):
            return "forced_exit_in_flight"
        if require_position and not position_exists:
            return "position_already_closed"
        return None

    def _log_stoploss_order_failure(
        self,
        *,
        label: str,
        strategy_name: Optional[str],
        stoploss_price: Optional[float],
        detail: Any,
        fallback_mode: Optional[str] = None,
    ) -> None:
        broker_status = None
        broker_url = None
        broker_content_type = None
        broker_body = None
        if isinstance(detail, (AngelHTTPBlockedError, AngelHTTPResponseError)):
            broker_status = getattr(detail, "status", None)
            broker_url = getattr(detail, "url", None)
            broker_content_type = getattr(detail, "content_type", None)
            broker_body = getattr(detail, "body_head", None)
        elif isinstance(detail, dict):
            broker_status = detail.get("terminal_status") or detail.get("status")
            broker_body = (
                detail.get("place_order")
                or detail.get("order_row")
                or detail
            )

        extras: Dict[str, Any] = {
            "label": label,
            "strategy_name": strategy_name or "UNKNOWN",
            "stoploss_price": stoploss_price,
            "fallback_mode": fallback_mode or "none",
            "error": self._truncate_log_value(detail, 1024),
        }
        truncated_body = self._truncate_log_value(broker_body, 1024)
        if broker_status is not None:
            extras["broker_status"] = broker_status
        if broker_url:
            extras["broker_url"] = broker_url
        if broker_content_type:
            extras["broker_content_type"] = broker_content_type
        if truncated_body:
            extras["broker_body"] = truncated_body
        log_event(
            logger,
            event_type="BRACKET_STOPLOSS_ORDER_ERROR",
            message="Stoploss order placement failed.",
            level=logging.WARNING,
            **extras,
        )

    @classmethod
    def _pick_nonzero_price(cls, *values: Any) -> Optional[float]:
        for value in values:
            price = cls._coerce_float(value)
            if price is None or price == 0.0:
                continue
            return price
        return None

    def _normalize_broker_order_status(self, value: Any) -> str:
        normalizer = getattr(self.order_client, "_normalize_order_status", None)
        if callable(normalizer):
            try:
                normalized = normalizer(value)
            except Exception:
                normalized = None
            if normalized:
                return str(normalized)
        raw = " ".join(str(value or "").strip().split()).lower()
        if not raw:
            return "UNKNOWN"
        if raw in ("complete", "completed", "filled", "fill"):
            return "COMPLETE"
        if raw in ("rejected", "reject"):
            return "REJECTED"
        if raw in ("cancelled", "canceled", "cancel"):
            return "CANCELLED"
        if raw in ("expired",):
            return "EXPIRED"
        if "trigger" in raw and "pending" in raw:
            return "PENDING"
        if raw in ("pending", "open", "put order req received", "validation pending"):
            return "PENDING"
        return raw.upper()

    def _pending_order_lot_size(self, ctx: Dict[str, Any]) -> float:
        label = str(ctx.get("label") or "")
        meta = self.instrument_meta.get(label, {})
        try:
            lot_size = float(ctx.get("lot_size") or meta.get("lot_size") or 1.0)
        except (TypeError, ValueError):
            lot_size = 1.0
        return lot_size if lot_size > 0 else 1.0

    def _pending_order_qty_from_row(
        self,
        *,
        ctx: Dict[str, Any],
        row: Dict[str, Any],
    ) -> int:
        qty = int(ctx.get("qty") or 0)
        filled_quantity = self._coerce_int(
            row.get("filledshares")
            or row.get("filled_qty")
            or row.get("filled_quantity")
            or row.get("filledQuantity")
        )
        if filled_quantity <= 0:
            return qty
        lot_size = self._pending_order_lot_size(ctx)
        normalized_qty = int(round(float(filled_quantity) / lot_size))
        return normalized_qty if normalized_qty > 0 else qty

    def _persist_execution_record_from_ctx(
        self,
        *,
        ctx: Dict[str, Any],
        qty: int,
        price: float,
        broker_order_id: str,
    ) -> None:
        if not self.trade_persister:
            return
        try:
            exec_ts = datetime.now(timezone.utc).isoformat()
            exec_record = {
                "timestamp": exec_ts,
                "label": ctx.get("label"),
                "underlying": ctx.get("underlying"),
                "strategy_name": ctx.get("strategy_name"),
                "template_name": ctx.get("template_name"),
                "reason": ctx.get("reason") or "pending_resolved",
                "side": ctx.get("side"),
                "qty": qty,
                "price": price,
                "exchange": ctx.get("exchange"),
                "symboltoken": ctx.get("symboltoken"),
                "tradingsymbol": ctx.get("tradingsymbol"),
                "trade_mode": self.current_trade_mode,
                "product_type": ctx.get("product_type"),
                "tag": ctx.get("tag"),
                "broker_order_id": broker_order_id,
            }
            exec_record["trade_id"] = "|".join(
                [
                    str(
                        ctx.get("symboltoken")
                        or ctx.get("tradingsymbol")
                        or ctx.get("label")
                    ),
                    str(ctx.get("side") or ""),
                    str(qty),
                    exec_ts,
                ]
            )
            self.trade_persister.record_execution(exec_record)
        except Exception as exc:
            logger.warning("Pending order execution persist failed: %s", exc)

    def _load_pending_order_row(
        self,
        order_id: str,
        *,
        order_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        details_row: Optional[Dict[str, Any]] = None
        try:
            details = self.order_client.get_order_details(str(order_id))
            if isinstance(details, dict):
                payload = details.get("data")
                if isinstance(payload, dict):
                    details_row = payload
        except Exception as exc:
            logger.debug(
                "Pending order reconcile: get_order_details failed order_id=%s err=%s",
                order_id,
                exc,
            )
        if details_row:
            return details_row
        if order_index is not None:
            return order_index.get(str(order_id))
        try:
            book = self.order_client.get_order_book()
        except Exception as exc:
            logger.debug(
                "Pending order reconcile: get_order_book failed order_id=%s err=%s",
                order_id,
                exc,
            )
            return None
        rows = book.get("data") if isinstance(book, dict) else None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_order_id = row.get("orderid") or row.get("order_id") or row.get("orderId")
            if str(row_order_id or "") == str(order_id):
                return row
        return None

    def _build_closing_pending_order_ctx(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: Optional[float],
        trigger_price: Optional[float],
        exchange: str,
        symboltoken: Any,
        tradingsymbol: str,
        tag: Optional[str],
        product_type: str,
    ) -> Dict[str, Any]:
        with self._state_lock:
            entry = dict(self.open_positions.get(label, {}))
        meta = self.instrument_meta.get(label, {})
        lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1.0)
        return {
            "label": label,
            "side": side,
            "qty": int(qty),
            "price": float(price or 0.0),
            "trigger_price": float(trigger_price)
            if trigger_price not in (None, 0, 0.0)
            else None,
            "template_name": entry.get("template_name"),
            "role": entry.get("role"),
            "underlying": entry.get("underlying") or self._infer_underlying(label),
            "strategy_name": entry.get("strategy_name"),
            "reason": entry.get("reason") or tag,
            "ml_info": entry.get("ml_info"),
            "count_for_spreads": False,
            "is_closing": True,
            "exchange": exchange,
            "symboltoken": symboltoken,
            "tradingsymbol": tradingsymbol,
            "tag": tag,
            "product_type": product_type,
            "lot_size": lot_size if lot_size > 0 else 1.0,
        }

    def _handle_confirmed_closing_order(
        self,
        *,
        response: Dict[str, Any],
        label: str,
        side: str,
        qty: int,
        price: Optional[float],
        trigger_price: Optional[float],
        exchange: str,
        symboltoken: Any,
        tradingsymbol: str,
        tag: Optional[str],
        product_type: str,
    ) -> Dict[str, Any]:
        terminal = str(response.get("terminal_status") or "UNKNOWN")
        order_id = str(response.get("order_id") or "")
        order_row = response.get("order_row") if isinstance(response, dict) else None
        ctx = self._build_closing_pending_order_ctx(
            label=label,
            side=side,
            qty=qty,
            price=price,
            trigger_price=trigger_price,
            exchange=exchange,
            symboltoken=symboltoken,
            tradingsymbol=tradingsymbol,
            tag=tag,
            product_type=product_type,
        )
        fill_price = float(
            self._pick_nonzero_price(
                order_row.get("averageprice") if isinstance(order_row, dict) else None,
                order_row.get("average_price") if isinstance(order_row, dict) else None,
                order_row.get("avgprice") if isinstance(order_row, dict) else None,
                order_row.get("averagePrice") if isinstance(order_row, dict) else None,
                order_row.get("tradedprice") if isinstance(order_row, dict) else None,
                order_row.get("traded_price") if isinstance(order_row, dict) else None,
                order_row.get("fillprice") if isinstance(order_row, dict) else None,
                order_row.get("filledprice") if isinstance(order_row, dict) else None,
                order_row.get("price") if isinstance(order_row, dict) else None,
                price,
                trigger_price,
                0.0,
            )
            or 0.0
        )
        if terminal == "COMPLETE":
            self._persist_execution_record_from_ctx(
                ctx=ctx,
                qty=int(qty),
                price=fill_price,
                broker_order_id=order_id,
            )
            self._register_exit(label, fill_price, int(qty))
            return {"terminal": terminal, "closed": True, "order_id": order_id}
        if terminal in {"PENDING", "UNKNOWN"} and order_id:
            self._record_pending_order(order_id=order_id, ctx=ctx)
        return {"terminal": terminal, "closed": False, "order_id": order_id}

    # Attach a maintenance manager for heartbeat and kill-switch checks.
    def attach_maintenance(self, maintenance: MaintenanceManager) -> None:
        self.maintenance = maintenance

    # Register an optional hub-routed submitter for forced/safety exits.
    def set_hub_exit_submitter(
        self, submitter: Optional[Callable[..., bool]]
    ) -> None:
        self._hub_exit_submitter = submitter

    def set_operating_mode(self, mode: str) -> None:
        """Set the resolved operating mode string for authority-path enforcement."""
        self._operating_mode = str(mode or "")

    def _assert_direct_submit_allowed(
        self, *, trade_mode: str, exit_source: str, label: str
    ) -> None:
        """Block direct broker submit fallback in hub-authoritative LIVE.

        Raises P0RuleViolation if the operating mode is HUB_AUTHORITATIVE
        and trade_mode is LIVE, since only the hub should place orders.
        """
        if str(trade_mode or "").upper() != "LIVE":
            return
        if not self._operating_mode:
            return
        assert_legacy_exit_allowed(
            operating_mode=self._operating_mode,
            is_break_glass=False,
            exit_source=exit_source,
            scope_key=label,
        )

    # Attempt to submit an EXIT order through the optional hub exit hook.
    def _submit_exit_via_hub(
        self,
        *,
        label: str,
        strategy_name: Optional[str],
        exchange: str,
        symboltoken: str,
        tradingsymbol: str,
        side: str,
        quantity: int,
        trade_mode: str,
        tag: Optional[str],
        reason: Optional[str],
    ) -> bool:
        submitter = self._hub_exit_submitter
        if submitter is None:
            return False
        if str(trade_mode or "").upper() != "LIVE":
            return False
        if quantity <= 0:
            return False
        try:
            return bool(
                submitter(
                    label=label,
                    strategy_name=strategy_name,
                    exchange=exchange,
                    symboltoken=str(symboltoken or ""),
                    tradingsymbol=tradingsymbol,
                    side=str(side or "").upper(),
                    quantity=int(quantity),
                    trade_mode=str(trade_mode or ""),
                    tag=tag,
                    reason=reason,
                )
            )
        except Exception as exc:
            logger.warning(
                "Hub exit submitter failed for %s (%s): %s",
                label,
                strategy_name or "unknown",
                exc,
            )
            return False

    def _maybe_migrate_legacy_state(self) -> None:
        """One-time migration: move legacy app/config/risk_positions.json to canonical path."""
        if self.state_path == _LEGACY_STATE_PATH:
            return
        if self.state_path.exists():
            return
        if not _LEGACY_STATE_PATH.exists():
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            _LEGACY_STATE_PATH.rename(self.state_path)
            logger.warning(
                "startup.risk_state_migrated: moved legacy risk state from %s to %s",
                _LEGACY_STATE_PATH,
                self.state_path,
            )
        except Exception as exc:
            logger.warning(
                "startup.risk_state_migration_failed: could not move %s to %s: %s",
                _LEGACY_STATE_PATH,
                self.state_path,
                exc,
            )

    # Load persisted risk state from disk if available.
    def _load_state(self) -> None:
        candidate_path = self.state_path
        if not candidate_path.exists():
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            return
        if not candidate_path.exists():
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            return
        try:
            data = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data is None:
            bak_path = candidate_path.with_suffix(candidate_path.suffix + ".bak")
            try:
                if bak_path.exists():
                    data = json.loads(bak_path.read_text(encoding="utf-8"))
                    logger.warning("Loaded risk state from backup: %s", bak_path)
            except Exception:
                return
        if data is None:
            return
        with self._state_lock:
            self.realized_pnl = float(data.get("realized_pnl", 0.0))
            self.max_equity = float(data.get("max_equity", self.realized_pnl))
            self.daily_realized_pnl = float(data.get("daily_realized_pnl", 0.0))
            self.daily_peak_equity = float(
                data.get("daily_peak_equity", self.daily_realized_pnl)
            )
            self.daily_peak_total_pnl = float(
                data.get(
                    "daily_peak_total_pnl",
                    data.get("daily_peak_equity", self.daily_realized_pnl),
                )
            )
            self.last_unrealized_pnl = float(data.get("last_unrealized_pnl", 0.0))
            self.last_total_pnl = float(
                data.get(
                    "last_total_pnl",
                    self.daily_realized_pnl + self.last_unrealized_pnl,
                )
            )
            ks_date = data.get("kill_switch_date")
            try:
                self.kill_switch_date = (
                    datetime.fromisoformat(ks_date).date() if ks_date else None
                )
            except Exception:
                self.kill_switch_date = None
            self.kill_switch_activated = bool(data.get("kill_switch_activated", False))
            for underlying, spreads in data.get("open_spreads", {}).items():
                for template, labels in spreads.items():
                    self.open_spreads_by_underlying.setdefault(underlying, {})[
                        template
                    ] = set(labels)
            self.open_positions = data.get("open_positions", {})

    # Persist current risk state to disk.
    def _persist_state(self, *, force: bool = True) -> None:
        now_mono = time.monotonic()
        if (
            not force
            and (now_mono - self._last_state_persist_mono)
            < self._state_persist_min_interval_seconds
        ):
            return
        with self._state_lock:
            payload = {
                "realized_pnl": self.realized_pnl,
                "open_spreads": {
                    underlying: {tpl: list(labels) for tpl, labels in templates.items()}
                    for underlying, templates in self.open_spreads_by_underlying.items()
                },
                "open_positions": dict(self.open_positions),
                "max_equity": self.max_equity,
                "daily_realized_pnl": self.daily_realized_pnl,
                "daily_peak_equity": self.daily_peak_equity,
                "daily_peak_total_pnl": self.daily_peak_total_pnl,
                "last_unrealized_pnl": self.last_unrealized_pnl,
                "last_total_pnl": self.last_total_pnl,
                "kill_switch_activated": self.kill_switch_activated,
                "kill_switch_date": (
                    self.kill_switch_date.isoformat() if self.kill_switch_date else None
                ),
            }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            bak_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")
            serialized = json.dumps(payload, indent=2)
            if self.state_path.exists():
                try:
                    bak_path.write_text(
                        self.state_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            self.state_path.write_text(serialized, encoding="utf-8")
            self._last_state_persist_mono = now_mono
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to persist risk state: %s", exc)

    # Infer underlying name from instrument metadata.
    def _infer_underlying(self, label: str) -> Optional[str]:
        meta = self.instrument_meta.get(label)
        if not meta:
            return None
        return meta.get("underlying")

    # Resolve an underlying label for the given instrument label.
    def _underlying_label_for_label(self, label: str) -> str:
        meta = self.instrument_meta.get(label, {})
        kind = meta.get("kind")
        if kind == "UNDERLYING":
            return label
        underlying_name = str(meta.get("underlying") or "")
        if underlying_name and underlying_name in self._underlying_name_to_label:
            return self._underlying_name_to_label[underlying_name]
        # Build mapping lazily
        for m_label, m_meta in self.instrument_meta.items():
            if m_meta.get("kind") == "UNDERLYING":
                name = str(m_meta.get("underlying") or "")
                if name:
                    self._underlying_name_to_label[name] = m_label
        return self._underlying_name_to_label.get(underlying_name, label)

    # Reset daily counters when a new trading day starts.
    def _reset_daily_if_new_day(self, now: datetime) -> bool:
        now_date = now.astimezone(IST).date()
        if self.kill_switch_date == now_date:
            return False
        # New day: reset daily counters and kill switch
        with self._state_lock:
            self._forced_exit_suppression_until.clear()
        self.kill_switch_date = now_date
        self.realized_pnl = 0.0
        self.max_equity = 0.0
        self.daily_realized_pnl = 0.0
        self.daily_peak_equity = 0.0
        self.daily_peak_total_pnl = 0.0
        self.last_unrealized_pnl = 0.0
        self.last_total_pnl = 0.0
        self.kill_switch_activated = False
        self._publish_account_loss_guard(source="new_day_reset")
        return True

    def _compute_open_unrealized_pnl(self) -> float:
        with self._state_lock:
            positions_snapshot = {
                str(label): dict(entry)
                for label, entry in self.open_positions.items()
                if isinstance(entry, dict)
            }
        unrealized_total = 0.0
        for label, entry in positions_snapshot.items():
            qty = float(entry.get("qty") or 0.0)
            if qty <= 0:
                continue
            side = str(entry.get("side") or "SELL").strip().upper()
            entry_price = float(entry.get("entry_price") or 0.0)
            meta = self.instrument_meta.get(label, {})
            lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1.0)
            last_price = dashboard_bus.get_last_price(label)
            if last_price is None:
                # Try instrument symbol/token lookup as fallback
                tradingsymbol = entry.get("tradingsymbol") or entry.get("symbol")
                symboltoken = entry.get("symboltoken") or entry.get("symbol_token")
                if tradingsymbol or symboltoken:
                    _fn = getattr(dashboard_bus, "get_last_price_for_instrument", None)
                    if callable(_fn):
                        last_price = _fn(symbol=tradingsymbol, token=symboltoken)
            if last_price is None:
                last_price = float(entry.get("avg_price", 0.0) or 0.0)
                if last_price > 0:
                    logger.warning(
                        "[RISK] kill_switch_mark_stale: label=%s using broker avg_price=%s "
                        "— stream mark unavailable. Drawdown may be inaccurate.",
                        label, last_price,
                    )
            if last_price is None:
                last_price = entry_price
            side_mult = 1.0 if side == "BUY" else -1.0
            unrealized_total += (
                (float(last_price) - entry_price) * side_mult * qty * lot_size
            )
        return float(unrealized_total)

    def evaluate_account_loss(
        self,
        *,
        now: Optional[datetime] = None,
        source: str = "runtime",
        force: bool = False,
    ) -> Dict[str, Any]:
        now_ts = now or datetime.now(timezone.utc)
        self._reset_daily_if_new_day(now_ts)
        now_mono = time.monotonic()
        if (
            not force
            and (now_mono - self._last_account_loss_eval_mono)
            < self._account_loss_eval_interval_seconds
        ):
            with self._state_lock:
                return {
                    "kill_switch_activated": bool(self.kill_switch_activated),
                    "daily_realized_pnl": float(self.daily_realized_pnl),
                    "unrealized_pnl": float(self.last_unrealized_pnl),
                    "daily_total_pnl": float(self.last_total_pnl),
                    "daily_peak_equity": float(self.daily_peak_equity),
                    "daily_peak_total_pnl": float(self.daily_peak_total_pnl),
                }
        self._last_account_loss_eval_mono = now_mono
        unrealized_pnl = self._compute_open_unrealized_pnl()
        with self._state_lock:
            prior_realized_peak = float(self.daily_peak_equity)
            prior_total_peak = float(self.daily_peak_total_pnl)
            prior_unrealized = float(self.last_unrealized_pnl)
            prior_total = float(self.last_total_pnl)

            realized_pnl = float(self.daily_realized_pnl)
            total_pnl = float(realized_pnl + unrealized_pnl)
            self.last_unrealized_pnl = float(unrealized_pnl)
            self.last_total_pnl = float(total_pnl)
            self.daily_peak_equity = max(float(self.daily_peak_equity), realized_pnl)
            self.daily_peak_total_pnl = max(
                float(self.daily_peak_total_pnl),
                realized_pnl,
                total_pnl,
            )
            realized_peak = float(self.daily_peak_equity)
            total_peak = float(self.daily_peak_total_pnl)

            realized_drawdown = max(0.0, realized_peak - realized_pnl)
            total_drawdown = max(0.0, total_peak - total_pnl)

            loss_limit = abs(float(self.max_daily_loss or 0.0))
            drawdown_limit = abs(float(self.max_intraday_drawdown or 0.0))
            realized_loss_hit = loss_limit > 0.0 and realized_pnl <= -loss_limit
            floating_loss_hit = (
                self._include_unrealized_in_kill_switch
                and loss_limit > 0.0
                and total_pnl <= -loss_limit
            )
            realized_drawdown_hit = (
                drawdown_limit > 0.0 and realized_drawdown >= drawdown_limit
            )
            floating_drawdown_hit = (
                self._include_unrealized_in_kill_switch
                and drawdown_limit > 0.0
                and total_drawdown >= drawdown_limit
            )
            should_activate = not self.kill_switch_activated and (
                realized_loss_hit
                or floating_loss_hit
                or realized_drawdown_hit
                or floating_drawdown_hit
            )
            if should_activate:
                self.kill_switch_activated = True
                self.kill_switch_date = now_ts.astimezone(IST).date()
            has_open_positions = bool(self.open_positions)
            open_labels = list(self.open_positions.keys()) if has_open_positions else []

        state_changed = (
            abs(prior_realized_peak - realized_peak) > 0.0001
            or abs(prior_total_peak - total_peak) > 0.0001
            or abs(prior_unrealized - unrealized_pnl) > 0.0001
            or abs(prior_total - total_pnl) > 0.0001
        )
        self._publish_account_loss_guard(
            source=source,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
        )
        if should_activate:
            ltp_lookup = {
                label: float(price)
                for label in open_labels
                for price in [dashboard_bus.get_last_price(label)]
                if price is not None
            }
            hit_reasons = []
            if realized_loss_hit:
                hit_reasons.append("realized_loss")
            if floating_loss_hit:
                hit_reasons.append("floating_loss")
            if realized_drawdown_hit:
                hit_reasons.append("realized_drawdown")
            if floating_drawdown_hit:
                hit_reasons.append("floating_drawdown")
            logger.error(
                "[RISK] Kill-switch activated: realized=%.2f unrealized=%.2f total=%.2f realized_dd=%.2f total_dd=%.2f (loss_lim=%.2f, dd_lim=%.2f) source=%s reasons=%s",
                realized_pnl,
                unrealized_pnl,
                total_pnl,
                realized_drawdown,
                total_drawdown,
                self.max_daily_loss,
                self.max_intraday_drawdown,
                source,
                ",".join(hit_reasons) or "unknown",
            )
            if self.kill_switch_square_off_open_positions and has_open_positions:
                try:
                    self.square_off_all(
                        trade_mode=self.current_trade_mode,
                        ltp_lookup=ltp_lookup or None,
                        reason="KILL_SWITCH_ACCOUNT_LOSS",
                        tag="KILL_SWITCH_EXIT",
                    )
                except Exception as exc:
                    logger.error("Kill-switch square-off failed: %s", exc)
            self._persist_state()
        elif state_changed:
            self._persist_state(force=False)
        return {
            "kill_switch_activated": bool(self.kill_switch_activated),
            "daily_realized_pnl": float(realized_pnl),
            "unrealized_pnl": float(unrealized_pnl),
            "daily_total_pnl": float(total_pnl),
            "daily_peak_equity": float(realized_peak),
            "daily_peak_total_pnl": float(total_peak),
            "realized_drawdown": float(realized_drawdown),
            "total_drawdown": float(total_drawdown),
        }

    # Get risk limit settings for a specific underlying.
    def _get_limits_for_underlying(
        self, underlying: Optional[str]
    ) -> Dict[str, Optional[float]]:
        limits: Dict[str, Optional[float]] = {
            "max_lots_per_trade": None,
            "max_open_lots": None,
            "max_notional_per_underlying": None,
        }
        cfg = self.risk_limits or {}
        default_cfg = cfg.get("default", {}) if isinstance(cfg, dict) else {}
        per_cfg = (cfg.get("per_underlying", {}) or {}) if isinstance(cfg, dict) else {}
        if isinstance(default_cfg, dict):
            limits.update({k: default_cfg.get(k) for k in limits})
        key = (underlying or "").upper()
        if (
            key
            and isinstance(per_cfg, dict)
            and key in per_cfg
            and isinstance(per_cfg[key], dict)
        ):
            for k in limits:
                if per_cfg[key].get(k) is not None:
                    limits[k] = per_cfg[key][k]
        return limits

    # Compute total open lots for a given underlying.
    def _get_current_open_lots(self, underlying: Optional[str]) -> float:
        if not underlying:
            return 0.0
        total_lots = 0.0
        with self._state_lock:
            for label, entry in self.open_positions.items():
                entry_under = (entry.get("underlying") or "").upper()
                if not entry_under:
                    meta = self.instrument_meta.get(label, {})
                    entry_under = str(meta.get("underlying") or "").upper()
                if not entry_under and label.upper().startswith((underlying or "").upper()):
                    entry_under = (underlying or "").upper()
                if entry_under != underlying.upper():
                    continue
                qty = abs(float(entry.get("qty", 0)))
                total_lots += qty
        return total_lots

    # Build and log a risk decision.
    def _make_decision(
        self,
        *,
        allowed: bool,
        reason: str,
        label: str,
        side: str,
        qty: int,
        strategy_name: Optional[str],
        template_name: Optional[str],
    ) -> RiskDecision:
        status = "ACCEPTED" if allowed else "REJECTED"
        log_event(
            logger,
            event_type="LEGACY_RISK_DECISION",
            message=reason,
            level=logging.INFO if allowed else logging.WARNING,
            label=label,
            side=side,
            qty=qty,
            strategy_name=strategy_name,
            template_name=template_name,
            allowed=allowed,
            status=status,
        )
        return RiskDecision(allowed=allowed, status=status, reason=reason)

    # Check whether a new spread can be opened for an underlying.
    def _can_enter(
        self, underlying: Optional[str], template_name: Optional[str]
    ) -> bool:
        if not underlying:
            return True
        # Select per-underlying override if configured.
        max_allowed = self.per_underlying_max_open_spreads.get(
            (underlying or "").upper(), self.max_open_spreads
        )
        if max_allowed and max_allowed > 0:
            with self._state_lock:
                open_spreads = dict(self.open_spreads_by_underlying.get(underlying, {}))
            # Allow adding legs to an existing spread
            if template_name in open_spreads:
                return True
            current = len(open_spreads)
            allowed = current < max_allowed
            if not allowed:
                logger.warning(
                    "RiskManager blocking spread for %s (max %d reached)",
                    underlying,
                    max_allowed,
                )
            return allowed
        return True

    # Record a new position entry and update trackers.
    def _register_entry(
        self,
        label: str,
        side: str,
        qty: int,
        price: float,
        template_name: str,
        role: Optional[str],
        underlying: Optional[str],
        strategy_name: Optional[str] = None,
        reason: Optional[str] = None,
        ml_info: Optional[Dict[str, Any]] = None,
        count_for_spreads: bool = True,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        template_key = template_name or "__UNKNOWN__"
        underlying = underlying or self._infer_underlying(label)
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange", "NSE")
        symboltoken = meta.get("token")
        tradingsymbol = meta.get("symbol", label)
        lot_size = float(meta.get("lot_size") or 1)
        with self._state_lock:
            if count_for_spreads:
                self.open_spreads_by_underlying.setdefault(
                    underlying or "__UNKNOWN__", {}
                ).setdefault(template_key, set()).add(label)
            self.open_positions[label] = {
                "side": side,
                "qty": qty,
                "entry_price": price,
                "template_name": template_key,
                "role": role,
                "underlying": underlying,
                "strategy_name": strategy_name,
                "reason": reason,
                "entry_ts": datetime.now(timezone.utc).isoformat(),
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "lot_size": lot_size,
            }
            if ml_info:
                self.open_positions[label]["ml_info"] = ml_info
            if strategy_context:
                self.open_positions[label]["strategy_context"] = dict(strategy_context)
            entry_ts = self.open_positions[label]["entry_ts"]
        # Ensure the executed token stays subscribed for live LTP updates
        try:
            executed_tokens_tracker.register_position(label, meta)
        except Exception as exc:  # safety net, do not break trading on tracker errors
            logger.warning(
                "ExecutedTokensTracker.register_position failed for %s: %s",
                label,
                exc,
            )
        dashboard_bus.add_position(
            label,
            side=side,
            qty=qty,
            entry_price=price,
            lot_size=lot_size,
            template_name=template_name,
            role=role,
            underlying=underlying,
            strategy_name=strategy_name,
            reason=reason,
            entry_ts=entry_ts,
        )
        logger.info(
            "[PNL_ENTRY] position_added | label=%s side=%s qty=%d entry_price=%.2f template=%s underlying=%s",
            label,
            side,
            qty,
            price,
            template_name,
            underlying,
        )

        if self._is_recovery_owned_entry(
            template_name=template_name,
            strategy_name=strategy_name,
            reason=reason,
        ):
            logger.info(
                "[BRACKET] Skipping broker bracket orders for %s due to recovery-owned position [%s]",
                label,
                strategy_name or template_name or "UNKNOWN",
            )
            return

        if not self._broker_brackets_enabled(strategy_context):
            logger.info(
                "[BRACKET] Skipping broker bracket orders for %s due to strategy-managed exit ownership [%s]",
                label,
                strategy_name or "UNKNOWN",
            )
            return

        # Automatically place target and stoploss orders after entry fills
        # Use strategy-specific exit parameters if available
        try:
            self._place_bracket_orders_async(
                label=label,
                side=side,
                qty=qty,
                entry_price=price,
                strategy_name=strategy_name,
                strategy_context=strategy_context,
            )
        except Exception as exc:
            logger.warning(
                "Failed to place bracket orders for %s: %s",
                label,
                exc,
            )

    # Apply broker position updates with lock protection.
    def update_position_from_broker(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        entry_price: float,
        exchange: Optional[str] = None,
        symboltoken: Optional[str] = None,
        tradingsymbol: Optional[str] = None,
        template_name: Optional[str] = None,
        strategy_name: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        meta = self.instrument_meta.get(label, {})
        with self._state_lock:
            existing = dict(self.open_positions.get(label, {}))
            existing["side"] = side
            existing["qty"] = qty
            existing["entry_price"] = entry_price
            if template_name is not None and str(template_name).strip():
                existing["template_name"] = str(template_name).strip()
            if exchange is not None:
                existing["exchange"] = exchange
            if symboltoken is not None:
                existing["symboltoken"] = symboltoken
            if tradingsymbol is not None:
                existing["tradingsymbol"] = tradingsymbol
            if strategy_name is not None and str(strategy_name).strip():
                existing["strategy_name"] = str(strategy_name).strip()
            if strategy_context:
                existing["strategy_context"] = dict(strategy_context)
            if existing.get("lot_size") in (None, 0, 0.0):
                existing["lot_size"] = float(meta.get("lot_size") or 1)
            existing.setdefault("entry_ts", datetime.now(timezone.utc).isoformat())
            self.open_positions[label] = existing

            template_name = existing.get("template_name")
            role = existing.get("role")
            underlying = existing.get("underlying")
            strategy_name = existing.get("strategy_name")
            reason = existing.get("reason")
            entry_ts = existing.get("entry_ts")

        try:
            dashboard_bus.add_position(
                label,
                side=side,
                qty=qty,
                entry_price=entry_price,
                lot_size=existing.get("lot_size"),
                template_name=template_name,
                role=role,
                underlying=underlying,
                strategy_name=strategy_name,
                reason=reason,
                entry_ts=entry_ts,
            )
        except Exception:
            pass
        self._persist_state()

    # Place target/stop orders in a background thread after entry fills.
    def _place_bracket_orders_async(
        self,
        label: str,
        side: str,
        qty: int,
        entry_price: float,
        strategy_name: Optional[str] = None,
        strategy_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Place target profit and stoploss orders as bracket orders after entry fills.
        
        Uses strategy-specific exit parameters if available, otherwise falls back to
        environment variables. This ensures each strategy uses its own configured
        risk management parameters rather than global settings.
        
        Args:
            label: Instrument label
            side: BUY or SELL
            qty: Position quantity
            entry_price: Entry order price
            strategy_name: Strategy name for logging
            strategy_context: Strategy configuration dict with target_pct/tp_pct and sl_pct
        """
        # Run in background thread to avoid blocking the main reconciliation loop
        # Place target and stoploss orders with strategy-specific parameters.
        def _place_exits():
            try:
                with self._state_lock:
                    position_was_tracked = label in self.open_positions
                    kill_switch_active = bool(self.kill_switch_activated)
                if kill_switch_active or self._has_forced_exit_suppression(label):
                    if not position_was_tracked or kill_switch_active:
                        logger.debug(
                            "[BRACKET] Skipping bracket orders for %s because a forced exit or kill switch is active",
                            label,
                        )
                        return
                meta = self.instrument_meta.get(label, {})
                exchange = meta.get("exchange", "NSE")
                symboltoken = meta.get("token")
                tradingsymbol = meta.get("symbol", label)
                lot_size = float(meta.get("lot_size") or 1)
                tick_size = self._resolve_tick_size(meta)
                if lot_size <= 0:
                    lot_size = 1.0
                order_qty_units = int(round(float(qty) * lot_size)) if qty else 0
                if order_qty_units <= 0:
                    logger.debug(
                        "[BRACKET] Skipping orders for %s due to invalid qty: lots=%s lot_size=%s",
                        label,
                        qty,
                        lot_size,
                    )
                    return
                
                # Determine target profit and stoploss settings.
                target_ratio = None
                stoploss_ratio = None
                if strategy_context:
                    for key in ("target_pct", "tp_pct"):
                        if strategy_context.get(key) is not None:
                            target_ratio = self._normalize_strategy_ratio(
                                strategy_context.get(key)
                            )
                            break
                    for key in ("sl_pct", "stoploss_pct", "stop_loss_pct"):
                        if strategy_context.get(key) is not None:
                            stoploss_ratio = self._normalize_strategy_ratio(
                                strategy_context.get(key)
                            )
                            break

                if target_ratio is None:
                    target_ratio = self._normalize_env_percent(
                        os.getenv("DEFAULT_PROFIT_TARGET_PCT", "0.50")
                    )
                if stoploss_ratio is None:
                    stoploss_ratio = self._normalize_env_percent(
                        os.getenv("DEFAULT_STOPLOSS_PCT", "0.30")
                    )

                if target_ratio is None or stoploss_ratio is None:
                    logger.debug(
                        "Bracket orders skipped for %s due to invalid target/stop config",
                        label,
                    )
                    return

                if target_ratio <= 0 or stoploss_ratio <= 0:
                    logger.debug(
                        "Bracket orders skipped for %s (target=%f%% sl=%f%% source=%s)",
                        label,
                        target_ratio * 100.0,
                        stoploss_ratio * 100.0,
                        "strategy" if strategy_context else "environment",
                    )
                    return

                # Exit side is opposite of entry side.
                exit_side = OrderSide.SELL if side.upper() == "BUY" else OrderSide.BUY
                if side.upper() == "BUY":
                    raw_target_price = entry_price * (1.0 + target_ratio)
                    raw_stoploss_price = entry_price * (1.0 - stoploss_ratio)
                else:
                    raw_target_price = entry_price * (1.0 - target_ratio)
                    raw_stoploss_price = entry_price * (1.0 + stoploss_ratio)
                target_price = self._snap_order_price(
                    raw_target_price,
                    tick_size=tick_size,
                    side=exit_side.value,
                )
                stoploss_price = self._snap_order_price(
                    raw_stoploss_price,
                    tick_size=tick_size,
                    side=exit_side.value,
                )

                # Compatibility: fall back to legacy place_order when confirmed helper
                # isn't available (e.g., in tests or non-Angel brokers).
                has_confirmed = callable(
                    getattr(self.order_client, "place_order_confirmed", None)
                )
                
                # Log bracket order configuration
                logger.debug(
                    "[BRACKET] Configuration for %s | source=%s target=%.2f%% sl=%.2f%% target_price=%.2f sl_price=%.2f",
                    label,
                    "strategy" if strategy_context else "environment",
                    target_ratio * 100.0,
                    stoploss_ratio * 100.0,
                    target_price,
                    stoploss_price,
                )
                
                # Place target profit order
                if target_ratio > 0:
                    if has_confirmed:
                        try:
                            response = self.order_client.place_order_confirmed(
                                exchange=exchange,
                                tradingsymbol=tradingsymbol,
                                symboltoken=str(symboltoken),
                                transaction_type=exit_side.value,
                                quantity=order_qty_units,
                                producttype=ProductType.INTRADAY.value,
                                ordertype=OrderType.LIMIT.value,
                                price=target_price,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                timeout_seconds=self.order_confirm_timeout_seconds,
                                poll_seconds=self.order_confirm_poll_seconds,
                                tick_size=tick_size,
                            )
                            terminal = str(response.get("terminal_status") or "")
                            outcome = self._handle_confirmed_closing_order(
                                response=response,
                                label=label,
                                side=exit_side.value,
                                qty=int(qty),
                                price=target_price,
                                trigger_price=None,
                                exchange=exchange,
                                symboltoken=symboltoken,
                                tradingsymbol=tradingsymbol,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                product_type=ProductType.INTRADAY.value,
                            )
                            if terminal == "COMPLETE":
                                logger.info(
                                    "[BRACKET] Target profit order placed for %s at %.2f qty=%d [%s]",
                                    label,
                                    target_price,
                                    order_qty_units,
                                    strategy_name or "UNKNOWN",
                                )
                                log_event(
                                    logger,
                                    event_type="BRACKET_TARGET_ORDER_PLACED",
                                    message="Target profit order placed after entry fill",
                                    label=label,
                                    target_price=target_price,
                                    qty=order_qty_units,
                                    strategy_name=strategy_name,
                                    source="strategy"
                                    if strategy_context
                                    else "environment",
                                )
                                if bool(outcome.get("closed")):
                                    return
                            elif terminal in {"PENDING", "UNKNOWN"}:
                                logger.info(
                                    "[BRACKET] Target profit order pending for %s: %s",
                                    label,
                                    response,
                                )
                            else:
                                logger.warning(
                                    "[BRACKET] Target profit order placement failed for %s: %s",
                                    label,
                                    response,
                                )
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Target profit order placement error for %s: %s",
                                label,
                                exc,
                            )
                    else:
                        try:
                            request = OrderRequest(
                                symbol=tradingsymbol,
                                quantity=order_qty_units,
                                side=exit_side,
                                order_type=OrderType.LIMIT,
                                product_type=ProductType.INTRADAY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=target_price,
                                stop_price=None,
                                tag=f"TARGET_{strategy_name or 'UNKNOWN'}",
                                purpose=OrderPurpose.EXIT,
                                exchange=exchange,
                                symbol_token=str(symboltoken)
                                if symboltoken is not None
                                else None,
                                tick_size=tick_size,
                            )
                            self.order_client.place_order(request)
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Target profit order placement error for %s: %s",
                                label,
                                exc,
                            )
                
                # Place stoploss order
                if stoploss_ratio > 0:
                    stoploss_skip_reason = self._stoploss_skip_reason(
                        label=label,
                        require_position=position_was_tracked,
                    )
                    if stoploss_skip_reason is not None:
                        logger.warning(
                            "[BRACKET] Skipping stoploss order for %s reason=%s",
                            label,
                            stoploss_skip_reason,
                        )
                        log_event(
                            logger,
                            event_type="BRACKET_STOPLOSS_SKIPPED",
                            message="Skipped stoploss order placement.",
                            level=logging.WARNING,
                            label=label,
                            strategy_name=strategy_name,
                            reason=stoploss_skip_reason,
                        )
                        return
                    if has_confirmed:
                        try:
                            response = self.order_client.place_order_confirmed(
                                exchange=exchange,
                                tradingsymbol=tradingsymbol,
                                symboltoken=str(symboltoken),
                                transaction_type=exit_side.value,
                                quantity=order_qty_units,
                                producttype=ProductType.INTRADAY.value,
                                ordertype=OrderType.SLM.value,
                                price=None,
                                trigger_price=stoploss_price,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                timeout_seconds=self.order_confirm_timeout_seconds,
                                poll_seconds=self.order_confirm_poll_seconds,
                                tick_size=tick_size,
                            )
                            if self._is_invalid_slm_ordertype_error(response):
                                fallback_limit_price = self._fallback_stoplimit_price(
                                    trigger_price=stoploss_price,
                                    exit_side=exit_side,
                                    tick_size=tick_size,
                                )
                                logger.info(
                                    "[BRACKET] Retrying stoploss order for %s as SL | trigger=%.2f limit=%.2f side=%s",
                                    label,
                                    stoploss_price,
                                    fallback_limit_price,
                                    exit_side.value,
                                )
                                response = self.order_client.place_order_confirmed(
                                    exchange=exchange,
                                    tradingsymbol=tradingsymbol,
                                    symboltoken=str(symboltoken),
                                    transaction_type=exit_side.value,
                                    quantity=order_qty_units,
                                    producttype=ProductType.INTRADAY.value,
                                    ordertype=OrderType.SL.value,
                                    price=fallback_limit_price,
                                    trigger_price=stoploss_price,
                                    tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                    timeout_seconds=self.order_confirm_timeout_seconds,
                                    poll_seconds=self.order_confirm_poll_seconds,
                                    tick_size=tick_size,
                                )
                            terminal = str(response.get("terminal_status") or "")
                            outcome = self._handle_confirmed_closing_order(
                                response=response,
                                label=label,
                                side=exit_side.value,
                                qty=int(qty),
                                price=response.get("order_row", {}).get("price")
                                if isinstance(response.get("order_row"), dict)
                                else fallback_limit_price if "fallback_limit_price" in locals() else None,
                                trigger_price=stoploss_price,
                                exchange=exchange,
                                symboltoken=symboltoken,
                                tradingsymbol=tradingsymbol,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                product_type=ProductType.INTRADAY.value,
                            )
                            if terminal == "COMPLETE":
                                logger.info(
                                    "[BRACKET] Stoploss order placed for %s at %.2f qty=%d [%s]",
                                    label,
                                    stoploss_price,
                                    order_qty_units,
                                    strategy_name or "UNKNOWN",
                                )
                                log_event(
                                    logger,
                                    event_type="BRACKET_STOPLOSS_ORDER_PLACED",
                                    message="Stoploss order placed after entry fill",
                                    label=label,
                                    stoploss_price=stoploss_price,
                                    qty=order_qty_units,
                                    strategy_name=strategy_name,
                                    source="strategy"
                                    if strategy_context
                                    else "environment",
                                )
                                if bool(outcome.get("closed")):
                                    return
                            elif terminal in {"PENDING", "UNKNOWN"}:
                                logger.info(
                                    "[BRACKET] Stoploss order pending for %s: %s",
                                    label,
                                    response,
                                )
                            else:
                                logger.warning(
                                    "[BRACKET] Stoploss order placement failed for %s: %s",
                                    label,
                                    response,
                                )
                                self._log_stoploss_order_failure(
                                    label=label,
                                    strategy_name=strategy_name,
                                    stoploss_price=stoploss_price,
                                    detail=response,
                                    fallback_mode=(
                                        "sl"
                                        if "fallback_limit_price" in locals()
                                        else "slm"
                                    ),
                                )
                        except Exception as exc:
                            if self._is_invalid_slm_ordertype_error(exc):
                                try:
                                    fallback_limit_price = self._fallback_stoplimit_price(
                                        trigger_price=stoploss_price,
                                        exit_side=exit_side,
                                        tick_size=tick_size,
                                    )
                                    logger.info(
                                        "[BRACKET] Retrying stoploss order for %s as SL | trigger=%.2f limit=%.2f side=%s",
                                        label,
                                        stoploss_price,
                                        fallback_limit_price,
                                        exit_side.value,
                                    )
                                    response = self.order_client.place_order_confirmed(
                                        exchange=exchange,
                                        tradingsymbol=tradingsymbol,
                                        symboltoken=str(symboltoken),
                                        transaction_type=exit_side.value,
                                        quantity=order_qty_units,
                                        producttype=ProductType.INTRADAY.value,
                                        ordertype=OrderType.SL.value,
                                        price=fallback_limit_price,
                                        trigger_price=stoploss_price,
                                        tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                        timeout_seconds=self.order_confirm_timeout_seconds,
                                        poll_seconds=self.order_confirm_poll_seconds,
                                        tick_size=tick_size,
                                    )
                                    terminal = str(response.get("terminal_status") or "")
                                    outcome = self._handle_confirmed_closing_order(
                                        response=response,
                                        label=label,
                                        side=exit_side.value,
                                        qty=int(qty),
                                        price=fallback_limit_price,
                                        trigger_price=stoploss_price,
                                        exchange=exchange,
                                        symboltoken=symboltoken,
                                        tradingsymbol=tradingsymbol,
                                        tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                        product_type=ProductType.INTRADAY.value,
                                    )
                                    if terminal == "COMPLETE":
                                        logger.info(
                                            "[BRACKET] Stoploss order placed for %s at %.2f qty=%d [%s]",
                                            label,
                                            stoploss_price,
                                            order_qty_units,
                                            strategy_name or "UNKNOWN",
                                        )
                                        log_event(
                                            logger,
                                            event_type="BRACKET_STOPLOSS_ORDER_PLACED",
                                            message="Stoploss order placed after entry fill",
                                            label=label,
                                            stoploss_price=stoploss_price,
                                            qty=order_qty_units,
                                            strategy_name=strategy_name,
                                            source="strategy"
                                            if strategy_context
                                            else "environment",
                                        )
                                        if bool(outcome.get("closed")):
                                            return
                                    elif terminal in {"PENDING", "UNKNOWN"}:
                                        logger.info(
                                            "[BRACKET] Stoploss order pending for %s: %s",
                                            label,
                                            response,
                                        )
                                    else:
                                        logger.warning(
                                            "[BRACKET] Stoploss order placement failed for %s: %s",
                                            label,
                                            response,
                                        )
                                        self._log_stoploss_order_failure(
                                            label=label,
                                            strategy_name=strategy_name,
                                            stoploss_price=stoploss_price,
                                            detail=response,
                                            fallback_mode="sl",
                                        )
                                    return
                                except Exception as fallback_exc:
                                    logger.warning(
                                        "[BRACKET] Stoploss order placement error for %s: %s",
                                        label,
                                        fallback_exc,
                                    )
                                    self._log_stoploss_order_failure(
                                        label=label,
                                        strategy_name=strategy_name,
                                        stoploss_price=stoploss_price,
                                        detail=fallback_exc,
                                        fallback_mode="sl",
                                    )
                                    return
                            logger.warning(
                                "[BRACKET] Stoploss order placement error for %s: %s",
                                label,
                                exc,
                            )
                            self._log_stoploss_order_failure(
                                label=label,
                                strategy_name=strategy_name,
                                stoploss_price=stoploss_price,
                                detail=exc,
                                fallback_mode="slm",
                            )
                    else:
                        try:
                            request = OrderRequest(
                                symbol=tradingsymbol,
                                quantity=order_qty_units,
                                side=exit_side,
                                order_type=OrderType.SLM,
                                product_type=ProductType.INTRADAY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=None,
                                stop_price=stoploss_price,
                                tag=f"STOPLOSS_{strategy_name or 'UNKNOWN'}",
                                purpose=OrderPurpose.EXIT,
                                exchange=exchange,
                                symbol_token=str(symboltoken)
                                if symboltoken is not None
                                else None,
                                tick_size=tick_size,
                            )
                            self.order_client.place_order(request)
                        except Exception as exc:
                            logger.warning(
                                "[BRACKET] Stoploss order placement error for %s: %s",
                                label,
                                exc,
                            )
                            self._log_stoploss_order_failure(
                                label=label,
                                strategy_name=strategy_name,
                                stoploss_price=stoploss_price,
                                detail=exc,
                                fallback_mode="legacy",
                            )
                        
            except Exception as exc:
                logger.error(
                    "[BRACKET] Unexpected error placing bracket orders for %s: %s",
                    label,
                    exc,
                )
        
        # Start in background thread (non-blocking)
        thread = threading.Thread(target=_place_exits, daemon=True, name=f"bracket-{label}")
        thread.start()

    # Close a position, compute PnL, and persist trade records.
    def _register_exit(self, label: str, exit_price: float, qty: int) -> None:
        # Drop from executed-token watch list when we close the position
        try:
            executed_tokens_tracker.unregister_position(label)
        except Exception as exc:
            logger.warning(
                "ExecutedTokensTracker.unregister_position failed for %s: %s",
                label,
                exc,
            )
        now_ts = datetime.now(timezone.utc)
        with self._state_lock:
            entry = self.open_positions.pop(label, None)
            if not entry:
                self._persist_state()
                return
            self._reset_daily_if_new_day(now_ts)

            entry_underlying = entry.get("underlying")
            template = entry.get("template_name")
            template_key = str(template or "__UNKNOWN__")
            self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ).get(template_key, set()).discard(label)
            if not self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ).get(template_key):
                self.open_spreads_by_underlying.get(
                    entry_underlying or "__UNKNOWN__", {}
                ).pop(template_key, None)
            if not self.open_spreads_by_underlying.get(
                entry_underlying or "__UNKNOWN__", {}
            ):
                self.open_spreads_by_underlying.pop(entry_underlying or "__UNKNOWN__", None)

            entry_side = entry.get("side", "SELL").upper()
            sign = 1.0 if entry_side == "BUY" else -1.0
            entry_price_val = float(entry.get("entry_price", 0.0))
            meta = self.instrument_meta.get(label, {})
            lot_size = float(entry.get("lot_size") or meta.get("lot_size") or 1)
            pnl = sign * (exit_price - entry_price_val) * qty * lot_size
            self.realized_pnl += pnl
            self.daily_realized_pnl += pnl
            self.max_equity = max(self.max_equity, self.realized_pnl)
            self.daily_peak_equity = max(self.daily_peak_equity, self.daily_realized_pnl)
            realized_total = self.realized_pnl
            drawdown = max(0.0, self.max_equity - self.realized_pnl)

        logger.info(
            "[PNL_EXIT] position_closed | label=%s side=%s qty=%d entry=%.2f exit=%.2f pnl=%.2f total_realized=%.2f",
            label,
            entry_side,
            qty,
            entry_price_val,
            exit_price,
            pnl,
            realized_total,
        )
        self._check_kill_switch(now_ts)
        trade_record = {
            "entry_ts": entry.get("entry_ts"),
            "exit_ts": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "underlying": entry_underlying,
            "strategy_name": entry.get("strategy_name"),
            "template_name": entry.get("template_name"),
            "side": entry_side,
            "qty": qty,
            "entry_price": entry.get("entry_price"),
            "exit_price": exit_price,
            "pnl": pnl,
            "realized_pnl": realized_total,
            "drawdown": drawdown,
            "reason": entry.get("reason"),
            "fees": 0.0,
        }
        if self.trade_persister:
            self.trade_persister.record_trade(trade_record)
        dashboard_bus.close_position(
            label,
            exit_price=exit_price,
            realized_pnl=realized_total,
            trade_record=trade_record,
        )
        logger.info(
            "RiskManager realized PnL for %s | label=%s qty=%d entry=%.3f exit=%.3f -> pnl=%.3f | total=%.3f",
            entry_side,
            label,
            qty,
            entry.get("entry_price"),
            exit_price,
            pnl,
            realized_total,
        )
        self._publish_account_loss_guard(source="register_exit")
        self._persist_state()

    # Activate the kill switch when loss/drawdown limits are hit.
    def _check_kill_switch(self, now: datetime) -> None:
        self.evaluate_account_loss(now=now, source="kill_switch_check", force=True)

    # ----------------------
    # Pending order reconciliation
    # ----------------------

    # Find a pending order context for the given label.
    def _pending_order_for_label(self, label: str) -> Optional[Dict[str, Any]]:
        with self._state_lock:
            for _oid, ctx in self._pending_orders.items():
                if ctx.get("label") == label:
                    return dict(ctx)
        return None

    # Store pending order context and log it for tracking.
    def _record_pending_order(self, *, order_id: str, ctx: Dict[str, Any]) -> None:
        if not order_id:
            return
        ctx = dict(ctx)
        ctx.setdefault("order_id", order_id)
        ctx.setdefault("created_ts", datetime.now(timezone.utc).isoformat())
        ctx.setdefault("last_status", "PENDING")
        with self._state_lock:
            self._pending_orders[str(order_id)] = ctx
        logger.warning(
            "[RISK][%s] Order pending | order_id=%s label=%s side=%s qty=%s",
            self.current_trade_mode,
            order_id,
            ctx.get("label"),
            ctx.get("side"),
            ctx.get("qty"),
        )

    def _pending_closing_order_items(
        self, label: str
    ) -> list[tuple[str, Dict[str, Any]]]:
        target_label = str(label or "")
        if not target_label:
            return []
        with self._state_lock:
            return [
                (str(oid), dict(ctx))
                for oid, ctx in self._pending_orders.items()
                if str(ctx.get("label") or "") == target_label
                and bool(ctx.get("is_closing"))
            ]

    def _cancel_pending_closing_orders_for_label(self, label: str) -> list[str]:
        pending_items = self._pending_closing_order_items(label)
        if not pending_items:
            return []
        cancel_order = getattr(self.order_client, "cancel_order", None)
        if not callable(cancel_order):
            logger.warning(
                "[RISK] Pending closing orders remain for %s because cancel_order is unavailable",
                label,
            )
            return []

        cancelled: list[str] = []
        terminal_statuses = {"CANCELLED", "REJECTED", "EXPIRED"}
        for oid, ctx in pending_items:
            tag = str(ctx.get("tag") or "").strip().upper()
            variety = "STOPLOSS" if tag.startswith("STOPLOSS") else "NORMAL"
            symbol = str(ctx.get("tradingsymbol") or ctx.get("label") or "").strip()
            try:
                try:
                    response = cancel_order(str(oid), symbol=symbol or None, variety=variety)
                except TypeError:
                    response = cancel_order(
                        order_id=str(oid),
                        symbol=symbol or None,
                        variety=variety,
                    )
            except Exception as exc:
                logger.warning(
                    "[RISK] Failed to cancel pending closing order before forced exit | order_id=%s label=%s error=%s",
                    oid,
                    label,
                    exc,
                )
                continue

            success = bool(response) and not isinstance(response, dict)
            terminal = "UNKNOWN"
            if isinstance(response, dict):
                response_data = response.get("data")
                raw_status = (
                    response.get("orderstatus")
                    or response.get("status")
                    if isinstance(response.get("status"), str)
                    else None
                )
                if raw_status is None and isinstance(response_data, dict):
                    raw_status = (
                        response_data.get("orderstatus")
                        or response_data.get("status")
                    )
                terminal = self._normalize_broker_order_status(raw_status)
                success = bool(response.get("status") is True or terminal in terminal_statuses)
            if not success:
                logger.warning(
                    "[RISK] Pending closing order cancel not confirmed | order_id=%s label=%s response=%s",
                    oid,
                    label,
                    response,
                )
                continue

            resolved_status = terminal if terminal in terminal_statuses else "CANCELLED"
            with self._state_lock:
                existing_ctx = self._pending_orders.pop(str(oid), None)
                if existing_ctx is not None:
                    existing_ctx["last_status"] = resolved_status
                    existing_ctx["cancelled_ts"] = datetime.now(timezone.utc).isoformat()
            cancelled.append(str(oid))
            logger.info(
                "[RISK] Cancelled pending closing order before forced exit | order_id=%s label=%s status=%s",
                oid,
                label,
                resolved_status,
            )
        remaining_pending = self._pending_closing_order_items(label)
        if remaining_pending:
            log_event(
                logger,
                event_type="FORCED_EXIT_PENDING_ORDERS_REMAIN",
                message="Pending closing orders remain after forced-exit cancellation attempt.",
                level=logging.WARNING,
                label=label,
                pending_count=len(remaining_pending),
                pending_order_ids=",".join(str(oid) for oid, _ctx in remaining_pending),
            )
        return cancelled

    def _reconcile_pending_order(
        self,
        order_id: str,
        *,
        ctx: Optional[Dict[str, Any]] = None,
        order_index: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        oid = str(order_id or "")
        if not oid:
            return {"matched": False, "closed": False, "pending": False, "status": "UNKNOWN"}
        if ctx is None:
            with self._state_lock:
                existing_ctx = self._pending_orders.get(oid)
            if existing_ctx is None:
                return {"matched": False, "closed": False, "pending": False, "status": "UNKNOWN"}
            ctx = dict(existing_ctx)
        else:
            ctx = dict(ctx)

        row = self._load_pending_order_row(oid, order_index=order_index)
        if not row:
            return {
                "matched": True,
                "closed": False,
                "pending": True,
                "status": str(ctx.get("last_status") or "PENDING"),
            }

        raw_status = (
            row.get("orderstatus")
            or row.get("orderStatus")
            or row.get("status")
            or row.get("order_state")
        )
        status = self._normalize_broker_order_status(raw_status)
        with self._state_lock:
            existing_ctx = self._pending_orders.get(oid)
            if existing_ctx is not None:
                existing_ctx["last_status"] = status
                existing_ctx["last_checked_ts"] = datetime.now(timezone.utc).isoformat()

        logger.info(
            "[ORDER_DETAILS] order_id=%s status=%s averageprice=%s filledshares=%s",
            oid,
            status,
            row.get("averageprice")
            or row.get("average_price")
            or row.get("avgprice")
            or row.get("averagePrice"),
            row.get("filledshares")
            or row.get("filled_qty")
            or row.get("filled_quantity")
            or row.get("filledQuantity"),
        )

        if status == "COMPLETE":
            label = str(ctx.get("label") or "")
            side = str(ctx.get("side") or "")
            qty = self._pending_order_qty_from_row(ctx=ctx, row=row)
            price = float(
                self._pick_nonzero_price(
                    row.get("averageprice"),
                    row.get("average_price"),
                    row.get("avgprice"),
                    row.get("averagePrice"),
                    row.get("tradedprice"),
                    row.get("traded_price"),
                    row.get("fillprice"),
                    row.get("filledprice"),
                    row.get("price"),
                    ctx.get("price"),
                    ctx.get("trigger_price"),
                    0.0,
                )
                or 0.0
            )
            if not label or qty <= 0:
                with self._state_lock:
                    self._pending_orders.pop(oid, None)
                return {
                    "matched": True,
                    "closed": False,
                    "pending": False,
                    "status": status,
                }

            self._persist_execution_record_from_ctx(
                ctx=ctx,
                qty=qty,
                price=price,
                broker_order_id=oid,
            )

            if bool(ctx.get("is_closing")):
                self._register_exit(label, price, qty)
            else:
                self._register_entry(
                    label,
                    side,
                    qty,
                    price,
                    str(ctx.get("template_name") or "__UNKNOWN__"),
                    ctx.get("role"),
                    ctx.get("underlying"),
                    ctx.get("strategy_name"),
                    ctx.get("reason"),
                    ml_info=ctx.get("ml_info"),
                    count_for_spreads=bool(ctx.get("count_for_spreads")),
                )

            with self._state_lock:
                self._pending_orders.pop(oid, None)
            return {
                "matched": True,
                "closed": bool(ctx.get("is_closing")),
                "pending": False,
                "status": status,
                "price": price,
                "qty": qty,
            }

        if status in ("REJECTED", "CANCELLED", "EXPIRED"):
            logger.warning(
                "[RISK][%s] Pending order terminal=%s | order_id=%s label=%s",
                self.current_trade_mode,
                status,
                oid,
                ctx.get("label"),
            )
            with self._state_lock:
                self._pending_orders.pop(oid, None)
            return {
                "matched": True,
                "closed": False,
                "pending": False,
                "status": status,
            }

        return {"matched": True, "closed": False, "pending": True, "status": status}

    def reconcile_pending_closing_orders_for_label(self, label: str) -> Dict[str, Any]:
        target_label = str(label or "")
        if not target_label:
            return {"matched": False, "closed": False, "pending": False}
        with self._state_lock:
            pending_items = [
                (str(oid), dict(ctx))
                for oid, ctx in self._pending_orders.items()
                if str(ctx.get("label") or "") == target_label and bool(ctx.get("is_closing"))
            ]
        if not pending_items:
            return {"matched": False, "closed": False, "pending": False}

        matched = False
        pending = False
        statuses: list[str] = []
        for oid, ctx in pending_items:
            outcome = self._reconcile_pending_order(oid, ctx=ctx)
            matched = matched or bool(outcome.get("matched"))
            status = str(outcome.get("status") or "")
            if status:
                statuses.append(status)
            if bool(outcome.get("closed")):
                return {
                    "matched": True,
                    "closed": True,
                    "pending": False,
                    "status": status or "COMPLETE",
                    "statuses": statuses,
                }
            pending = pending or bool(outcome.get("pending"))
        return {
            "matched": matched,
            "closed": False,
            "pending": pending,
            "status": statuses[-1] if statuses else "UNKNOWN",
            "statuses": statuses,
        }

    # Resolve pending orders by inspecting the broker order book.
    def _reconcile_pending_orders(self) -> None:
        """Check pending broker orders and finalize state only when COMPLETE."""
        if self.current_trade_mode.upper() != "LIVE":
            return
        with self._state_lock:
            pending_items = list(self._pending_orders.items())
        if not pending_items:
            return

        # Fetch once to limit API pressure.
        try:
            book = self.order_client.get_order_book()
        except Exception as exc:
            logger.warning("Pending order reconcile: get_order_book failed: %s", exc)
            return

        rows = book.get("data") if isinstance(book, dict) else None
        if not isinstance(rows, list):
            return
        index: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            oid = row.get("orderid") or row.get("order_id") or row.get("orderId")
            if oid:
                index[str(oid)] = row

        for oid, ctx in pending_items:
            self._reconcile_pending_order(oid, ctx=ctx, order_index=index)

    # Submit an order to the broker or simulate in paper mode.
    def _submit_order(
        self,
        *,
        exchange: str,
        symboltoken: str,
        tradingsymbol: str,
        side: str,
        quantity: int,
        producttype: str,
        trade_mode: str,
        tag: Optional[str] = None,
    ) -> Tuple[bool, str, Optional[str], Optional[Dict[str, Any]]]:
        mode = trade_mode.upper()
        self._reset_daily_if_new_day(datetime.now(timezone.utc))
        if mode != "LIVE":
            logger.info(
                "[PAPER][RiskManager] %s %s (%s:%s) qty=%d tag=%s",
                side,
                tradingsymbol,
                exchange,
                symboltoken,
                quantity,
                tag,
            )
            return True, "PAPER", None, None

        try:
            confirm = self.order_client.place_order_confirmed(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                symboltoken=str(symboltoken),
                transaction_type=side,
                quantity=quantity,
                producttype=producttype,
                ordertype="MARKET",
                variety="NORMAL",
                tag=tag,
                timeout_seconds=self.order_confirm_timeout_seconds,
                poll_seconds=self.order_confirm_poll_seconds,
            )
            order_id = str(confirm.get("order_id") or "")
            terminal = str(confirm.get("terminal_status") or "UNKNOWN")
            order_row = confirm.get("order_row") if isinstance(confirm, dict) else None
            logger.info(
                "[LIVE][RiskManager] %s %s (%s:%s) qty=%d tag=%s -> order_id=%s terminal=%s",
                side,
                tradingsymbol,
                exchange,
                symboltoken,
                quantity,
                tag,
                order_id or "-",
                terminal,
            )
            return (terminal == "COMPLETE"), terminal, order_id, order_row
        except Exception as exc:
            logger.error("[LIVE][RiskManager] order failed: %s", exc)
            return False, "ERROR", None, None

    # Main entry point to place an order with full risk checks.
    def place_order(
        self,
        *,
        label: str,
        side: str,
        qty: int,
        price: float,
        template_name: Optional[str] = None,
        role: Optional[str] = None,
        trade_mode: str = "PAPER",
        product_type: str = "INTRADAY",
        strategy_name: Optional[str] = None,
        reason: Optional[str] = None,
        tag: Optional[str] = None,
        ml_info: Optional[Dict[str, Any]] = None,
    ) -> RiskDecision:
        side = side.upper()
        meta = self.instrument_meta.get(label, {})
        exchange = meta.get("exchange", "NSE")
        symboltoken = meta.get("token")
        tradingsymbol = meta.get("symbol", label)
        underlying = meta.get("underlying")
        heartbeat_label = underlying or label
        self.current_trade_mode = trade_mode.upper()
        mode_tag = f"[RISK][{self.current_trade_mode}]"
        dashboard_bus.set_trade_mode(self.current_trade_mode)
        now_ts = datetime.now(timezone.utc)
        self._reset_daily_if_new_day(now_ts)
        # Finalize any previously submitted broker orders before deciding on new actions.
        self._reconcile_pending_orders()

        if self.current_trade_mode.upper() == "LIVE":
            # §102: In LIVE hub-authoritative mode, direct broker order placement
            # through RiskManager is forbidden for automated entries.  All entry
            # and exit signals must pass through the hub/router/lifecycle path.
            # If the hub exit submitter is set, this indicates hub mode is active;
            # block legacy direct entries.  Closing (exit) orders are still
            # allowed through this path as break-glass or reconciliation exits.
            with self._state_lock:
                _existing_for_guard = (
                    dict(self.open_positions.get(label, {}))
                    if label in self.open_positions
                    else None
                )
            _is_closing_entry = bool(
                _existing_for_guard
                and _existing_for_guard.get("side", "").upper() != side
            )
            if (
                self._hub_exit_submitter is not None
                and not _is_closing_entry
            ):
                logger.error(
                    "risk_manager.legacy_entry_blocked: LIVE hub-authoritative mode active "
                    "but RiskManager.place_order() called for automated entry "
                    "label=%s side=%s qty=%d strategy=%s. "
                    "Use place_order_via_bridge / hub router instead.",
                    label, side, qty, strategy_name,
                )
                return self._make_decision(
                    allowed=False,
                    reason="legacy_direct_entry_blocked_in_live_hub_mode",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            pending = self._pending_order_for_label(label)
            if pending:
                return self._make_decision(
                    allowed=False,
                    reason=f"order_pending:{pending.get('last_status') or 'PENDING'}",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )
        with self._state_lock:
            existing = dict(self.open_positions.get(label, {})) if label in self.open_positions else None
        is_closing = existing and existing.get("side", "").upper() != side

        if side not in {"BUY", "SELL"} or qty <= 0:
            logger.warning("%s Skipping invalid order %s qty=%d", mode_tag, label, qty)
            return self._make_decision(
                allowed=False,
                reason="invalid_order_params",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if not is_closing:
            self.evaluate_account_loss(
                now=now_ts,
                source="place_order",
                force=True,
            )
        if self.kill_switch_activated:
            if not is_closing:
                logger.warning("%s Kill switch active, blocking new entries", mode_tag)
                return self._make_decision(
                    allowed=False,
                    reason="kill_switch_active_blocking_entries",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

        # Always enforce day-loss and maintenance before spread limits
        if (
            not is_closing
            and self.day_loss_limit > 0
            and self.realized_pnl <= -abs(self.day_loss_limit)
        ):
            logger.warning(
                "%s Day loss limit reached (limit=%.2f, realized=%.2f); blocking new entries",
                mode_tag,
                self.day_loss_limit,
                self.realized_pnl,
            )
            return self._make_decision(
                allowed=False,
                reason="day_loss_limit_reached",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if (
            self.maintenance
            and not is_closing
            and not self.maintenance.allow_new_entries(heartbeat_label)
        ):
            logger.warning(
                "%s [%s] Maintenance blocks new entries", mode_tag, heartbeat_label
            )
            return self._make_decision(
                allowed=False,
                reason="maintenance_block",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        if side == "SELL" and not is_closing and not self._can_enter(
            underlying, template_name
        ):
            logger.warning(
                "%s Spread cap hit for %s (template=%s)",
                mode_tag,
                underlying,
                template_name,
            )
            return self._make_decision(
                allowed=False,
                reason="max_spreads_reached",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        underlying_label = self._underlying_label_for_label(label)
        if (
            not is_closing
            and self.instrument_controller
            and not self.instrument_controller.is_enabled(underlying_label)
        ):
            logger.warning(
                "[INSTRUMENT_CONTROL] Blocking order: strategy=%s instrument=%s reason=DISABLED",
                strategy_name,
                underlying_label,
            )
            return self._make_decision(
                allowed=False,
                reason="instrument_disabled",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )
        if (
            not is_closing
            and self.instrument_controller
            and strategy_name
            and not self.instrument_controller.is_strategy_allowed(
                underlying_label, strategy_name
            )
        ):
            logger.warning(
                "[INSTRUMENT_CONTROL] Blocking order: strategy=%s instrument=%s reason=NOT_ALLOWED",
                strategy_name,
                underlying_label,
            )
            return self._make_decision(
                allowed=False,
                reason="strategy_not_allowed_for_instrument",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        # Per-underlying lot/notional limits (only for opening trades)
        if not is_closing:
            limits = self._get_limits_for_underlying(underlying)
            meta = self.instrument_meta.get(label, {})
            lot_size = float(meta.get("lot_size") or 1)
            order_lots = float(qty)
            open_lots_before = self._get_current_open_lots(underlying)
            open_lots_after = open_lots_before + order_lots

            max_lots_per_trade = limits.get("max_lots_per_trade")
            if max_lots_per_trade is not None and order_lots > float(
                max_lots_per_trade
            ):
                logger.warning(
                    "%s Blocked order for %s: lots=%.3f open_before=%.3f limit=%.3f reason=RISK_MAX_LOTS_PER_TRADE",
                    mode_tag,
                    underlying,
                    order_lots,
                    open_lots_before,
                    max_lots_per_trade,
                )
                return self._make_decision(
                    allowed=False,
                    reason="max_lots_per_trade_exceeded",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            max_open_lots = limits.get("max_open_lots")
            if max_open_lots is not None and open_lots_after > float(max_open_lots):
                logger.warning(
                    "%s Blocked order for %s: lots=%.3f open_before=%.3f open_after=%.3f limit=%.3f reason=RISK_MAX_OPEN_LOTS",
                    mode_tag,
                    underlying,
                    order_lots,
                    open_lots_before,
                    open_lots_after,
                    max_open_lots,
                )
                return self._make_decision(
                    allowed=False,
                    reason="max_open_lots_exceeded",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            max_notional = limits.get("max_notional_per_underlying")
            if max_notional:
                try:
                    price_f = float(price)
                    notional_after = open_lots_after * lot_size * price_f
                    if notional_after > float(max_notional):
                        logger.warning(
                            "%s Blocked order for %s: notional_after=%.2f limit=%.2f reason=RISK_MAX_NOTIONAL_EXCEEDED",
                            mode_tag,
                            underlying,
                            notional_after,
                            max_notional,
                        )
                        return self._make_decision(
                            allowed=False,
                            reason="max_notional_exceeded",
                            label=label,
                            side=side,
                            qty=qty,
                            strategy_name=strategy_name,
                            template_name=template_name,
                        )
                except Exception:
                    logger.warning(
                        "%s Unable to compute notional for %s, skipping notional check",
                        mode_tag,
                        underlying,
                    )

        symboltoken_str = str(symboltoken or "")
        ok, terminal, broker_order_id, _order_row = self._submit_order(
            exchange=exchange,
            symboltoken=symboltoken_str,
            tradingsymbol=tradingsymbol,
            side=side,
            quantity=qty,
            producttype=product_type,
            trade_mode=trade_mode,
            tag=tag,
        )
        if not ok:
            # If the broker has accepted the order but it hasn't reached a terminal
            # status yet, treat it as pending and block duplicates.
            if (
                self.current_trade_mode.upper() == "LIVE"
                and broker_order_id
                and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
            ):
                self._record_pending_order(
                    order_id=str(broker_order_id),
                    ctx={
                        "label": label,
                        "side": side,
                        "qty": qty,
                        "price": price,
                        "template_name": template_name,
                        "role": role,
                        "underlying": underlying,
                        "strategy_name": strategy_name,
                        "reason": reason,
                        "ml_info": ml_info,
                        "count_for_spreads": (side == "SELL"),
                        "is_closing": bool(is_closing),
                        "exchange": exchange,
                        "symboltoken": symboltoken,
                        "tradingsymbol": tradingsymbol,
                        "tag": tag,
                        "product_type": product_type,
                    },
                )
                return self._make_decision(
                    allowed=False,
                    reason=f"order_pending:{terminal}",
                    label=label,
                    side=side,
                    qty=qty,
                    strategy_name=strategy_name,
                    template_name=template_name,
                )

            return self._make_decision(
                allowed=False,
                reason=f"order_{(terminal or 'submit_failed').lower()}",
                label=label,
                side=side,
                qty=qty,
                strategy_name=strategy_name,
                template_name=template_name,
            )

        def _coerce_float(value: Any) -> Optional[float]:
            try:
                if value is None:
                    return None
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*values: Any) -> Optional[float]:
            for v in values:
                f = _coerce_float(v)
                if f is None:
                    continue
                if f == 0.0:
                    continue
                return f
            return None

        fill_price = None
        if isinstance(_order_row, dict):
            fill_price = _pick_price(
                _order_row.get("averageprice"),
                _order_row.get("average_price"),
                _order_row.get("avgprice"),
                _order_row.get("averagePrice"),
                _order_row.get("tradedprice"),
                _order_row.get("traded_price"),
                _order_row.get("fillprice"),
                _order_row.get("filledprice"),
                _order_row.get("price"),
            )

        exec_price = float(fill_price if fill_price is not None else price)
        exec_ts = datetime.now(timezone.utc).isoformat()
        if self.trade_persister:
            exec_record = {
                "timestamp": exec_ts,
                "label": label,
                "underlying": underlying,
                "strategy_name": strategy_name,
                "template_name": template_name,
                "reason": reason,
                "side": side,
                "qty": qty,
                "price": exec_price,
                "exchange": exchange,
                "symboltoken": symboltoken,
                "tradingsymbol": tradingsymbol,
                "trade_mode": trade_mode,
                "product_type": product_type,
                "tag": tag,
                "broker_order_id": broker_order_id,
            }
            exec_record["trade_id"] = "|".join(
                [
                    str(symboltoken or tradingsymbol or label),
                    side,
                    str(qty),
                    exec_ts,
                ]
            )
            try:
                self.trade_persister.record_execution(exec_record)
            except Exception as exc:
                logger.error("Failed to persist execution for %s: %s", label, exc)

        strategy_tag = strategy_name or "__UNKNOWN_STRAT__"
        if is_closing:
            self._register_exit(label, exec_price, qty)
        else:
            self._register_entry(
                label,
                side,
                qty,
                exec_price,
                template_name or "__UNKNOWN__",
                role,
                underlying,
                strategy_name,
                reason,
                ml_info=ml_info,
                count_for_spreads=(side == "SELL"),
            )
            logger.info(
                "[%s] ENTER %s %s qty=%d price=%.2f tmpl=%s role=%s reason=%s",
                strategy_tag,
                side,
                tradingsymbol,
                qty,
                price,
                template_name,
                role,
                reason,
            )
        if is_closing:
            logger.info(
                "[%s] EXIT %s %s qty=%d price=%.2f tmpl=%s role=%s reason=%s",
                strategy_tag,
                side,
                tradingsymbol,
                qty,
                price,
                template_name,
                role,
                reason,
            )
        self.current_trade_mode = trade_mode.upper()

        return self._make_decision(
            allowed=True,
            reason=reason or "order_submitted",
            label=label,
            side=side,
            qty=qty,
            strategy_name=strategy_name,
            template_name=template_name,
        )

    # Square off all open positions immediately.
    def square_off_all(
        self,
        *,
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        reason: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> None:
        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())
        for label, entry in positions_snapshot:
            side = entry.get("side", "SELL").upper()
            qty = entry.get("qty", 0)
            exit_side = "BUY" if side == "SELL" else "SELL"
            price = float(entry.get("entry_price", 0.0))
            if ltp_lookup and label in ltp_lookup:
                price = float(ltp_lookup[label])
            meta = self.instrument_meta.get(label, {})
            entry_exchange = entry.get("exchange")
            entry_token = entry.get("symboltoken")
            entry_symbol = entry.get("tradingsymbol")
            exchange = entry_exchange or meta.get("exchange", "NSE")
            symboltoken = entry_token or meta.get("token")
            tradingsymbol = entry_symbol or meta.get("symbol", label)
            if not symboltoken or qty <= 0:
                continue
            strategy_name = (
                str(entry.get("strategy_name") or "").strip() or None
            )
            exit_reason = str(reason or tag or "SQUARE_OFF")
            self._mark_forced_exit_suppression(label)
            try:
                self._cancel_pending_closing_orders_for_label(label)
                if self._submit_exit_via_hub(
                    label=label,
                    strategy_name=strategy_name,
                    exchange=str(exchange),
                    symboltoken=str(symboltoken),
                    tradingsymbol=str(tradingsymbol),
                    side=exit_side,
                    quantity=int(qty),
                    trade_mode=trade_mode,
                    tag=tag,
                    reason=exit_reason,
                ):
                    self._register_exit(label, price, qty)
                    continue
                self._assert_direct_submit_allowed(
                    trade_mode=trade_mode,
                    exit_source="square_off_all",
                    label=label,
                )
                ok, terminal, broker_order_id, _order_row = self._submit_order(
                    exchange=exchange,
                    symboltoken=symboltoken,
                    tradingsymbol=tradingsymbol,
                    side=exit_side,
                    quantity=qty,
                    producttype="INTRADAY",
                    trade_mode=trade_mode,
                    tag=tag,
                )
                if not ok:
                    if (
                        trade_mode.upper() == "LIVE"
                        and broker_order_id
                        and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
                    ):
                        self._record_pending_order(
                            order_id=str(broker_order_id),
                            ctx={
                                "label": label,
                                "side": exit_side,
                                "qty": int(qty),
                                "price": float(price),
                                "template_name": entry.get("template_name"),
                                "role": entry.get("role"),
                                "underlying": entry.get("underlying"),
                                "strategy_name": entry.get("strategy_name"),
                                "reason": entry.get("reason") or tag,
                                "ml_info": entry.get("ml_info"),
                                "count_for_spreads": False,
                                "is_closing": True,
                                "exchange": exchange,
                                "symboltoken": symboltoken,
                                "tradingsymbol": tradingsymbol,
                                "tag": tag,
                                "product_type": "INTRADAY",
                            },
                        )
                    continue

                self._register_exit(label, price, qty)
            except P0RuleViolation:
                logger.warning(
                    "Direct broker submit blocked for %s in hub-authoritative LIVE (square_off_all)",
                    label,
                )
            except Exception as exc:
                logger.error("Error while square-off %s: %s", label, exc)

    # Square off positions at end-of-day, respecting exclusions.
    def square_off_eod(
        self,
        *,
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        exclude_underlyings: Optional[set[str]] = None,
        processed_underlyings: Optional[set[str]] = None,
        tag: str = "EOD_SQUARE_OFF",
    ) -> set[str]:
        """

        from __future__ import annotations

                Square-off all open positions except excluded underlyings, skipping any already processed.
                Returns the set of underlyings for which square-off orders were placed successfully.
        """
        exclude_set = {u.upper() for u in (exclude_underlyings or set())}
        processed_set = {u.upper() for u in (processed_underlyings or set())}
        positions_by_underlying: Dict[str, list[tuple[str, Dict[str, Any]]]] = {}

        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())
        for label, entry in positions_snapshot:
            underlying = (
                entry.get("underlying") or self._infer_underlying(label) or ""
            ).upper()
            if (
                not underlying
                or underlying in exclude_set
                or underlying in processed_set
            ):
                continue
            positions_by_underlying.setdefault(underlying, []).append((label, entry))

        closed_underlyings: set[str] = set()
        for underlying, entries in positions_by_underlying.items():
            any_success = False
            had_failure = False
            for label, entry in entries:
                side = entry.get("side", "SELL").upper()
                qty = int(entry.get("qty", 0))
                if qty <= 0:
                    continue
                exit_side = "BUY" if side == "SELL" else "SELL"
                price = float(entry.get("entry_price", 0.0))
                if ltp_lookup and label in ltp_lookup:
                    price = float(ltp_lookup[label])

                meta = self.instrument_meta.get(label, {})
                exchange = entry.get("exchange") or meta.get("exchange", "NSE")
                symboltoken = entry.get("symboltoken") or meta.get("token")
                tradingsymbol = entry.get("tradingsymbol") or meta.get("symbol", label)
                if not symboltoken:
                    logger.warning(
                        "[EOD] Missing token for %s (%s), skipping square-off",
                        label,
                        underlying,
                    )
                    continue

                logger.info(
                    "[EOD] Square-off | ts=%s underlying=%s label=%s side=%s qty=%d reason=%s",
                    datetime.now(timezone.utc).isoformat(),
                    underlying,
                    label,
                    exit_side,
                    qty,
                    tag,
                )

                try:
                    strategy_name = (
                        str(entry.get("strategy_name") or "").strip() or None
                    )
                    self._mark_forced_exit_suppression(label)
                    self._cancel_pending_closing_orders_for_label(label)
                    if self._submit_exit_via_hub(
                        label=label,
                        strategy_name=strategy_name,
                        exchange=str(exchange),
                        symboltoken=str(symboltoken),
                        tradingsymbol=str(tradingsymbol),
                        side=exit_side,
                        quantity=int(qty),
                        trade_mode=trade_mode,
                        tag=tag,
                        reason=tag,
                    ):
                        self._register_exit(label, price, qty)
                        any_success = True
                        continue
                    self._assert_direct_submit_allowed(
                        trade_mode=trade_mode,
                        exit_source="square_off_eod",
                        label=label,
                    )
                    ok, terminal, broker_order_id, _order_row = self._submit_order(
                        exchange=exchange,
                        symboltoken=symboltoken,
                        tradingsymbol=tradingsymbol,
                        side=exit_side,
                        quantity=qty,
                        producttype="INTRADAY",
                        trade_mode=trade_mode,
                        tag=tag,
                    )
                    if not ok:
                        had_failure = True
                        if (
                            trade_mode.upper() == "LIVE"
                            and broker_order_id
                            and terminal not in (
                                "REJECTED",
                                "CANCELLED",
                                "EXPIRED",
                                "ERROR",
                            )
                        ):
                            self._record_pending_order(
                                order_id=str(broker_order_id),
                                ctx={
                                    "label": label,
                                    "side": exit_side,
                                    "qty": int(qty),
                                    "price": float(price),
                                    "template_name": entry.get("template_name"),
                                    "role": entry.get("role"),
                                    "underlying": entry.get("underlying"),
                                    "strategy_name": entry.get("strategy_name"),
                                    "reason": entry.get("reason") or tag,
                                    "ml_info": entry.get("ml_info"),
                                    "count_for_spreads": False,
                                    "is_closing": True,
                                    "exchange": exchange,
                                    "symboltoken": symboltoken,
                                    "tradingsymbol": tradingsymbol,
                                    "tag": tag,
                                    "product_type": "INTRADAY",
                                },
                            )
                        continue

                    self._register_exit(label, price, qty)
                    if self.trade_persister:
                        exec_ts = datetime.now(timezone.utc).isoformat()
                        exec_record = {
                            "timestamp": exec_ts,
                            "label": label,
                            "underlying": underlying,
                            "strategy_name": entry.get("strategy_name"),
                            "template_name": entry.get("template_name"),
                            "reason": entry.get("reason") or tag,
                            "side": exit_side,
                            "qty": qty,
                            "price": price,
                            "exchange": exchange,
                            "symboltoken": symboltoken,
                            "tradingsymbol": tradingsymbol,
                            "trade_mode": trade_mode,
                            "product_type": "INTRADAY",
                            "tag": tag,
                            "broker_order_id": broker_order_id,
                        }
                        exec_record["trade_id"] = "|".join(
                            [
                                str(symboltoken or tradingsymbol or label),
                                exit_side,
                                str(qty),
                                exec_ts,
                            ]
                        )
                        try:
                            self.trade_persister.record_execution(exec_record)
                        except Exception as exc:
                            logger.error(
                                "[EOD] Failed to persist execution for %s: %s",
                                label,
                                exc,
                            )
                    any_success = True
                except Exception as exc:
                    logger.error(
                        "[EOD] Square-off failed for %s (%s): %s",
                        label,
                        underlying,
                        exc,
                    )
                    had_failure = True
            if any_success and not had_failure:
                closed_underlyings.add(underlying)
        return closed_underlyings

    # Square off positions belonging to specific strategies.
    def square_off_strategies(
        self,
        *,
        strategy_names: set[str],
        trade_mode: str = "LIVE",
        ltp_lookup: Optional[Dict[str, float]] = None,
        tag: str = "STRATEGY_SQUARE_OFF",
        reason: Optional[str] = None,
    ) -> set[str]:
        target_names = {
            canonicalize_strategy_name(
                str(name),
                source="risk.square_off_strategies.target",
                warn_alias=False,
            )
            for name in (strategy_names or set())
            if str(name or "").strip()
        }
        if not target_names:
            return set()

        with self._state_lock:
            positions_snapshot = list(self.open_positions.items())

        closed_labels: set[str] = set()
        for label, entry in positions_snapshot:
            strategy_name = canonicalize_strategy_name(
                str(entry.get("strategy_name") or "").strip(),
                source="risk.square_off_strategies.entry",
                warn_alias=False,
            )
            if strategy_name not in target_names:
                continue

            side = str(entry.get("side", "SELL")).upper()
            qty = int(entry.get("qty", 0) or 0)
            if qty <= 0:
                continue
            # Avoid duplicate submit while prior exit is pending broker confirmation.
            if self._pending_order_for_label(label):
                continue

            exit_side = "BUY" if side == "SELL" else "SELL"
            price = float(entry.get("entry_price", 0.0) or 0.0)
            if ltp_lookup and label in ltp_lookup:
                price = float(ltp_lookup[label])

            meta = self.instrument_meta.get(label, {})
            exchange = entry.get("exchange") or meta.get("exchange", "NSE")
            symboltoken = entry.get("symboltoken") or meta.get("token")
            tradingsymbol = entry.get("tradingsymbol") or meta.get("symbol", label)
            if not symboltoken:
                logger.warning(
                    "[%s] Missing token for %s (strategy=%s), skipping square-off",
                    tag,
                    label,
                    strategy_name,
                )
                continue

            try:
                exit_reason = str(reason or tag or "STRATEGY_SQUARE_OFF")
                strategy_name_for_route = (
                    str(entry.get("strategy_name") or "").strip() or None
                )
                self._mark_forced_exit_suppression(label)
                self._cancel_pending_closing_orders_for_label(label)
                if self._submit_exit_via_hub(
                    label=label,
                    strategy_name=strategy_name_for_route,
                    exchange=str(exchange),
                    symboltoken=str(symboltoken),
                    tradingsymbol=str(tradingsymbol),
                    side=exit_side,
                    quantity=int(qty),
                    trade_mode=trade_mode,
                    tag=tag,
                    reason=exit_reason,
                ):
                    self._register_exit(label, price, qty)
                    closed_labels.add(label)
                    continue

                self._assert_direct_submit_allowed(
                    trade_mode=trade_mode,
                    exit_source="square_off_strategies",
                    label=label,
                )
                ok, terminal, broker_order_id, _order_row = self._submit_order(
                    exchange=exchange,
                    symboltoken=symboltoken,
                    tradingsymbol=tradingsymbol,
                    side=exit_side,
                    quantity=qty,
                    producttype="INTRADAY",
                    trade_mode=trade_mode,
                    tag=tag,
                )
                if not ok:
                    if (
                        trade_mode.upper() == "LIVE"
                        and broker_order_id
                        and terminal not in ("REJECTED", "CANCELLED", "EXPIRED", "ERROR")
                    ):
                        self._record_pending_order(
                            order_id=str(broker_order_id),
                            ctx={
                                "label": label,
                                "side": exit_side,
                                "qty": int(qty),
                                "price": float(price),
                                "template_name": entry.get("template_name"),
                                "role": entry.get("role"),
                                "underlying": entry.get("underlying"),
                                "strategy_name": entry.get("strategy_name"),
                                "reason": entry.get("reason") or tag,
                                "ml_info": entry.get("ml_info"),
                                "count_for_spreads": False,
                                "is_closing": True,
                                "exchange": exchange,
                                "symboltoken": symboltoken,
                                "tradingsymbol": tradingsymbol,
                                "tag": tag,
                                "product_type": "INTRADAY",
                            },
                        )
                    continue

                self._register_exit(label, price, qty)
                closed_labels.add(label)
            except P0RuleViolation:
                logger.warning(
                    "Direct broker submit blocked for %s in hub-authoritative LIVE (square_off_strategies)",
                    label,
                )
            except Exception as exc:
                logger.error(
                    "[%s] Square-off failed for %s (strategy=%s): %s",
                    tag,
                    label,
                    strategy_name,
                    exc,
                )
        return closed_labels
