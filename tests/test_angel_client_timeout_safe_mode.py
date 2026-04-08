import asyncio
from types import SimpleNamespace

import pytest

from app.brokers import angel_client as angel_client_module
from app.brokers.angel_client import AngelBlockedResponseError, AngelBrokerClient
from app.brokers.base import OrderRequest, OrderSide, OrderType, ProductType, TimeInForce
from app.orders.order_outbox import OUTBOX_STATUS_PENDING, _outbox_status_from_response


class _FakeOrderClient:
    def __init__(self, side_effects):
        self._side_effects = list(side_effects)
        self.calls = 0

    def place_order(self, **kwargs):
        _ = kwargs
        self.calls += 1
        effect = self._side_effects[self.calls - 1]
        if isinstance(effect, BaseException):
            raise effect
        return effect


def _build_client(fake_order_client: _FakeOrderClient) -> AngelBrokerClient:
    client = AngelBrokerClient(
        account=SimpleNamespace(broker_account_id="A1", tenant_id="T1"),
        secrets=SimpleNamespace(client_code="client-code"),
    )
    client._order_client = fake_order_client
    client._order_place_max_attempts = 2
    client._order_place_retry_base_seconds = 0.0
    return client


def _build_request() -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY24APR22000CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        tag="tag-1",
        idempotency_key="idem-1",
    )


@pytest.mark.asyncio
async def test_place_order_timeout_safe_mode_off_retries(monkeypatch):
    fake_order_client = _FakeOrderClient(
        [
            asyncio.TimeoutError("timed out"),
            {"orderid": "OID-123", "status": "OPEN", "message": "accepted"},
        ]
    )
    client = _build_client(fake_order_client)
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        AngelBrokerClient,
        "_order_place_timeout_safe_mode_enabled",
        staticmethod(lambda: False),
    )
    monkeypatch.setattr(angel_client_module.asyncio, "sleep", _fake_sleep)

    response = await client.place_order(_build_request())

    assert fake_order_client.calls == 2
    assert sleep_calls
    assert response.status == "OPEN"
    assert response.broker_order_id == "OID-123"


@pytest.mark.asyncio
async def test_place_order_timeout_safe_mode_on_returns_pending(monkeypatch):
    fake_order_client = _FakeOrderClient([asyncio.TimeoutError("timed out")])
    client = _build_client(fake_order_client)
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        AngelBrokerClient,
        "_order_place_timeout_safe_mode_enabled",
        staticmethod(lambda: True),
    )
    monkeypatch.setattr(angel_client_module.asyncio, "sleep", _fake_sleep)

    response = await client.place_order(_build_request())

    assert fake_order_client.calls == 1
    assert sleep_calls == []
    assert response.status == "PENDING"
    assert response.broker_order_id == "PENDING_TIMEOUT_RECONCILIATION"
    assert "pending_reconciliation" in response.message
    assert response.requested_quantity == 65
    assert response.details == {
        "pending_reconciliation": True,
        "reconciliation_reason": "broker_place_timeout",
    }


@pytest.mark.asyncio
async def test_place_order_timeout_safe_mode_keeps_blocked_behavior(monkeypatch):
    fake_order_client = _FakeOrderClient([AngelBlockedResponseError("blocked by broker")])
    client = _build_client(fake_order_client)

    monkeypatch.setattr(
        AngelBrokerClient,
        "_order_place_timeout_safe_mode_enabled",
        staticmethod(lambda: True),
    )

    response = await client.place_order(_build_request())

    assert fake_order_client.calls == 1
    assert response.status == "REJECTED"
    assert response.broker_order_id == "BLOCKED"
    assert "blocked by broker" in response.message


def test_pending_reconciliation_response_stays_pending_in_outbox():
    response = angel_client_module.OrderResponse(
        broker_order_id="PENDING_TIMEOUT_RECONCILIATION",
        status="PENDING",
        message="place_order_timeout_pending_reconciliation",
        requested_quantity=65,
        details={
            "pending_reconciliation": True,
            "reconciliation_reason": "broker_place_timeout",
        },
    )

    assert _outbox_status_from_response(response) == OUTBOX_STATUS_PENDING
