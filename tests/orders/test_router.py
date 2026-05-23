import importlib
import types
import asyncio
from types import SimpleNamespace
import pytest
from app.brokers.base import (
    Balance,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)

MODULE_PATH = "app.orders.router"
CALLABLE_NAMES = ['OrderRouter']


def test_import_module_succeeds():
    mod = importlib.import_module(MODULE_PATH)
    assert isinstance(mod, types.ModuleType)


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_module_defines_callable(name):
    mod = importlib.import_module(MODULE_PATH)
    assert hasattr(mod, name), f"Module {MODULE_PATH} should define {name}"


@pytest.mark.parametrize("name", CALLABLE_NAMES)
def test_callable_behaviour_placeholder(name):
    mod = importlib.import_module(MODULE_PATH)
    obj = getattr(mod, name)
    assert obj is not None


@pytest.mark.anyio
async def test_router_resizes_quantity_before_risk_reject(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _Runner:
        is_running = True

        def __init__(self):
            self.last_order_req = None

        async def place_order(self, order_req):
            self.last_order_req = order_req
            return OrderResponse(
                broker_order_id="B123",
                status="SUCCESS",
                message="ok",
                filled_quantity=0,
                average_price=None,
            )

    class _Hub:
        def __init__(self, runner):
            self._runner = runner

        def get_runner(self, _broker_account_id):
            return self._runner

    class _StateStore:
        def __init__(self):
            self.last = None

        def get_balance(self, _broker_account_id):
            return Balance(available=1000000.0, utilized=0.0, total=1000000.0)

        def get_positions(self, _broker_account_id):
            return []

        def set_last_order_response(self, _broker_account_id, response):
            self.last = response

    class _CapitalEngine:
        def suggest_order_quantity(self, **_kwargs):
            return 75, "resized_for_max_notional_per_order_150_to_75"

        def can_afford_order(self, **_kwargs):
            return True, "capital_ok"

    class _RiskEngine:
        def check_order_allowed(self, **_kwargs):
            return True, "risk_ok"

    monkeypatch.setattr(
        mod.dashboard_bus,
        "resolve_instrument_meta",
        lambda **_k: {"lot_size": 75, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(mod.dashboard_bus, "get_last_price", lambda _label: 7000.0)

    runner = _Runner()
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=_CapitalEngine(),
        risk_engine=_RiskEngine(),
        profit_engine=None,
        state_store=_StateStore(),
    )
    req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=2,  # lots
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
    )

    _hub_order_id, response = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=req,
    )

    assert response.status == "SUCCESS"
    assert runner.last_order_req is not None
    assert int(runner.last_order_req.quantity) == 75
    assert int(response.requested_quantity or 0) == 75


@pytest.mark.anyio
async def test_router_skips_auto_resize_when_toggle_disabled(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _Runner:
        is_running = True

        def __init__(self):
            self.last_order_req = None

        async def place_order(self, order_req):
            self.last_order_req = order_req
            return OrderResponse(
                broker_order_id="B124",
                status="SUCCESS",
                message="ok",
                filled_quantity=0,
                average_price=None,
            )

    class _Hub:
        def __init__(self, runner):
            self._runner = runner

        def get_runner(self, _broker_account_id):
            return self._runner

    class _StateStore:
        def __init__(self):
            self.last = None

        def get_balance(self, _broker_account_id):
            return Balance(available=1000000.0, utilized=0.0, total=1000000.0)

        def get_positions(self, _broker_account_id):
            return []

        def set_last_order_response(self, _broker_account_id, response):
            self.last = response

    class _CapitalEngine:
        def suggest_order_quantity(self, **_kwargs):
            raise AssertionError("suggest_order_quantity should not be called")

        def can_afford_order(self, **_kwargs):
            return True, "capital_ok"

    class _RiskEngine:
        def check_order_allowed(self, **_kwargs):
            return True, "risk_ok"

    monkeypatch.setattr(
        mod.dashboard_bus,
        "resolve_instrument_meta",
        lambda **_k: {"lot_size": 75, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(mod.dashboard_bus, "get_last_price", lambda _label: 7000.0)
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(
            default_time_zone="UTC",
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
            enable_profit_checks=False,
        ),
    )

    runner = _Runner()
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=_CapitalEngine(),
        risk_engine=_RiskEngine(),
        profit_engine=None,
        state_store=_StateStore(),
    )
    req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=2,  # lots
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
    )

    _hub_order_id, response = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=req,
    )

    assert response.status == "SUCCESS"
    assert runner.last_order_req is not None
    assert int(runner.last_order_req.quantity) == 150
    assert int(response.requested_quantity or 0) == 150


