"""
Broker Adapter Layer: base types and interface.

Defines a BrokerClient abstraction that we can implement for:
- Angel One (real broker)
- Simulated broker (for PAPER mode)
- Future brokers (Zerodha, Upstox, ICICI, etc.)

This module intentionally contains only light protocol/DTO definitions, so it can
be imported from anywhere without heavy dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from app.brokers.positions_types import PositionsFetchResult

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SLM = "SLM"


class ProductType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"
    CARRY_FORWARD = "CARRY_FORWARD"


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"


class OrderPurpose(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ADJUST = "ADJUST"


@dataclass
class OrderRequest:
    symbol: str
    quantity: int
    side: OrderSide
    order_type: OrderType
    product_type: ProductType
    time_in_force: TimeInForce
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    tag: Optional[str] = None
    purpose: Optional[OrderPurpose] = None
    exchange: Optional[str] = None
    symbol_token: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Explicit bypass for system safety exits (EOD/intraday strict).
    position_ownership_bypass: bool = False
    tick_size: Optional[float] = None
    position_label: Optional[str] = None
    strategy_context: Optional[Dict[str, Any]] = None
    # ── Exit-context fields (P1-9) ──────────────────────────────
    # e.g. "STOPLOSS", "TARGET", "TRAIL_SL", "TIME_EXIT", "SIGNAL",
    #      "EOD", "KILL_SWITCH", "FORCED_FLATTEN"
    exit_reason: Optional[str] = None
    position_id: Optional[str] = None          # internal position reference
    contract_key_ref: Optional[str] = None     # serialized contract key for audit trail
    strategy_id: Optional[str] = None          # originating strategy
    account_id: Optional[str] = None           # canonical account scope for exit/audit

    @property
    def is_exit_order(self) -> bool:
        try:
            return self.purpose == OrderPurpose.EXIT
        except Exception:
            return False


@dataclass
class OrderResponse:
    broker_order_id: str
    status: str
    message: str
    filled_quantity: int = 0
    average_price: Optional[float] = None
    # Broker quantity submitted after router preflight/capital sizing.
    requested_quantity: Optional[int] = None
    execution_mode: Optional[str] = None
    virtual: Optional[bool] = None
    details: Optional[Dict[str, Any]] = None


@dataclass
class OrderStatus:
    order_id: str
    symbol: str
    side: str
    status: str
    order_type: str
    product_type: str
    quantity: int
    filled_quantity: int
    average_price: Optional[float] = None
    price: Optional[float] = None
    exchange: Optional[str] = None
    variety: Optional[str] = None
    updated_at: Optional[str] = None
    execution_mode: Optional[str] = None
    virtual: Optional[bool] = None


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: float
    product_type: ProductType
    # When positions are synced from the broker, we usually do NOT get a true
    # exchange fill timestamp. The hub will populate this as the first time the
    # position was observed during polling so the dashboard can show an
    # approximate entry time.
    entry_ts: Optional[str] = None
    execution_mode: Optional[str] = None
    virtual: Optional[bool] = None


@dataclass
class Balance:
    available: float
    utilized: float
    total: float


class BrokerClient(Protocol):
    """
    Minimal broker interface used by the hub and strategies.

    Concrete implementations wrap actual broker SDKs or simulated brokers.
    """

    async def login(self) -> None: ...

    async def get_balance(self) -> Balance: ...

    async def get_positions(self) -> PositionsFetchResult: ...

    async def get_orders(self) -> List[OrderStatus]: ...

    async def place_order(self, request: OrderRequest) -> OrderResponse: ...


__all__ = [
    "OrderSide",
    "OrderType",
    "ProductType",
    "TimeInForce",
    "OrderPurpose",
    "OrderRequest",
    "OrderResponse",
    "OrderStatus",
    "Position",
    "Balance",
    "PositionsFetchResult",
    "BrokerClient",
]
