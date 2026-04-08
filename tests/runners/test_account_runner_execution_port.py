import asyncio

from app.brokers.base import (
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.core.identifiers import BrokerAccountId, TenantId
from app.data.state_store import StateStore
from app.hub.account_runner import AccountRunner
from app.tenants.models import BrokerAccountModel


class _BrokerClientNoPlace:
    async def login(self):
        return None

    async def get_balance(self):
        return None

    async def get_positions(self):
        return []

    async def get_orders(self):
        return []

    async def place_order(self, _request):
        raise AssertionError("broker_client.place_order should not be called")


class _ExecutionPortStub:
    def __init__(self) -> None:
        self.calls: list[OrderRequest] = []

    async def place(self, request: OrderRequest) -> OrderResponse:
        self.calls.append(request)
        return OrderResponse(
            broker_order_id="EXEC-1",
            status="FILLED",
            message="ok",
            filled_quantity=request.quantity,
            average_price=100.0,
        )

    async def cancel(self, broker_order_id: str, *, symbol=None) -> OrderResponse:
        return OrderResponse(
            broker_order_id=broker_order_id,
            status="CANCELLED",
            message="ok",
            filled_quantity=0,
            average_price=None,
        )

    async def status(self, broker_order_id: str):
        return None


class _BrokerClientWithCancelableOrders(_BrokerClientNoPlace):
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str | None, str | None]] = []

    async def get_orders(self):
        return [
            OrderStatus(
                order_id="OID-OPEN-1",
                symbol="SBIN-EQ",
                side="BUY",
                status="OPEN",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=0,
                exchange="NSE",
                variety="NORMAL",
            ),
            OrderStatus(
                order_id="OID-COMPLETE-1",
                symbol="SBIN-EQ",
                side="BUY",
                status="COMPLETE",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=1,
                exchange="NSE",
                variety="NORMAL",
            ),
            OrderStatus(
                order_id="OID-DELIVERY-1",
                symbol="SBIN-EQ",
                side="BUY",
                status="OPEN",
                order_type="MARKET",
                product_type="DELIVERY",
                quantity=1,
                filled_quantity=0,
                exchange="NSE",
                variety="NORMAL",
            ),
            OrderStatus(
                order_id="OID-MCX-1",
                symbol="NG_FUT",
                side="SELL",
                status="TRIGGER PENDING",
                order_type="SLM",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=0,
                exchange="MCX",
                variety="STOPLOSS",
            ),
        ]

    async def cancel_order(self, broker_order_id: str, *, symbol=None, variety=None):
        self.cancel_calls.append((broker_order_id, symbol, variety))
        return OrderResponse(
            broker_order_id=broker_order_id,
            status="CANCELLED",
            message="ok",
            filled_quantity=0,
            average_price=None,
        )


def _dummy_account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id=BrokerAccountId("ba_exec_test"),
        tenant_id=TenantId("tenant_exec_test"),
        broker_type="angel",
        display_name="test",
        client_code="test_client",
        secret_ref="secret/ref",
    )


def test_runner_place_order_uses_execution_port_only():
    account = _dummy_account()
    state_store = StateStore()
    execution_port = _ExecutionPortStub()
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BrokerClientNoPlace(),
        execution_port=execution_port,
        runtime_mode="LIVE",
        state_store=state_store,
        poll_interval_seconds=1.0,
    )

    order = OrderRequest(
        symbol="SBIN-EQ",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )

    response = asyncio.run(runner.place_order(order))

    assert response.status == "FILLED"
    assert len(execution_port.calls) == 1
    assert state_store.get_last_order_response(account.broker_account_id) is not None


def test_runner_cancel_open_intraday_orders_filters_and_cancels():
    account = _dummy_account()
    state_store = StateStore()
    execution_port = _ExecutionPortStub()
    broker = _BrokerClientWithCancelableOrders()
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=broker,
        execution_port=execution_port,
        runtime_mode="LIVE",
        state_store=state_store,
        poll_interval_seconds=1.0,
    )

    summary = asyncio.run(
        runner.cancel_open_intraday_orders(exclude_exchanges={"MCX"}, refresh=True)
    )

    assert summary["attempted"] == 1
    assert summary["cancelled"] == 1
    assert summary["failed"] == 0
    assert summary["unsupported"] == 0
    assert summary["skipped"] >= 2
    assert broker.cancel_calls == [("OID-OPEN-1", "SBIN-EQ", "NORMAL")]


def test_runner_uses_shadow_execution_adapter_when_runtime_mode_shadow():
    account = _dummy_account()
    state_store = StateStore()
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=_BrokerClientNoPlace(),
        execution_port=None,
        runtime_mode="SHADOW",
        state_store=state_store,
        poll_interval_seconds=1.0,
    )
    assert type(runner._execution_port).__name__ == "ShadowExecutionAdapter"
