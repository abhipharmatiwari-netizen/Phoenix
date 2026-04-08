import importlib
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.brokers.base import OrderPurpose, OrderResponse, Position, ProductType
from app.hub.exit_engines import EODExitEngine

MODULE_PATH = "app.hub.exit_engines"
CALLABLE_NAMES = ['ProfitSweepEngine', 'EODExitEngine']


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


def test_resolve_exit_lots_converts_units_to_lots():
    mod = importlib.import_module(MODULE_PATH)
    pos = SimpleNamespace(
        symbol="NIFTY27FEB2622500CE",
        quantity=50,
        lot_size=50,
        exchange="NFO",
    )

    lots, broker_units, lot_size, reason = mod._resolve_exit_lots(pos)

    assert lots == 1
    assert broker_units == 50
    assert lot_size == 50
    assert reason == "ok"


def test_resolve_exit_lots_rejects_non_divisible_units():
    mod = importlib.import_module(MODULE_PATH)
    pos = SimpleNamespace(
        symbol="NIFTY27FEB2622500CE",
        quantity=75,
        lot_size=50,
        exchange="NFO",
    )

    lots, broker_units, lot_size, reason = mod._resolve_exit_lots(pos)

    assert lots is None
    assert broker_units == 75
    assert lot_size == 50
    assert reason == "units_not_multiple_of_lot_size"


def test_resolve_exit_lots_rejects_derivative_without_lot_size():
    mod = importlib.import_module(MODULE_PATH)
    pos = SimpleNamespace(
        symbol="FOOBAR27FEB2622500CE",
        quantity=50,
        exchange="NFO",
    )

    lots, broker_units, lot_size, reason = mod._resolve_exit_lots(pos)

    assert lots is None
    assert broker_units == 50
    assert lot_size is None
    assert reason == "lot_size_missing"


def test_resolve_exit_lots_non_derivative_defaults_to_one_lot_size():
    mod = importlib.import_module(MODULE_PATH)
    pos = SimpleNamespace(
        symbol="SBIN-EQ",
        quantity=50,
        exchange="NSE",
    )

    lots, broker_units, lot_size, reason = mod._resolve_exit_lots(pos)

    assert lots == 50
    assert broker_units == 50
    assert lot_size == 1
    assert reason == "ok"


@pytest.mark.anyio
async def test_eod_exit_engine_submits_exit_orders_with_exit_purpose():
    order_router = MagicMock()
    order_router.submit_order = AsyncMock(
        return_value=(
            "hub-1",
            OrderResponse(
                broker_order_id="OID-1",
                status="FILLED",
                message="ok",
                filled_quantity=50,
                average_price=100.0,
            ),
        )
    )
    state_store = MagicMock()
    state_store.get_positions.return_value = [
        Position(
            symbol="SBIN-EQ",
            quantity=50,
            avg_price=100.0,
            product_type=ProductType.INTRADAY,
        )
    ]
    settings = SimpleNamespace(
        default_time_zone="UTC",
        enable_eod_exit=True,
        eod_exit_time="00:00",
        eod_exit_excluded_exchanges="",
    )
    runner = SimpleNamespace(
        is_running=True,
        tenant_id="tenant-1",
        broker_account_id="account-1",
    )

    engine = EODExitEngine(
        settings=settings,
        state_store=state_store,
        order_router=order_router,
    )

    await engine.maybe_force_exit_all([runner])

    order_router.submit_order.assert_awaited_once()
    submitted = order_router.submit_order.await_args.kwargs["order_req"]
    assert submitted.tag == "EOD_EXIT"
    assert submitted.purpose == OrderPurpose.EXIT


@pytest.mark.anyio
async def test_eod_exit_engine_skips_excluded_exchanges():
    order_router = MagicMock()
    order_router.submit_order = AsyncMock()
    state_store = MagicMock()
    state_store.get_positions.return_value = [
        SimpleNamespace(
            symbol="MCX-NG",
            quantity=1,
            product_type=ProductType.INTRADAY,
            exchange="MCX",
            symbol_token="1234",
        )
    ]
    settings = SimpleNamespace(
        default_time_zone="UTC",
        enable_eod_exit=True,
        eod_exit_time="00:00",
        eod_exit_excluded_exchanges="MCX",
    )
    runner = SimpleNamespace(
        is_running=True,
        tenant_id="tenant-1",
        broker_account_id="account-1",
    )

    engine = EODExitEngine(
        settings=settings,
        state_store=state_store,
        order_router=order_router,
    )

    await engine.maybe_force_exit_all([runner])

    order_router.submit_order.assert_not_awaited()


