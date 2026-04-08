import importlib
import types

import pytest

from app.brokers.base import (
    OrderPurpose,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)

MODULE_PATH = "app.brokers.execution"
CALLABLE_NAMES = [
    "ExecutionPort",
    "BrokerExecutionAdapter",
    "AngelExecutionAdapter",
    "SimulatedExecutionAdapter",
    "ShadowExecutionAdapter",
    "create_execution_port",
]


def _order_request() -> OrderRequest:
    return OrderRequest(
        symbol="SBIN-EQ",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )


class _StubBrokerClient:
    def __init__(self) -> None:
        self.placed: list[OrderRequest] = []
        self.orders: list[OrderStatus] = [
            OrderStatus(
                order_id="OID-1",
                symbol="SBIN-EQ",
                side="BUY",
                status="OPEN",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=0,
            )
        ]

    async def login(self) -> None:
        return None

    async def get_balance(self):
        return None

    async def get_positions(self):
        return []

    async def get_orders(self):
        return self.orders

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        self.placed.append(request)
        return OrderResponse(
            broker_order_id="OID-1",
            status="PLACED",
            message="ok",
            filled_quantity=0,
            average_price=None,
        )


class _CancelableBrokerClient(_StubBrokerClient):
    async def cancel_order(self, broker_order_id: str, *, symbol=None) -> OrderResponse:
        return OrderResponse(
            broker_order_id=broker_order_id,
            status="CANCELLED",
            message=f"cancelled:{symbol or ''}",
            filled_quantity=0,
            average_price=None,
        )

    async def get_order_status(self, broker_order_id: str) -> OrderStatus | None:
        for order in self.orders:
            if order.order_id == broker_order_id:
                return order
        return None


class _RetryOnceBrokerClient(_StubBrokerClient):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("temporary timeout while placing order")
        return await super().place_order(request)


class _RetryableFailureBrokerClient(_StubBrokerClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def place_order(self, request: OrderRequest) -> OrderResponse:
        self.calls += 1
        return OrderResponse(
            broker_order_id="",
            status="FAILED",
            message="timeout contacting broker",
            filled_quantity=0,
            average_price=None,
        )


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.anyio
async def test_execution_adapter_place_and_status_fallback():
    mod = importlib.import_module(MODULE_PATH)
    broker = _StubBrokerClient()
    adapter = mod.BrokerExecutionAdapter(broker_client=broker)

    response = await adapter.place(_order_request())
    status = await adapter.status("OID-1")

    assert response.status == "PLACED"
    assert len(broker.placed) == 1
    assert status is not None
    assert status.order_id == "OID-1"
    assert status.status == "OPEN"


@pytest.mark.anyio
async def test_execution_adapter_cancel_and_status_with_broker_primitives():
    mod = importlib.import_module(MODULE_PATH)
    broker = _CancelableBrokerClient()
    adapter = mod.BrokerExecutionAdapter(broker_client=broker)

    cancel_resp = await adapter.cancel("OID-1", symbol="SBIN-EQ")
    status = await adapter.status("OID-1")

    assert cancel_resp.status == "CANCELLED"
    assert "SBIN-EQ" in cancel_resp.message
    assert status is not None
    assert status.order_id == "OID-1"


@pytest.mark.anyio
async def test_execution_adapter_cancel_unsupported_without_broker_primitive():
    mod = importlib.import_module(MODULE_PATH)
    broker = _StubBrokerClient()
    adapter = mod.BrokerExecutionAdapter(broker_client=broker)

    cancel_resp = await adapter.cancel("OID-404")

    assert cancel_resp.status == "UNSUPPORTED"
    assert "cancel_not_supported" in cancel_resp.message


@pytest.mark.anyio
async def test_execution_adapter_retries_retryable_place_exception_then_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    broker = _RetryOnceBrokerClient()
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    adapter = mod.BrokerExecutionAdapter(
        broker_client=broker,
        retry_max_attempts=2,
        retry_base_seconds=0.25,
        retry_max_seconds=1.0,
        sleep_fn=_sleep,
    )

    response = await adapter.place(_order_request())

    assert response.status == "PLACED"
    assert broker.attempts == 2
    assert broker.placed == [_order_request()]
    assert sleeps == [0.25]


@pytest.mark.anyio
async def test_execution_adapter_opens_circuit_after_retryable_place_failures():
    mod = importlib.import_module(MODULE_PATH)
    broker = _RetryableFailureBrokerClient()
    clock = {"t": 100.0}

    async def _sleep(delay: float) -> None:
        clock["t"] += delay

    adapter = mod.BrokerExecutionAdapter(
        broker_client=broker,
        retry_max_attempts=2,
        retry_base_seconds=1.0,
        retry_max_seconds=1.0,
        circuit_failure_threshold=1,
        circuit_base_seconds=30.0,
        circuit_max_seconds=30.0,
        sleep_fn=_sleep,
        now_mono=lambda: float(clock["t"]),
    )

    with pytest.raises(mod.ExecutionRetryableResponseError):
        await adapter.place(_order_request())

    assert broker.calls == 2

    with pytest.raises(mod.ExecutionCircuitOpenError, match="execution_circuit_open:place"):
        await adapter.place(_order_request())

    assert broker.calls == 2


@pytest.mark.anyio
async def test_execution_adapter_allows_exit_orders_while_entry_circuit_is_open():
    mod = importlib.import_module(MODULE_PATH)
    broker = _RetryableFailureBrokerClient()
    clock = {"t": 100.0}

    async def _sleep(delay: float) -> None:
        clock["t"] += delay

    adapter = mod.BrokerExecutionAdapter(
        broker_client=broker,
        retry_max_attempts=1,
        circuit_failure_threshold=1,
        circuit_base_seconds=30.0,
        circuit_max_seconds=30.0,
        sleep_fn=_sleep,
        now_mono=lambda: float(clock["t"]),
    )

    with pytest.raises(mod.ExecutionRetryableResponseError):
        await adapter.place(_order_request())

    async def _place_exit(request: OrderRequest) -> OrderResponse:
        return OrderResponse(
            broker_order_id="OID-EXIT-1",
            status="PLACED",
            message="ok",
            filled_quantity=0,
            average_price=None,
        )

    broker.place_order = _place_exit  # type: ignore[method-assign]
    exit_request = _order_request()
    exit_request.purpose = OrderPurpose.EXIT

    response = await adapter.place(exit_request)

    assert response.broker_order_id == "OID-EXIT-1"


def test_create_execution_port_is_broker_type_driven():
    mod = importlib.import_module(MODULE_PATH)
    broker = _StubBrokerClient()

    angel_port = mod.create_execution_port(
        broker_client=broker,
        broker_type="angel",
    )
    simulated_port = mod.create_execution_port(
        broker_client=broker,
        broker_type="simulated",
    )
    shadow_port = mod.create_execution_port(
        broker_client=broker,
        broker_type="shadow",
    )
    default_port = mod.create_execution_port(
        broker_client=broker,
        broker_type="unknown",
    )

    assert type(angel_port).__name__ == "AngelExecutionAdapter"
    assert type(simulated_port).__name__ == "SimulatedExecutionAdapter"
    assert type(shadow_port).__name__ == "ShadowExecutionAdapter"
    assert type(default_port).__name__ == "BrokerExecutionAdapter"