def test_record_order_to_bq_uses_hub_order_id_as_insert_id(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    insert_ids: list[str | None] = []
    monkeypatch.setattr(
        mod,
        "insert_order_record",
        lambda record, insert_id=None: insert_ids.append(insert_id),
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )

    class _Hub:
        def get_runner(self, _broker_account_id):
            return None

    class _StateStore:
        def set_last_order_response(self, _broker_account_id, _response):
            return None

    router = mod.OrderRouter(
        hub=_Hub(),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_StateStore(),
    )
    req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
    )
    resp = OrderResponse(
        broker_order_id="OID-1",
        status="ACK",
        message="ok",
        filled_quantity=0,
        average_price=None,
    )

    router._record_order_to_bq(
        hub_order_id="HUB-ORDER-1",
        tenant_id="tenant-1",
        broker_account_id="ba-1",
        strategy_id="ema20_strategy",
        order_req=req,
        response=resp,
    )

    assert insert_ids == ["HUB-ORDER-1"]


def test_record_order_to_bq_prefers_async_csv_dispatcher(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    insert_ids: list[str | None] = []
    async_calls: list[tuple[dict, str]] = []
    monkeypatch.setattr(
        mod,
        "insert_order_record",
        lambda record, insert_id=None: insert_ids.append(insert_id),
    )
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )

    class _Hub:
        def get_runner(self, _broker_account_id):
            return None

    class _StateStore:
        def set_last_order_response(self, _broker_account_id, _response):
            return None

    class _AsyncWriter:
        def enqueue_order(self, record, *, hub_order_id):
            async_calls.append((record, hub_order_id))
            return True

    router = mod.OrderRouter(
        hub=_Hub(),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_StateStore(),
    )
    router._async_csv_writer = _AsyncWriter()

    def _sync_write_not_expected(_record):
        raise AssertionError("synchronous CSV writer should not be called")

    monkeypatch.setattr(router._csv_writer, "write_order", _sync_write_not_expected)

    req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
    )
    resp = OrderResponse(
        broker_order_id="OID-2",
        status="ACK",
        message="ok",
        filled_quantity=0,
        average_price=None,
    )

    router._record_order_to_bq(
        hub_order_id="HUB-ORDER-ASYNC",
        tenant_id="tenant-1",
        broker_account_id="ba-1",
        strategy_id="ema20_strategy",
        order_req=req,
        response=resp,
    )

    assert insert_ids == ["HUB-ORDER-ASYNC"]
    assert len(async_calls) == 1
    _, queued_hub_order_id = async_calls[0]
    assert queued_hub_order_id == "HUB-ORDER-ASYNC"


def test_default_interceptor_order_matches_runtime_contract():
    from app.orders.interceptors import build_default_interceptors

    names = [type(interceptor).__name__ for interceptor in build_default_interceptors()]

    assert names == [
        "CapitalResizeInterceptor",
        "CapitalGuardInterceptor",
        "RiskGuardInterceptor",
        "GlobalKillSwitchInterceptor",
        "OptionSellGuardInterceptor",
        "ExposureLimitInterceptor",
        "ProfitGuardInterceptor",
        "PositionOwnershipInterceptor",
        "BrokerTokenGuardInterceptor",
        "CircuitBreakerInterceptor",
    ]


