import asyncio
import importlib
import types

import pytest

from app.brokers import angel_client as angel_mod
from app.brokers.angel_client import AngelBrokerClient
from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.brokers.secrets import AngelSecrets
from app.tenants.models import BrokerAccountModel
from app.core.identifiers import BrokerAccountId, TenantId

MODULE_PATH = "app.brokers.angel_client"
CALLABLE_NAMES = ["AngelBrokerClient"]


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


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


class _StubOrderClient:
    def __init__(self) -> None:
        self.last_kwargs = None
        self.last_cancel_kwargs = None

    def place_order(self, **kwargs):
        self.last_kwargs = kwargs
        return {"status": True, "orderid": "OID123", "message": "ok", "data": {}}

    def cancel_order(self, **kwargs):
        self.last_cancel_kwargs = kwargs
        return {"status": True, "message": "ok", "data": {"orderid": kwargs.get("order_id")}}


def _raw_order_row(*, status: str) -> dict:
    return {
        "orderid": "OID-TERM-1",
        "tradingsymbol": "NIFTY17FEB2625750CE",
        "transactiontype": "BUY",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "quantity": "50",
        "filledshares": "50",
        "averageprice": "101.25",
        "price": "0",
        "exchange": "NFO",
        "updatetime": "2026-03-09T10:00:00+00:00",
        "orderstatus": status,
    }


def test_place_order_uses_exchange_and_token_when_provided():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    stub = _StubOrderClient()
    client._order_client = stub  # inject stub to avoid network calls

    order = OrderRequest(
        symbol="NIFTY24DEC25000CE",
        quantity=50,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        exchange="NFO",
        symbol_token="123456",
    )

    resp = asyncio.run(client.place_order(order))

    assert stub.last_kwargs["exchange"] == "NFO"
    assert stub.last_kwargs["symboltoken"] == "123456"
    assert stub.last_kwargs["tradingsymbol"] == order.symbol
    assert resp.broker_order_id == "OID123"


def test_place_order_falls_back_when_exchange_or_token_missing():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    stub = _StubOrderClient()
    client._order_client = stub

    order = OrderRequest(
        symbol="SBIN",
        quantity=10,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    asyncio.run(client.place_order(order))

    assert stub.last_kwargs["exchange"] == "NSE"
    assert stub.last_kwargs["symboltoken"] == "SBIN"


def test_cancel_order_uses_order_client_and_variety():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    stub = _StubOrderClient()
    client._order_client = stub

    resp = asyncio.run(client.cancel_order("OID321", symbol="SBIN", variety="NORMAL"))

    assert stub.last_cancel_kwargs["order_id"] == "OID321"
    assert stub.last_cancel_kwargs["symbol"] == "SBIN"
    assert stub.last_cancel_kwargs["variety"] == "NORMAL"
    assert resp.broker_order_id == "OID321"
    assert resp.status == "CANCELLED"


@pytest.mark.parametrize("status", ["COMPLETE", "REJECTED", "CANCELLED"])
def test_parse_orders_preserves_terminal_rows_when_snapshot_flag_enabled(
    monkeypatch,
    status: str,
):
    monkeypatch.setenv("ORDER_SNAPSHOT_PRESERVE_TERMINAL", "true")
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())

    orders = client._parse_orders({"data": [_raw_order_row(status=status)]})

    assert len(orders) == 1
    assert orders[0].order_id == "OID-TERM-1"
    assert orders[0].status == status


@pytest.mark.parametrize("status", ["COMPLETE", "REJECTED", "CANCELLED"])
def test_parse_orders_keeps_legacy_terminal_drop_when_snapshot_flag_disabled(
    monkeypatch,
    status: str,
):
    monkeypatch.setenv("ORDER_SNAPSHOT_PRESERVE_TERMINAL", "false")
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())

    orders = client._parse_orders({"data": [_raw_order_row(status=status)]})

    assert orders == []


def test_place_order_retries_timeout_then_succeeds(monkeypatch):
    class _FlakyOrderClient:
        def __init__(self) -> None:
            self.calls = 0

        def place_order(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return {"status": True, "orderid": "OID_RETRY_OK", "message": "ok", "data": {}}

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setenv("ANGEL_ORDER_PLACE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("ANGEL_ORDER_PLACE_RETRY_BASE_SECONDS", "0.1")
    monkeypatch.setattr(angel_mod.asyncio, "sleep", _no_sleep)

    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    stub = _FlakyOrderClient()
    client._order_client = stub
    order = OrderRequest(
        symbol="NIFTY24DEC25000CE",
        quantity=50,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        exchange="NFO",
        symbol_token="123456",
    )

    resp = asyncio.run(client.place_order(order))

    assert stub.calls == 2
    assert resp.broker_order_id == "OID_RETRY_OK"


def test_place_order_timeout_returns_failed_instead_of_raising(monkeypatch):
    class _TimeoutOrderClient:
        def place_order(self, **_kwargs):
            raise TimeoutError("timed out")

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setenv("ANGEL_ORDER_PLACE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("ANGEL_ORDER_PLACE_RETRY_BASE_SECONDS", "0.1")
    monkeypatch.setattr(angel_mod.asyncio, "sleep", _no_sleep)

    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _TimeoutOrderClient()
    order = OrderRequest(
        symbol="SBIN",
        quantity=10,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    resp = asyncio.run(client.place_order(order))

    assert resp.status == "FAILED"
    assert "place_order_failed" in resp.message
    assert "timeout" in resp.message.lower()


def test_classify_fetch_exception_treats_ag7002_as_blocked():
    failure = angel_mod._classify_fetch_exception(
        RuntimeError(
            "Order failed: {'success': False, 'message': 'Access denied: "
            "Unregistered IP address. Register your IP before retrying.', "
            "'errorCode': 'AG7002', 'data': ''}"
        )
    )

    assert failure.kind == angel_mod.FetchFailureKind.BLOCKED


def test_place_order_unregistered_ip_returns_rejected_and_sets_cooldown():
    class _BlockedOrderClient:
        def place_order(self, **_kwargs):
            raise RuntimeError(
                "Order failed: {'success': False, 'message': 'Access denied: "
                "Unregistered IP address. Register your IP before retrying.', "
                "'errorCode': 'AG7002', 'data': ''}"
            )

    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _BlockedOrderClient()
    order = OrderRequest(
        symbol="SBIN",
        quantity=10,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    resp = asyncio.run(client.place_order(order))

    assert resp.status == "REJECTED"
    assert resp.broker_order_id == "BLOCKED"
    assert "Unregistered IP address" in resp.message
    assert client._orders_blocked_count == 1
    assert client._orders_blocked_until > 0.0