@pytest.mark.anyio
async def test_eod_exit_engine_retry_on_no_eligible_does_not_mark_exited_today():
    order_router = MagicMock()
    order_router.submit_order = AsyncMock()
    state_store = MagicMock()
    state_store.get_positions.return_value = []
    state_store.get_positions_status.return_value = {
        "status": "OK",
        "last_ok_ts": datetime.now(timezone.utc).isoformat(),
        "last_count": 0,
        "error_reason": None,
        "blocked_ts": None,
        "retry_after_seconds": None,
    }
    settings = SimpleNamespace(
        default_time_zone="UTC",
        enable_eod_exit=True,
        eod_exit_time="00:00",
        eod_exit_excluded_exchanges="",
        eod_exit_retry_on_no_eligible=True,
        eod_exit_require_fresh_position_sync=False,
        eod_exit_positions_max_age_seconds=60,
        eod_exit_position_telemetry=False,
        eod_exit_retry_cutoff_time="23:59",
    )
    runner = SimpleNamespace(
        is_running=True,
        tenant_id="tenant-1",
        broker_account_id="account-1",
    )

    engine = EODExitEngine(
        settings=settings,
        state_store=state_store,
        order_router=order_router,
    )

    await engine.maybe_force_exit_all([runner])

    assert engine._exited_today is None
    order_router.submit_order.assert_not_awaited()


@pytest.mark.anyio
async def test_eod_exit_engine_freshness_gate_requires_ok_snapshot_before_empty_conclusion():
    order_router = MagicMock()
    order_router.submit_order = AsyncMock()
    state_store = MagicMock()
    state_store.get_positions.return_value = []
    state_store.get_positions_status.side_effect = [
        {
            "status": "BLOCKED",
            "last_ok_ts": None,
            "last_count": None,
            "error_reason": "rate_limited",
            "blocked_ts": datetime.now(timezone.utc).isoformat(),
            "retry_after_seconds": 30.0,
        },
        {
            "status": "OK",
            "last_ok_ts": datetime.now(timezone.utc).isoformat(),
            "last_count": 0,
            "error_reason": None,
            "blocked_ts": None,
            "retry_after_seconds": None,
        },
    ]
    settings = SimpleNamespace(
        default_time_zone="UTC",
        enable_eod_exit=True,
        eod_exit_time="00:00",
        eod_exit_excluded_exchanges="",
        eod_exit_retry_on_no_eligible=False,
        eod_exit_require_fresh_position_sync=True,
        eod_exit_positions_max_age_seconds=60,
        eod_exit_position_telemetry=False,
        eod_exit_retry_cutoff_time="23:59",
    )
    runner = SimpleNamespace(
        is_running=True,
        tenant_id="tenant-1",
        broker_account_id="account-1",
    )

    engine = EODExitEngine(
        settings=settings,
        state_store=state_store,
        order_router=order_router,
    )

    await engine.maybe_force_exit_all([runner])
    assert engine._exited_today is None

    await engine.maybe_force_exit_all([runner])
    assert engine._exited_today is not None
    order_router.submit_order.assert_not_awaited()


@pytest.mark.anyio
async def test_eod_exit_engine_cancels_open_intraday_orders_when_enabled():
    order_router = MagicMock()
    order_router.submit_order = AsyncMock()
    state_store = MagicMock()
    state_store.get_positions.return_value = []
    state_store.get_positions_status.return_value = {
        "status": "OK",
        "last_ok_ts": datetime.now(timezone.utc).isoformat(),
        "last_count": 0,
        "error_reason": None,
        "blocked_ts": None,
        "retry_after_seconds": None,
    }
    cancel_mock = AsyncMock(
        return_value={
            "attempted": 2,
            "cancelled": 2,
            "failed": 0,
            "unsupported": 0,
            "skipped": 0,
        }
    )
    settings = SimpleNamespace(
        default_time_zone="UTC",
        enable_eod_exit=True,
        eod_exit_time="00:00",
        eod_exit_excluded_exchanges="",
        eod_exit_retry_on_no_eligible=False,
        eod_exit_require_fresh_position_sync=False,
        eod_exit_positions_max_age_seconds=60,
        eod_exit_position_telemetry=False,
        eod_exit_retry_cutoff_time="23:59",
        eod_cancel_open_orders_enabled=True,
    )
    runner = SimpleNamespace(
        is_running=True,
        tenant_id="tenant-1",
        broker_account_id="account-1",
        cancel_open_intraday_orders=cancel_mock,
    )

    engine = EODExitEngine(
        settings=settings,
        state_store=state_store,
        order_router=order_router,
    )

    await engine.maybe_force_exit_all([runner])

    cancel_mock.assert_awaited_once()
    order_router.submit_order.assert_not_awaited()
