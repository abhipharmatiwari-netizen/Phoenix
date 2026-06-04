from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.brokers.base import (
    Balance,
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
from app.orders.position_ownership import ContractKey
from app.orders.trade_processed_store import InMemoryProcessedTradeStore
from app.orders.router import OrderRouter


@pytest.mark.anyio
async def test_order_lifecycle_golden_entry_then_exit(monkeypatch):
    class _Runner:
        is_running = True

        def __init__(self) -> None:
            self.calls: list[OrderRequest] = []

        async def place_order(self, order_req: OrderRequest) -> OrderResponse:
            self.calls.append(order_req)
            return OrderResponse(
                broker_order_id=f"OID-{len(self.calls)}",
                status="FILLED",
                message="ok",
                filled_quantity=order_req.quantity,
                average_price=100.0,
            )

    class _Hub:
        def __init__(self, runner: _Runner) -> None:
            self._runner = runner

        def get_runner(self, _broker_account_id):
            return self._runner

    class _StateStore:
        def __init__(self) -> None:
            self.last = None

        def get_balance(self, _broker_account_id):
            return Balance(available=1_000_000.0, utilized=0.0, total=1_000_000.0)

        def get_positions(self, _broker_account_id):
            return []

        def set_last_order_response(self, _broker_account_id, response):
            self.last = response

    class _CapitalEngine:
        def suggest_order_quantity(self, **_kwargs):
            return 50, "ok"

        def can_afford_order(self, **_kwargs):
            return True, "capital_ok"

    class _RiskEngine:
        def check_order_allowed(self, **_kwargs):
            return True, "risk_ok"

    class _ProfitEngine:
        def __init__(self) -> None:
            self.calls = 0

        def check_order_allowed(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(allowed=True, reason="profit_ok")

    monkeypatch.setattr(
        "app.orders.router.get_settings",
        lambda: SimpleNamespace(
            default_time_zone="UTC",
            enable_capital_checks=True,
            capital_auto_resize_enabled=False,
            enable_risk_checks=True,
            enable_profit_checks=True,
        ),
    )
    monkeypatch.setattr(
        "app.orders.router.dashboard_bus.resolve_instrument_meta",
        lambda **_k: {"lot_size": 50, "label": "NIFTY_ATM_CE_25750"},
    )
    monkeypatch.setattr(
        "app.orders.router.dashboard_bus.get_last_price",
        lambda _label: 100.0,
    )

    runner = _Runner()
    profit_engine = _ProfitEngine()
    router = OrderRouter(
        hub=_Hub(runner),
        capital_engine=_CapitalEngine(),
        risk_engine=_RiskEngine(),
        profit_engine=profit_engine,
        state_store=_StateStore(),
    )

    entry_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.ENTRY,
        symbol_token="12345",
    )
    exit_req = OrderRequest(
        symbol="NIFTY17FEB2625750CE",
        quantity=1,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        purpose=OrderPurpose.EXIT,
        symbol_token="12345",
    )

    _, entry_resp = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="golden-order-lifecycle",
        order_req=entry_req,
    )
    _, exit_resp = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="golden-order-lifecycle",
        order_req=exit_req,
    )

    assert entry_resp.status == "FILLED"
    assert exit_resp.status == "FILLED"
    assert len(runner.calls) == 2
    assert runner.calls[0].quantity == 50
    assert runner.calls[1].quantity == 50
    assert runner.calls[0].purpose == OrderPurpose.ENTRY
    assert runner.calls[1].purpose == OrderPurpose.EXIT
    assert profit_engine.calls == 1


@dataclass
class _PnlSpy:
    events: list[object] = field(default_factory=list)

    def on_trade(self, event: object) -> None:
        self.events.append(event)


@dataclass
class _OwnershipSpy:
    release_calls: list[dict] = field(default_factory=list)
    fill_calls: list[dict] = field(default_factory=list)

    def release_pending(self, **kwargs) -> None:
        self.release_calls.append(dict(kwargs))

    def apply_fill(self, **kwargs) -> None:
        self.fill_calls.append(dict(kwargs))


def _entry_order_request() -> OrderRequest:
    return OrderRequest(
        symbol="NATURALGAS24MAR26310CE",
        quantity=1250,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
    )


