"""
Naive in-memory simulated broker.

Used for PAPER mode before we wire to actual broker APIs.
"""

from __future__ import annotations

import logging

from app.brokers.base import (
    Balance,
    BrokerClient,
    OrderRequest,
    OrderResponse,
    OrderStatus,
    Position,
)
from app.brokers.positions_types import PositionsFetchResult, PositionsStatus
from app.tenants.models import BrokerAccountModel

logger = logging.getLogger(__name__)


class SimulatedBrokerClient(BrokerClient):
    def __init__(self, account: BrokerAccountModel) -> None:
        self._account = account
        self._balance = Balance(
            available=1_00_00_000.0, utilized=0.0, total=1_00_00_000.0
        )
        self._positions: dict[str, Position] = {}

    async def login(self) -> None:
        logger.info(
            "SimulatedBrokerClient: login noop for broker_account_id=%s",
            self._account.broker_account_id,
        )

    async def get_balance(self) -> Balance:
        return self._balance

    async def get_positions(self) -> PositionsFetchResult:
        return PositionsFetchResult(
            status=PositionsStatus.OK,
            positions=list(self._positions.values()),
            reason="ok",
        )

    async def get_orders(self) -> List[OrderStatus]:
        return []

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        price = request.limit_price or 0.0
        qty_delta = (
            request.quantity if request.side.value == "BUY" else -request.quantity
        )

        pos = self._positions.get(request.symbol)
        if pos is None:
            pos = Position(
                symbol=request.symbol,
                quantity=qty_delta,
                avg_price=price,
                product_type=request.product_type,
            )
        else:
            new_qty = pos.quantity + qty_delta
            if new_qty == 0:
                pos = Position(
                    symbol=request.symbol,
                    quantity=0,
                    avg_price=0.0,
                    product_type=request.product_type,
                )
            else:
                pos = Position(
                    symbol=request.symbol,
                    quantity=new_qty,
                    avg_price=price,
                    product_type=request.product_type,
                )

        self._positions[request.symbol] = pos

        return OrderResponse(
            broker_order_id="SIM-ORDER",
            status="FILLED",
            message="Simulated fill",
            filled_quantity=request.quantity,
            average_price=price,
        )

    async def cancel_order(
        self,
        broker_order_id: str,
        *,
        symbol: str | None = None,
        variety: str | None = None,
    ) -> OrderResponse:
        _ = variety
        return OrderResponse(
            broker_order_id=str(broker_order_id or "SIM-ORDER"),
            status="CANCELLED",
            message=f"Simulated cancel for {symbol or '-'}",
            filled_quantity=0,
            average_price=None,
        )


__all__ = ["SimulatedBrokerClient"]
