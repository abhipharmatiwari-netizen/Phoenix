from __future__ import annotations

import asyncio

import pytest

from app.brokers.angel_client import AngelBrokerClient
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.brokers.secrets import AngelSecrets
from app.core.identifiers import BrokerAccountId, TenantId
from app.tenants.models import BrokerAccountModel


def _dummy_account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id=BrokerAccountId("ba_test"),
        tenant_id=TenantId("tenant_test"),
        broker_type="angel",
        display_name="test",
        client_code="test_client",
        secret_ref="secret/ref",
    )


def _dummy_secrets() -> AngelSecrets:
    return AngelSecrets(
        api_key="key",
        api_secret="secret",
        client_code="client",
        pin="1234",
        totp_secret="BASE32",
        client_local_ip="127.0.0.1",
        client_public_ip="127.0.0.1",
        mac_address="aa:bb:cc:dd:ee:ff",
    )


class _StubOrderClient:
    def get_rms(self):
        return {"status": True, "data": {"availablecash": "1000", "net": "1000", "utilisedmargin": "0"}}

    def get_positions(self):
        return {"status": True, "data": []}

    def get_order_book(self):
        return {
            "status": True,
            "data": [
                {
                    "orderid": "OID1",
                    "tradingsymbol": "SBIN",
                    "transactiontype": "BUY",
                    "status": "COMPLETE",
                    "ordertype": "MARKET",
                    "producttype": "INTRADAY",
                    "quantity": 1,
                    "filledshares": 1,
                    "averageprice": 100.0,
                    "price": 100.0,
                    "exchange": "NSE",
                }
            ],
        }

    def place_order(self, **_kwargs):
        return {"status": True, "orderid": "OID-PLACE", "message": "ok", "data": {}}


@pytest.mark.anyio
async def test_async_methods_use_to_thread(monkeypatch):
    calls: list[str] = []

    async def _fake_to_thread(fn, *args, **kwargs):
        calls.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StubOrderClient()

    await client.get_balance()
    await client.get_positions()
    await client.get_orders()
    await client.place_order(
        OrderRequest(
            symbol="SBIN",
            quantity=1,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
        )
    )

    assert "get_rms" in calls
    assert "get_positions" in calls
    assert "get_order_book" in calls
    assert "place_order" in calls
