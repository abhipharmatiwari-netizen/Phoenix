from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

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
from app.data.state_store import StateStore
from app.orders.order_lifecycle import OrderLifecycleService
from app.orders.position_ownership import PositionOwnershipStore, derive_contract_key
from app.orders.order_state import OrderLifecycleState
from app.orders.trade_processed_store import InMemoryProcessedTradeStore


@dataclass
class _PnlSpy:
    events: list[object] = field(default_factory=list)

    def on_trade(self, event: object) -> None:
        self.events.append(event)


def test_order_lifecycle_recent_entry_submission_survives_immediate_fill(monkeypatch):
    lifecycle = OrderLifecycleService(
        state_store=StateStore(),
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    req = OrderRequest(
        symbol="NATURALGAS24MAR26310CE",
        quantity=1250,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.ENTRY,
        position_label="NG_ATM_CE_310",
        strategy_context={"tp_pct": 0.08, "sl_pct": 0.15},
    )

    lifecycle.register_submission(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        hub_order_id="tenant-1:A1:ema20-trend:entry-1",
        order_req=req,
        response=OrderResponse(
            broker_order_id="OID-ENTRY-1",
            status="COMPLETE",
            message="filled",
            filled_quantity=1250,
            average_price=29.55,
        ),
    )

    hint = lifecycle.find_recent_entry_submission(
        broker_account_id="A1",
        label="NG_ATM_CE_310",
        symbol="NATURALGAS24MAR26310CE",
        side="SELL",
    )

    assert hint is not None
    assert hint["strategy_name"] == "ema20-trend"
    assert hint["strategy_context"] == {"tp_pct": 0.08, "sl_pct": 0.15}
    assert hint["position_label"] == "NG_ATM_CE_310"


def test_order_lifecycle_immediate_fill_projects_position_into_state_store(monkeypatch):
    state_store = StateStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:entry-projection",
        order_req=OrderRequest(
            symbol="NIFTY17FEB2625750CE",
            quantity=25,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.ENTRY,
        ),
        response=OrderResponse(
            broker_order_id="OID-POS-1",
            status="COMPLETE",
            message="filled",
            filled_quantity=25,
            average_price=101.25,
        ),
    )

    positions = state_store.get_positions("ba1")
    assert len(positions) == 1
    assert positions[0].symbol == "NIFTY17FEB2625750CE"
    assert positions[0].quantity == 25
    assert positions[0].avg_price == pytest.approx(101.25)
    assert positions[0].product_type == ProductType.INTRADAY
    assert positions[0].entry_ts is not None


def test_order_lifecycle_exit_fill_updates_projected_state_store_position(monkeypatch):
    state_store = StateStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    entry_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=25,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.ENTRY,
    )
    exit_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=10,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.EXIT,
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:entry-projection",
        order_req=entry_req,
        response=OrderResponse(
            broker_order_id="OID-POS-ENTRY",
            status="COMPLETE",
            message="filled",
            filled_quantity=25,
            average_price=100.0,
        ),
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:exit-partial",
        order_req=exit_req,
        response=OrderResponse(
            broker_order_id="OID-POS-EXIT-1",
            status="COMPLETE",
            message="filled",
            filled_quantity=10,
            average_price=105.0,
        ),
    )

    positions = state_store.get_positions("ba1")
    assert len(positions) == 1
    assert positions[0].quantity == 15
    assert positions[0].avg_price == pytest.approx(100.0)

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:exit-flat",
        order_req=OrderRequest(
            symbol="NIFTY17FEB2625750CE",
            quantity=15,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.EXIT,
        ),
        response=OrderResponse(
            broker_order_id="OID-POS-EXIT-2",
            status="COMPLETE",
            message="filled",
            filled_quantity=15,
            average_price=110.0,
        ),
    )

    assert state_store.get_positions("ba1") == []


@pytest.mark.anyio
async def test_order_lifecycle_delayed_fill_emits_trade_and_pnl_once(monkeypatch):
    state_store = StateStore()
    pnl_spy = _PnlSpy()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )

    inserted_rows: list[dict] = []
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row: inserted_rows.append(dict(row)),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    ack_response = OrderResponse(
        broker_order_id="OID1",
        status="OPEN",
        message="accepted",
        filled_quantity=0,
        average_price=None,
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:xyz",
        order_req=order_req,
        response=ack_response,
    )

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID1",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="COMPLETE",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=65,
            filled_quantity=65,
            average_price=100.0,
            price=None,
            exchange="NFO",
            updated_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    await lifecycle._tick_once()
    assert len(inserted_rows) == 1
    assert len(pnl_spy.events) == 1
    assert inserted_rows[0]["trade_id"] == "OID1"
    assert inserted_rows[0]["qty"] == 65
    assert inserted_rows[0]["price"] == 100.0

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID1",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="COMPLETE",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=65,
            filled_quantity=65,
            average_price=100.0,
            price=None,
            exchange="NFO",
            updated_at=datetime.now(timezone.utc).isoformat(),
        ),
    )

    await lifecycle._tick_once()
    assert len(inserted_rows) == 1
    assert len(pnl_spy.events) == 1


