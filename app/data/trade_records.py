from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import logging

from app.core.identifiers import (
    BrokerAccountId,
    HubOrderId,
    StrategyId,
    TenantId,
)
from app.config.settings import get_settings
from app.core.logging_utils import log_event

logger = logging.getLogger(__name__)


# Canonical trade fill record for BigQuery persistence.
@dataclass
class TradeRecord:
    """
    Canonical representation of a single trade fill to persist to BigQuery.
    """

    trade_id: str
    hub_order_id: Optional[HubOrderId]
    tenant_id: TenantId
    broker_account_id: BrokerAccountId
    strategy_id: StrategyId

    symbol: str
    side: str  # "BUY" or "SELL"
    qty: int  # absolute quantity; direction encoded in side
    price: float
    trade_time: datetime

    realized_pnl: Optional[float] = None
    fees: Optional[float] = None

    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None


# Normalize a broker trade id or fall back to a provided id.
def _clean_trade_id(raw: Optional[str], fallback: str) -> str:
    """
    Extract a clean broker trade id:
    - If raw contains composite delimiters (e.g. "51501|SELL|..."), take the first part.
    - Fallback to the provided string when raw is missing/blank.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return fallback
    if "|" in candidate:
        candidate = candidate.split("|", 1)[0]
    return candidate


# Parse a timestamp into a UTC datetime when possible.
def _parse_timestamp(value: Any) -> Optional[datetime]:
    """
    Parse a timestamp value into a UTC datetime.

    Returns None when parsing fails so callers can decide on a fallback.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value)
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


# Return default tenant id fallback from settings.
def _default_tenant_id() -> TenantId:
    settings = get_settings()
    value = TenantId(str(settings.hub_default_tenant_id))
    if getattr(settings, "enable_multi_hub", False):
        log_event(
            logger,
            event_type="TRADE_DEFAULT_TENANT_FALLBACK_USED",
            message="Using hub_default_tenant_id fallback while multi-hub is enabled.",
            tenant_id=value,
            level=logging.WARNING,
        )
    return value


# Return default broker account id fallback from settings.
def _default_broker_account_id() -> BrokerAccountId:
    settings = get_settings()
    fallback = getattr(settings, "hub_default_broker_account_id", "legacy-broker")
    value = BrokerAccountId(str(fallback))
    if getattr(settings, "enable_multi_hub", False):
        log_event(
            logger,
            event_type="TRADE_DEFAULT_BROKER_FALLBACK_USED",
            message="Using hub_default_broker_account_id fallback while multi-hub is enabled.",
            broker_account_id=value,
            level=logging.WARNING,
        )
    return value


# Return default strategy id fallback from settings.
def _default_strategy_id() -> StrategyId:
    settings = get_settings()
    fallback = getattr(settings, "hub_default_strategy_id", "__LEGACY_STRATEGY__")
    value = StrategyId(str(fallback))
    if getattr(settings, "enable_multi_hub", False):
        log_event(
            logger,
            event_type="TRADE_DEFAULT_STRATEGY_FALLBACK_USED",
            message="Using hub_default_strategy_id fallback while multi-hub is enabled.",
            strategy_id=value,
            level=logging.WARNING,
        )
    return value


# Convert a value to int with a fallback default.
def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return default


# Convert a value to float with a fallback default.
def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return default


# Build a TradeRecord from explicit fill fields.
def build_trade_record_from_fill(
    *,
    trade_id: Optional[str],
    hub_order_id: Optional[str],
    tenant_id: TenantId,
    broker_account_id: BrokerAccountId,
    strategy_id: StrategyId,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    trade_time: datetime,
    realized_pnl: Optional[float] = None,
    fees: Optional[float] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    entry_time: Optional[datetime] = None,
    exit_time: Optional[datetime] = None,
) -> TradeRecord:
    clean_side = side.upper()
    clean_qty = abs(_safe_int(qty, 0))
    price_value = _safe_float(price, 0.0)
    clean_id = _clean_trade_id(trade_id, fallback=hub_order_id or "unknown")

    return TradeRecord(
        trade_id=clean_id,
        hub_order_id=HubOrderId(hub_order_id) if hub_order_id else None,
        tenant_id=tenant_id,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=clean_side,
        qty=clean_qty,
        price=price_value,
        trade_time=trade_time,
        realized_pnl=realized_pnl,
        fees=fees,
        entry_price=_safe_float(entry_price, None) if entry_price is not None else None,
        exit_price=_safe_float(exit_price, None) if exit_price is not None else None,
        entry_time=entry_time,
        exit_time=exit_time,
    )