def _snapshot_order(*, status: str, updated_at: str) -> OrderStatus:
    return OrderStatus(
        order_id="OID-SNAPSHOT-1",
        symbol="NATURALGAS24MAR26310CE",
        side="SELL",
        status=status,
        order_type="MARKET",
        product_type="INTRADAY",
        quantity=1250,
        filled_quantity=1250 if status == "COMPLETE" else 0,
        average_price=26.60 if status == "COMPLETE" else None,
        price=26.60,
        exchange="MCX",
        updated_at=updated_at,
    )


@pytest.mark.anyio
async def test_order_lifecycle_uses_broker_snapshot_for_delayed_complete_once(monkeypatch):
    monkeypatch.setenv("ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT", "true")
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
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="ema20-trend",
        hub_order_id="tenant-1:acc-1:ema20-trend:oid-snapshot",
        order_req=_entry_order_request(),
        response=OrderResponse(
            broker_order_id="OID-SNAPSHOT-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )

    state_store.set_orders("acc-1", [])
    state_store.set_order_snapshot(
        "acc-1",
        [_snapshot_order(status="COMPLETE", updated_at="2026-03-09T08:21:00+00:00")],
    )

    await lifecycle._tick_once()
    await lifecycle._tick_once()

    assert len(inserted_rows) == 1
    assert inserted_rows[0]["trade_id"] == "OID-SNAPSHOT-1"
    assert len(pnl_spy.events) == 1


@pytest.mark.anyio
async def test_order_lifecycle_releases_submitter_pending_lock_when_fill_owner_differs(monkeypatch):
    monkeypatch.setenv("ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT", "true")
    state_store = StateStore()
    ownership_spy = _OwnershipSpy()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        position_ownership_store=ownership_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )
    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )
    contract_key = ContractKey(
        underlying="NATURALGAS",
        expiry="2026-03-24",
        strike="310",
        option_right="CE",
        product_type="INTRADAY",
    )

    lifecycle.register_submission(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="system::position_trailing_lock",
        hub_order_id="tenant-1:acc-1:system::position_trailing_lock:oid-exit",
        order_req=OrderRequest(
            symbol="NATURALGAS24MAR26310CE",
            quantity=1250,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            product_type=ProductType.INTRADAY,
            time_in_force=TimeInForce.DAY,
            purpose=OrderPurpose.EXIT,
        ),
        response=OrderResponse(
            broker_order_id="OID-SNAPSHOT-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
        contract_key=contract_key,
        ownership_strategy_id="ema20_strategy",
    )

    state_store.set_orders("acc-1", [])
    state_store.set_order_snapshot(
        "acc-1",
        [_snapshot_order(status="COMPLETE", updated_at="2026-03-09T08:21:00+00:00")],
    )

    await lifecycle._tick_once()
    await lifecycle._tick_once()

    assert ownership_spy.fill_calls
    assert ownership_spy.fill_calls[0]["strategy_id"] == "ema20_strategy"
    assert ownership_spy.release_calls == [
        {
            "tenant_id": "tenant-1",
            "broker_account_id": "acc-1",
            "contract_key": contract_key,
            "strategy_id": "system::position_trailing_lock",
        }
    ]


@pytest.mark.anyio
async def test_order_lifecycle_uses_broker_snapshot_for_delayed_reject_once(monkeypatch):
    monkeypatch.setenv("ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT", "true")
    state_store = StateStore()
    ownership_spy = _OwnershipSpy()
    lifecycle = OrderLifecycleService(
        state_store=state_store,
        pnl_engine=None,
        position_ownership_store=ownership_spy,
        processed_store=InMemoryProcessedTradeStore(),
    )

    monkeypatch.setattr(
        "app.orders.order_lifecycle.insert_trade_record",
        lambda row, **_kwargs: None,
    )

    lifecycle.register_submission(
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="ema20-trend",
        hub_order_id="tenant-1:acc-1:ema20-trend:oid-reject",
        order_req=_entry_order_request(),
        response=OrderResponse(
            broker_order_id="OID-SNAPSHOT-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
        contract_key=ContractKey(
            underlying="NATURALGAS",
            expiry="2026-03-24",
            strike="310",
            option_right="CE",
            product_type="INTRADAY",
        ),
        ownership_strategy_id="ema20-trend",
    )

    state_store.set_orders("acc-1", [])
    state_store.set_order_snapshot(
        "acc-1",
        [_snapshot_order(status="REJECTED", updated_at="2026-03-09T08:22:00+00:00")],
    )

    await lifecycle._tick_once()
    await lifecycle._tick_once()

    assert len(ownership_spy.release_calls) == 1
    assert ownership_spy.release_calls[0]["strategy_id"] == "ema20-trend"
    assert ownership_spy.fill_calls == []


@pytest.mark.anyio
async def test_order_lifecycle_legacy_mode_ignores_terminal_broker_snapshot(monkeypatch):
    monkeypatch.setenv("ORDER_LIFECYCLE_USE_BROKER_SNAPSHOT", "false")
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
        tenant_id="tenant-1",
        broker_account_id="acc-1",
        strategy_id="ema20-trend",
        hub_order_id="tenant-1:acc-1:ema20-trend:oid-legacy",
        order_req=_entry_order_request(),
        response=OrderResponse(
            broker_order_id="OID-SNAPSHOT-1",
            status="OPEN",
            message="accepted",
            filled_quantity=0,
            average_price=None,
        ),
    )

    state_store.set_orders("acc-1", [])
    state_store.set_order_snapshot(
        "acc-1",
        [_snapshot_order(status="COMPLETE", updated_at="2026-03-09T08:23:00+00:00")],
    )

    await lifecycle._tick_once()

    assert inserted_rows == []
    assert pnl_spy.events == []


# ---------------------------------------------------------------------------
# Issue #17: forced position_state mutation must never happen
# ---------------------------------------------------------------------------

def test_reconciling_to_exit_pending_escalates_to_degraded_not_forced():
    """RECONCILING -> EXIT_PENDING is illegal; must escalate to DEGRADED, not bypass SM."""
    from app.core.position_state import InternalPosition, PositionState
    from app.orders.order_lifecycle import OrderLifecycleService

    svc = OrderLifecycleService.__new__(OrderLifecycleService)
    svc._position_ownership_store = None

    pos = InternalPosition()
    pos.position_state = PositionState.RECONCILING
    pos.state_reason = "test"

    svc._transition_position_state(pos, PositionState.EXIT_PENDING, reason="exit_submitted")

    assert pos.position_state == PositionState.DEGRADED, (
        f"Expected DEGRADED after illegal RECONCILING->EXIT_PENDING, got {pos.position_state.value}"
    )
    assert "EXIT_PENDING" in pos.state_reason


def test_reconciling_to_opening_escalates_to_degraded_not_forced():
    """RECONCILING -> OPENING is illegal; must escalate to DEGRADED."""
    from app.core.position_state import InternalPosition, PositionState
    from app.orders.order_lifecycle import OrderLifecycleService

    svc = OrderLifecycleService.__new__(OrderLifecycleService)
    svc._position_ownership_store = None

    pos = InternalPosition()
    pos.position_state = PositionState.RECONCILING
    pos.state_reason = "test"

    svc._transition_position_state(pos, PositionState.OPENING, reason="entry_awaiting_durable_fill")

    assert pos.position_state == PositionState.DEGRADED, (
        f"Expected DEGRADED after illegal RECONCILING->OPENING, got {pos.position_state.value}"
    )


def test_reconciling_illegal_targets_all_escalate_to_degraded():
    """Every illegal target from RECONCILING must escalate to DEGRADED, never force-assign."""
    from app.core.position_state import InternalPosition, PositionState, VALID_TRANSITIONS
    from app.orders.order_lifecycle import OrderLifecycleService

    svc = OrderLifecycleService.__new__(OrderLifecycleService)
    svc._position_ownership_store = None

    allowed_from_reconciling = VALID_TRANSITIONS[PositionState.RECONCILING]
    illegal_targets = set(PositionState) - allowed_from_reconciling - {PositionState.RECONCILING}

    for target in illegal_targets:
        pos = InternalPosition()
        pos.position_state = PositionState.RECONCILING

        svc._transition_position_state(pos, target, reason="test")

        assert pos.position_state != target, (
            f"Direct mutation detected: RECONCILING -> {target.value} "
            f"bypassed state machine (got {pos.position_state.value})"
        )
        assert pos.position_state == PositionState.DEGRADED, (
            f"Expected DEGRADED after illegal RECONCILING->{target.value}, "
            f"got {pos.position_state.value}"
        )