@pytest.mark.anyio
async def test_router_idempotency_replays_completed_response_without_second_broker_call(
    monkeypatch,
):
    mod = importlib.import_module(MODULE_PATH)

    class _Runner:
        is_running = True

        def __init__(self):
            self.calls = 0

        async def place_order(self, _order_req):
            self.calls += 1
            return OrderResponse(
                broker_order_id="OID-ONCE",
                status="SUCCESS",
                message="ok",
                filled_quantity=0,
                average_price=None,
            )

    class _Hub:
        def __init__(self, runner):
            self._runner = runner

        def get_runner(self, _broker_account_id):
            return self._runner

    class _StateStore:
        def get_balance(self, _broker_account_id):
            return Balance(available=1_000_000.0, utilized=0.0, total=1_000_000.0)

        def get_positions(self, _broker_account_id):
            return []

        def set_last_order_response(self, _broker_account_id, _response):
            return None

    monkeypatch.setattr(
        mod.dashboard_bus,
        "resolve_instrument_meta",
        lambda **_k: {"lot_size": 1, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(mod.dashboard_bus, "get_last_price", lambda _label: 7000.0)
    monkeypatch.setattr(
        mod,
        "get_runtime_settings",
        lambda **_kwargs: SimpleNamespace(order_router_enforce_idempotency=True),
    )
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(default_time_zone="UTC"))

    from app.orders.order_outbox import InMemoryOrderSubmissionOutbox

    runner = _Runner()
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_StateStore(),
        submission_outbox=InMemoryOrderSubmissionOutbox(),
    )
    request = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
        idempotency_key=f"idem-{id(router)}",
    )

    first_hub_id, first_response = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=request,
    )
    second_hub_id, second_response = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=request,
    )

    expected_key = f"idem-{id(router)}"
    assert first_hub_id == expected_key
    assert second_hub_id == expected_key
    assert runner.calls == 1
    assert first_response.broker_order_id == "OID-ONCE"
    assert second_response.broker_order_id == "OID-ONCE"
    assert second_response.status == first_response.status


@pytest.mark.anyio
async def test_router_idempotency_suppresses_parallel_duplicate_submit(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _Runner:
        is_running = True

        def __init__(self):
            self.calls = 0

        async def place_order(self, _order_req):
            self.calls += 1
            await asyncio.sleep(0.05)
            return OrderResponse(
                broker_order_id="OID-PARALLEL",
                status="SUCCESS",
                message="ok",
                filled_quantity=0,
                average_price=None,
            )

    class _Hub:
        def __init__(self, runner):
            self._runner = runner

        def get_runner(self, _broker_account_id):
            return self._runner

    class _StateStore:
        def get_balance(self, _broker_account_id):
            return Balance(available=1_000_000.0, utilized=0.0, total=1_000_000.0)

        def get_positions(self, _broker_account_id):
            return []

        def set_last_order_response(self, _broker_account_id, _response):
            return None

    monkeypatch.setattr(
        mod.dashboard_bus,
        "resolve_instrument_meta",
        lambda **_k: {"lot_size": 1, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(mod.dashboard_bus, "get_last_price", lambda _label: 7000.0)
    monkeypatch.setattr(
        mod,
        "get_runtime_settings",
        lambda **_kwargs: SimpleNamespace(order_router_enforce_idempotency=True),
    )
    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(default_time_zone="UTC"))

    from app.orders.order_outbox import InMemoryOrderSubmissionOutbox

    runner = _Runner()
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=_StateStore(),
        submission_outbox=InMemoryOrderSubmissionOutbox(),
    )
    request = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
        idempotency_key=f"idem-parallel-{id(router)}",
    )

    results = await asyncio.gather(
        router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id="ema20-trend",
            order_req=request,
        ),
        router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id="ema20-trend",
            order_req=request,
        ),
    )
    statuses = {res[1].status for res in results}
    assert runner.calls == 1
    assert statuses.issubset({"SUCCESS", "DUPLICATE_PENDING"})
