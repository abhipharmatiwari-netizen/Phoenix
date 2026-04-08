from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest

from app.brokers.base import (
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.data.state_store import StateStore
from app.orders.order_lifecycle import OrderLifecycleService
from app.orders.order_outbox import (
    InMemoryOrderSubmissionOutbox,
    OUTBOX_STATUS_SUBMITTING,
)
from app.orders.trade_processed_store import InMemoryProcessedTradeStore

MODULE_PATH = "app.orders.router"


class _Hub:
    def __init__(self, runner) -> None:
        self._runner = runner

    def get_runner(self, _broker_account_id):
        return self._runner


class _SuccessRunner:
    is_running = True

    def __init__(self, *, broker_order_id: str, status: str = "SUCCESS") -> None:
        self.broker_order_id = broker_order_id
        self.status = status
        self.calls = 0

    async def place_order(self, _order_req):
        self.calls += 1
        return OrderResponse(
            broker_order_id=self.broker_order_id,
            status=self.status,
            message="ok",
            filled_quantity=0,
            average_price=None,
        )


class _CrashAfterAcceptRunner:
    is_running = True

    def __init__(self, *, state_store: StateStore, request_symbol: str, quantity: int) -> None:
        self._state_store = state_store
        self._request_symbol = request_symbol
        self._quantity = quantity
        self.calls = 0

    async def place_order(self, _order_req):
        self.calls += 1
        completed = OrderStatus(
            order_id="OID-RECOVER-1",
            symbol=self._request_symbol,
            side="BUY",
            status="COMPLETE",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=self._quantity,
            filled_quantity=self._quantity,
            average_price=101.5,
            price=None,
            exchange="NFO",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._state_store.set_order_snapshot("A1", [completed])
        self._state_store.set_orders("A1", [])
        raise TimeoutError("broker timeout after accept")


def _patch_router_basics(monkeypatch, mod) -> None:
    monkeypatch.setattr(
        mod.dashboard_bus,
        "resolve_instrument_meta",
        lambda **_k: {"lot_size": 1, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(mod.dashboard_bus, "get_last_price", lambda _label: 7000.0)
    monkeypatch.setattr(
        mod,
        "get_runtime_settings",
        lambda **_kwargs: type("Cfg", (), {"order_router_enforce_idempotency": True})(),
    )
    monkeypatch.setattr(mod, "get_settings", lambda: type("Cfg", (), {"default_time_zone": "UTC"})())


def _request(idempotency_key: str | None = None) -> OrderRequest:
    return OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="12345",
        idempotency_key=idempotency_key,
    )


@pytest.mark.anyio
async def test_router_replays_completed_outbox_response_across_router_restart(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    _patch_router_basics(monkeypatch, mod)

    outbox = InMemoryOrderSubmissionOutbox()
    runner = _SuccessRunner(broker_order_id="OID-OUTBOX-1")
    state_store = StateStore()

    router_1 = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        submission_outbox=outbox,
    )
    router_2 = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        submission_outbox=outbox,
    )

    first_hub_id, first_response = await router_1.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=_request(idempotency_key="idem-outbox-1"),
    )
    second_hub_id, second_response = await router_2.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=_request(idempotency_key="idem-outbox-1"),
    )

    assert first_hub_id == "idem-outbox-1"
    assert second_hub_id == "idem-outbox-1"
    assert runner.calls == 1
    assert first_response.broker_order_id == "OID-OUTBOX-1"
    assert second_response.broker_order_id == "OID-OUTBOX-1"


@pytest.mark.anyio
async def test_router_recovery_adopts_broker_snapshot_after_submit_timeout(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    _patch_router_basics(monkeypatch, mod)

    inserted_rows: list[dict] = []
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: inserted_rows.append(dict(row)),
    )

    state_store = StateStore()
    outbox = InMemoryOrderSubmissionOutbox()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
        submission_outbox=outbox,
    )
    runner = _CrashAfterAcceptRunner(
        state_store=state_store,
        request_symbol="NIFTY17FEB2625750CE",
        quantity=1,
    )
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        order_lifecycle=lifecycle,
        submission_outbox=outbox,
    )

    with pytest.raises(TimeoutError, match="broker timeout after accept"):
        await router.submit_order(
            tenant_id="tenant-1",
            broker_account_id="A1",
            strategy_id="ema20-trend",
            order_req=_request(idempotency_key="idem-recover-1"),
        )

    active_records = outbox.list_active_submissions()
    assert len(active_records) == 1
    assert active_records[0].status == OUTBOX_STATUS_SUBMITTING

    summary = await router.recover_submission_outbox()

    assert summary["adopted"] == 1
    assert runner.calls == 1
    assert len(inserted_rows) == 1
    assert inserted_rows[0]["trade_id"] == "OID-RECOVER-1"
    assert outbox.list_active_submissions() == []


@pytest.mark.anyio
async def test_router_recovery_replays_pending_outbox_submission(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    _patch_router_basics(monkeypatch, mod)

    state_store = StateStore()
    outbox = InMemoryOrderSubmissionOutbox()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
        submission_outbox=outbox,
    )
    runner = _SuccessRunner(broker_order_id="OID-PENDING-1", status="OPEN")
    router = mod.OrderRouter(
        hub=_Hub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        order_lifecycle=lifecycle,
        submission_outbox=outbox,
    )

    claimed, record = outbox.reserve_submission(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        submission_key="idem-pending-1",
        hub_order_id="idem-pending-1",
        order_req=_request(idempotency_key="idem-pending-1"),
        contract_key=None,
        ownership_strategy_id=None,
        created_at=datetime.now(timezone.utc),
    )
    assert claimed is True
    assert record.status == "PENDING"

    summary = await router.recover_submission_outbox()

    assert summary["replayed"] == 1
    assert runner.calls == 1
    active_records = outbox.list_active_submissions()
    assert len(active_records) == 1
    assert active_records[0].broker_order_id == "OID-PENDING-1"
    assert active_records[0].status == "ACKED"
