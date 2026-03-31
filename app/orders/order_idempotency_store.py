"""Idempotency store for OrderRouter claim-before-submit behavior."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import threading
from typing import Optional

from app.brokers.base import OrderResponse
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId


def _scope_key(
    *,
    tenant_id: TenantId,
    broker_account_id: BrokerAccountId,
    strategy_id: StrategyId,
    idempotency_key: str,
) -> tuple[str, str, str, str]:
    return (
        str(tenant_id),
        str(broker_account_id),
        str(strategy_id),
        str(idempotency_key),
    )


def _clone_response(response: OrderResponse) -> OrderResponse:
    return replace(response)


class InMemoryOrderIdempotencyStore:
    """Thread-safe in-memory idempotency store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: dict[tuple[str, str, str, str], datetime] = {}
        self._responses: dict[tuple[str, str, str, str], OrderResponse] = {}

    def claim(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        idempotency_key: str,
    ) -> tuple[bool, Optional[OrderResponse]]:
        key = _scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            existing = self._responses.get(key)
            if existing is not None:
                return False, _clone_response(existing)
            if key in self._claims:
                return False, None
            self._claims[key] = datetime.now(timezone.utc)
            return True, None

    def complete(
        self,
        *,
        tenant_id: TenantId,
        broker_account_id: BrokerAccountId,
        strategy_id: StrategyId,
        idempotency_key: str,
        response: OrderResponse,
    ) -> None:
        key = _scope_key(
            tenant_id=tenant_id,
            broker_account_id=broker_account_id,
            strategy_id=strategy_id,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            if key in self._responses:
                return
            self._claims[key] = datetime.now(timezone.utc)
            self._responses[key] = _clone_response(response)


__all__ = ["InMemoryOrderIdempotencyStore"]