# Build a TradeRecord from a legacy execution payload.
def build_trade_record_from_exec(exec_record: Dict[str, Any]) -> TradeRecord:
    """
    Build a canonical TradeRecord from a legacy execution record emitted by RiskManager.
    """
    trade_id = str(exec_record.get("trade_id") or "").strip()
    if not trade_id:
        ts = exec_record.get("timestamp") or datetime.now(timezone.utc).isoformat()
        trade_id = "|".join(
            [
                str(
                    exec_record.get("order_id")
                    or exec_record.get("label")
                    or exec_record.get("tradingsymbol")
                    or exec_record.get("symboltoken")
                    or "unknown"
                ),
                str(exec_record.get("side") or "NA"),
                str(exec_record.get("qty") or 0),
                str(ts),
            ]
        )

    parsed_ts = _parse_timestamp(exec_record.get("timestamp"))
    trade_time = parsed_ts or datetime.now(timezone.utc)

    raw_symbol = (
        exec_record.get("tradingsymbol")
        or exec_record.get("symboltoken")
        or exec_record.get("underlying")
        or exec_record.get("label")
        or ""
    )

    return TradeRecord(
        trade_id=trade_id,
        hub_order_id=None,
        tenant_id=TenantId(str(exec_record.get("tenant_id") or _default_tenant_id())),
        broker_account_id=BrokerAccountId(
            str(exec_record.get("broker_account_id") or _default_broker_account_id())
        ),
        strategy_id=StrategyId(
            str(
                exec_record.get("strategy_id")
                or exec_record.get("strategy_name")
                or _default_strategy_id()
            )
        ),
        symbol=str(raw_symbol),
        side=str(exec_record.get("side") or "").upper() or "BUY",
        qty=abs(_safe_int(exec_record.get("qty"), 0)),
        price=_safe_float(exec_record.get("price"), 0.0),
        trade_time=trade_time,
        realized_pnl=None,
        fees=None,
        entry_price=_safe_float(exec_record.get("price"), 0.0),
        exit_price=_safe_float(exec_record.get("price"), 0.0),
        entry_time=trade_time,
        exit_time=trade_time,
    )


# Build a TradeRecord from an aggregated legacy trade payload.
def build_trade_record_from_legacy_trade(
    trade: Dict[str, Any], *, trade_id: Optional[str] = None
) -> TradeRecord:
    """
    Build a canonical TradeRecord from a legacy aggregated trade payload (entry/exit).
    """
    fallback_trade_id = trade_id or trade.get("trade_id")
    exit_ts = trade.get("exit_ts")
    entry_ts = trade.get("entry_ts")
    computed_id = fallback_trade_id or "|".join(
        [
            str(trade.get("label") or trade.get("tradingsymbol") or "legacy_trade"),
            str(trade.get("side") or "NA"),
            str(trade.get("qty") or 0),
            str(exit_ts or entry_ts or datetime.now(timezone.utc).isoformat()),
        ]
    )

    entry_time = _parse_timestamp(entry_ts)
    exit_time = _parse_timestamp(exit_ts)
    trade_time = exit_time or entry_time or datetime.now(timezone.utc)
    entry_price = trade.get("entry_price")
    exit_price = trade.get("exit_price")

    raw_symbol = (
        trade.get("tradingsymbol")
        or trade.get("label")
        or trade.get("underlying")
        or ""
    )

    return TradeRecord(
        trade_id=str(computed_id),
        hub_order_id=None,
        tenant_id=TenantId(str(trade.get("tenant_id") or _default_tenant_id())),
        broker_account_id=BrokerAccountId(
            str(trade.get("broker_account_id") or _default_broker_account_id())
        ),
        strategy_id=StrategyId(
            str(
                trade.get("strategy_id")
                or trade.get("strategy_name")
                or _default_strategy_id()
            )
        ),
        symbol=str(raw_symbol),
        side=str(trade.get("side") or "").upper() or "SELL",
        qty=abs(_safe_int(trade.get("qty"), 0)),
        price=_safe_float(exit_price or entry_price, 0.0),
        trade_time=trade_time,
        realized_pnl=(
            _safe_float(trade.get("pnl"), None)
            if trade.get("pnl") is not None
            else (
                _safe_float(trade.get("realized_pnl"), None)
                if trade.get("realized_pnl") is not None
                else None
            )
        ),
        fees=(
            _safe_float(trade.get("fees"), None)
            if trade.get("fees") is not None
            else None
        ),
        entry_price=_safe_float(entry_price, None) if entry_price is not None else None,
        exit_price=_safe_float(exit_price, None) if exit_price is not None else None,
        entry_time=entry_time,
        exit_time=exit_time,
    )


# Convert a TradeRecord into a BigQuery row dict.
def trade_record_to_bq_row(rec: TradeRecord) -> dict[str, Any]:
    """
    Convert a TradeRecord to a flat dict compatible with the BigQuery schema.
    """
    return {
        "trade_id": rec.trade_id,
        "hub_order_id": str(rec.hub_order_id) if rec.hub_order_id else None,
        "tenant_id": str(rec.tenant_id),
        "broker_account_id": str(rec.broker_account_id),
        "strategy_id": str(rec.strategy_id),
        "symbol": rec.symbol,
        "side": rec.side,
        "qty": rec.qty,
        "price": rec.price,
        "trade_time": rec.trade_time.isoformat() if rec.trade_time else None,
        "realized_pnl": rec.realized_pnl,
        "fees": rec.fees,
        "entry_price": rec.entry_price,
        "exit_price": rec.exit_price,
        "entry_time": rec.entry_time.isoformat() if rec.entry_time else None,
        "exit_time": rec.exit_time.isoformat() if rec.exit_time else None,
    }


__all__ = [
    "TradeRecord",
    "build_trade_record_from_fill",
    "build_trade_record_from_exec",
    "build_trade_record_from_legacy_trade",
    "trade_record_to_bq_row",
]