@pytest.mark.anyio
async def test_order_lifecycle_upsert_order_delayed_fill_is_exactly_once(monkeypatch):
    state_store = StateStore()
    pnl_spy = _PnlSpy()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )

    inserted_rows: list[dict] = []
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: inserted_rows.append(dict(row)),
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:upsert-once",
        order_req=OrderRequest(
            symbol="NIFTY17FEB2625750CE",
            quantity=65,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
        ),
        response=OrderResponse(
            broker_order_id="OID-UPSERT-ONCE",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )

    for updated_at in (
        "2026-02-22T11:40:00+00:00",
        "2026-02-22T11:40:30+00:00",
        "2026-02-22T11:41:00+00:00",
    ):
        state_store.upsert_order(
            "ba1",
            OrderStatus(
                order_id="OID-UPSERT-ONCE",
                symbol="NIFTY17FEB2625750CE",
                side="BUY",
                status="COMPLETE",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=65,
                filled_quantity=65,
                average_price=100.0,
                price=None,
                exchange="NFO",
                updated_at=updated_at,
            ),
        )
        await lifecycle._tick_once()

    assert len(inserted_rows) == 1
    assert len(pnl_spy.events) == 1
    assert inserted_rows[0]["trade_id"] == "OID-UPSERT-ONCE"


@pytest.mark.anyio
async def test_order_lifecycle_passes_insert_id_for_trade_insert(monkeypatch):
    state_store = StateStore()
    pnl_spy = _PnlSpy()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )

    inserted: list[tuple[dict, str | None]] = []

    def _capture_insert(row: dict, *, insert_id: str | None = None) -> None:
        inserted.append((dict(row), insert_id))

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        _capture_insert,
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=25,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    fill_response = OrderResponse(
        broker_order_id="OID-INSERTID",
        status="COMPLETE",
        message="filled",
        filled_quantity=25,
        average_price=101.25,
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:oid-insertid",
        order_req=order_req,
        response=fill_response,
    )

    assert len(inserted) == 1
    row, insert_id = inserted[0]
    assert row["trade_id"] == "OID-INSERTID"
    assert insert_id == "ba1:OID-INSERTID"


@pytest.mark.anyio
async def test_order_lifecycle_restart_dedupe_with_processed_marker(monkeypatch):
    state_store = StateStore()
    durable_store = InMemoryProcessedTradeStore()
    inserted_rows: list[dict] = []

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: inserted_rows.append(dict(row)),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    ack_response = OrderResponse(
        broker_order_id="OID-RESTART",
        status="OPEN",
        message="accepted",
        filled_quantity=0,
        average_price=None,
    )
    completed = OrderStatus(
        order_id="OID-RESTART",
        symbol="NIFTY17FEB2625750CE",
        side="BUY",
        status="COMPLETE",
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=65,
        filled_quantity=65,
        average_price=100.0,
        price=None,
        exchange="NFO",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    pnl_spy_1 = _PnlSpy()
    lifecycle_1 = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy_1,
        processed_store=durable_store,
    )
    lifecycle_1.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:xyz",
        order_req=order_req,
        response=ack_response,
    )
    state_store.upsert_order("ba1", completed)
    await lifecycle_1._tick_once()

    assert len(inserted_rows) == 1
    assert len(pnl_spy_1.events) == 1

    # Simulate process restart: new lifecycle service, same durable marker backend.
    pnl_spy_2 = _PnlSpy()
    lifecycle_2 = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy_2,
        processed_store=durable_store,
    )
    lifecycle_2.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:xyz-restart",
        order_req=order_req,
        response=ack_response,
    )
    await lifecycle_2._tick_once()

    assert len(inserted_rows) == 1
    assert len(pnl_spy_2.events) == 0


