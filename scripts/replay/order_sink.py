"""Replay-local order sink that keeps strategy replay isolated from live Hub."""

from __future__ import annotations

from typing import Optional

from app.brokers.base import OrderRequest, OrderResponse
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from scripts.replay.mock_execution import MockExecutionRecorder


class ReplayOrderSink:
    """Deterministic replay executor that fills orders at the current bar close."""

    def __init__(self, *, recorder: MockExecutionRecorder) -> None:
        self._recorder = recorder

    def place_order(
        self,
        *,
        strategy_id: StrategyId,
        order_req: OrderRequest,
        tenant_id: Optional[TenantId] = None,
        broker_account_id: Optional[BrokerAccountId] = None,
    ) -> OrderResponse:
        del tenant_id, broker_account_id
        return self._recorder.record_order(
            strategy_id=str(strategy_id),
            order_req=order_req,
        )


__all__ = ["ReplayOrderSink"]