@pytest.mark.anyio
async def test_order_lifecycle_processed_order_cache_is_capped(monkeypatch):
    monkeypatch.setenv("ORDER_LIFECYCLE_PROCESSED_MAX_ENTRIES", "3")
    monkeypatch.setenv("ORDER_LIFECYCLE_PROCESSED_TTL_SECONDS", str(7 * 24 * 60 * 60))

    state_store = StateStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    inserted_rows: list[dict] = []
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: inserted_rows.append(dict(row)),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    for idx in range(6):
        lifecycle.register_submission(
            tenant_id="t1",
            broker_account_id="ba1",
            strategy_id="s1",
            hub_order_id=f"t1:ba1:s1:{idx}",
            order_req=order_req,
            response=OrderResponse(
                broker_order_id=f"OID{idx}",
                status="COMPLETE",
                message="filled",
                filled_quantity=1,
                average_price=100.0 + idx,
            ),
        )

    assert len(inserted_rows) == 6
    assert lifecycle.processed_count == 3
    assert list(lifecycle._processed_orders.keys()) == [
        "ba1:OID3",
        "ba1:OID4",
        "ba1:OID5",
    ]


@pytest.mark.anyio
async def test_order_lifecycle_tick_processes_only_tracked_orders(monkeypatch):
    state_store = StateStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:tracked-1",
        order_req=order_req,
        response=OrderResponse(
            broker_order_id="OID-TRACKED-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:tracked-2",
        order_req=order_req,
        response=OrderResponse(
            broker_order_id="OID-TRACKED-2",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )

    state_store.set_orders(
        "ba1",
        [
            OrderStatus(
                order_id="OID-TRACKED-1",
                symbol="NIFTY17FEB2625750CE",
                side="BUY",
                status="OPEN",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=0,
                average_price=None,
                price=None,
                exchange="NFO",
                updated_at="2026-02-22T11:00:00+00:00",
            ),
            OrderStatus(
                order_id="OID-UNTRACKED",
                symbol="NIFTY17FEB2625750CE",
                side="BUY",
                status="COMPLETE",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=1,
                average_price=101.0,
                price=None,
                exchange="NFO",
                updated_at="2026-02-22T11:00:01+00:00",
            ),
            OrderStatus(
                order_id="OID-TRACKED-2",
                symbol="NIFTY17FEB2625750CE",
                side="BUY",
                status="OPEN",
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=0,
                average_price=None,
                price=None,
                exchange="NFO",
                updated_at="2026-02-22T11:00:02+00:00",
            ),
        ],
    )

    processed_ids: list[str] = []

    def _capture_only_tracked(
        _account_id: str,
        order: OrderStatus,
        **_kwargs,
    ) -> None:
        processed_ids.append(order.order_id)

    monkeypatch.setattr(lifecycle, "_process_order_status", _capture_only_tracked)

    await lifecycle._tick_once()

    assert set(processed_ids) == {"OID-TRACKED-1", "OID-TRACKED-2"}
    assert "OID-UNTRACKED" not in processed_ids


@pytest.mark.anyio
async def test_order_lifecycle_tick_skips_unchanged_snapshot(monkeypatch):
    state_store = StateStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        processed_store=InMemoryProcessedTradeStore(),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:tracked",
        order_req=order_req,
        response=OrderResponse(
            broker_order_id="OID-TRACKED",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID-TRACKED",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="OPEN",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=1,
            filled_quantity=0,
            average_price=None,
            price=None,
            exchange="NFO",
            updated_at="2026-02-22T11:05:00+00:00",
        ),
    )

    call_count = 0

    def _capture_calls(_account_id: str, _order: OrderStatus, **_kwargs) -> None:
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(lifecycle, "_process_order_status", _capture_calls)

    await lifecycle._tick_once()
    await lifecycle._tick_once()
    assert call_count == 1

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID-TRACKED",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="COMPLETE",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=1,
            filled_quantity=1,
            average_price=102.0,
            price=None,
            exchange="NFO",
            updated_at="2026-02-22T11:05:30+00:00",
        ),
    )
    await lifecycle._tick_once()
    assert call_count == 2


@pytest.mark.anyio
async def test_order_lifecycle_applies_fill_to_position_ownership_store(monkeypatch):
    state_store = StateStore()
    ownership_store = PositionOwnershipStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        position_ownership_store=ownership_store,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    entry_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    contract_key, _ = derive_contract_key(entry_req)
    assert contract_key is not None
    ownership_store.try_acquire(
        tenant_id="t1",
        broker_account_id="ba1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:entry",
        order_req=entry_req,
        response=OrderResponse(
            broker_order_id="OID-OWN-1",
            status="COMPLETE",
            message="filled",
            filled_quantity=1,
            average_price=100.0,
        ),
        contract_key=contract_key,
        ownership_strategy_id="s1",
    )
    assert (
        ownership_store.get_owner(
            tenant_id="t1",
            broker_account_id="ba1",
            contract_key=contract_key,
        )
        == "s1"
    )

    exit_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    ownership_store.try_acquire(
        tenant_id="t1",
        broker_account_id="ba1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=True,
        unknown_mode="block_entries",
    )
    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:exit",
        order_req=exit_req,
        response=OrderResponse(
            broker_order_id="OID-OWN-2",
            status="COMPLETE",
            message="filled",
            filled_quantity=1,
            average_price=99.0,
        ),
        contract_key=contract_key,
        ownership_strategy_id="s1",
    )
    assert (
        ownership_store.get_owner(
            tenant_id="t1",
            broker_account_id="ba1",
            contract_key=contract_key,
        )
        is None
    )


@pytest.mark.anyio
async def test_order_lifecycle_partial_fill_then_cancel_records_executed_qty(monkeypatch):
    state_store = StateStore()
    pnl_spy = _PnlSpy()
    ownership_store = PositionOwnershipStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=pnl_spy,
        position_ownership_store=ownership_store,
        processed_store=InMemoryProcessedTradeStore(),
    )

    inserted_rows: list[dict] = []
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: inserted_rows.append(dict(row)),
    )

    order_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=65,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.ENTRY,
        symbol_token="12345",
    )
    contract_key, _ = derive_contract_key(order_req)
    ownership_store.try_acquire(
        tenant_id="t1",
        broker_account_id="ba1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:partial-cancel",
        order_req=order_req,
        response=OrderResponse(
            broker_order_id="OID-PARTIAL-CANCEL",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
        contract_key=contract_key,
        ownership_strategy_id="s1",
    )

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID-PARTIAL-CANCEL",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="PARTIAL",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=65,
            filled_quantity=25,
            average_price=101.5,
            price=None,
            exchange="NFO",
            updated_at="2026-02-22T11:10:00+00:00",
        ),
    )
    await lifecycle._tick_once()

    partial_record = ownership_store.get_ownership_record(
        tenant_id="t1",
        broker_account_id="ba1",
        contract_key=contract_key,
    )
    assert partial_record is not None
    assert partial_record.state.value == "OWNED"

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID-PARTIAL-CANCEL",
            symbol="NIFTY17FEB2625750CE",
            side="BUY",
            status="CANCELLED",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=65,
            filled_quantity=25,
            average_price=101.5,
            price=None,
            exchange="NFO",
            updated_at="2026-02-22T11:10:30+00:00",
        ),
    )
    await lifecycle._tick_once()

    assert len(inserted_rows) == 1
    assert inserted_rows[0]["qty"] == 25
    assert len(pnl_spy.events) == 1
    assert pnl_spy.events[0].qty == 25
    assert (
        lifecycle.get_order_state(
            broker_account_id="ba1",
            broker_order_id="OID-PARTIAL-CANCEL",
        )
        == OrderLifecycleState.CANCELLED
    )
    assert (
        ownership_store.get_owner(
            tenant_id="t1",
            broker_account_id="ba1",
            contract_key=contract_key,
        )
        == "s1"
    )


@pytest.mark.anyio
async def test_order_lifecycle_terminal_non_fill_releases_position_ownership_pending(
    monkeypatch,
):
    state_store = StateStore()
    ownership_store = PositionOwnershipStore()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        position_ownership_store=ownership_store,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )
    contract_key, _ = derive_contract_key(req)
    assert contract_key is not None
    ownership_store.try_acquire(
        tenant_id="t1",
        broker_account_id="ba1",
        contract_key=contract_key,
        strategy_id="s1",
        is_exit_order=False,
        unknown_mode="block_entries",
    )

    lifecycle.register_submission(
        tenant_id="t1",
        broker_account_id="ba1",
        strategy_id="s1",
        hub_order_id="t1:ba1:s1:pending",
        order_req=req,
        response=OrderResponse(
            broker_order_id="OID-CANCEL",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
        contract_key=contract_key,
        ownership_strategy_id="s1",
    )

    state_store.upsert_order(
        "ba1",
        OrderStatus(
            order_id="OID-CANCEL",
            symbol="NIFTY17FEB2625750CE",
            side="SELL",
            status="CANCELLED",
            order_type="MARKET",
            product_type="INTRADAY",
            quantity=1,
            filled_quantity=0,
            average_price=None,
            price=None,
            exchange="NFO",
            updated_at="2026-02-22T11:05:30+00:00",
        ),
    )
    await lifecycle._tick_once()

    assert (
        ownership_store.get_owner(
            tenant_id="t1",
            broker_account_id="ba1",
            contract_key=contract_key,
        )
        is None
    )
