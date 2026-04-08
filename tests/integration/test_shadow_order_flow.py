from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.brokers.base import (
    OrderRequest,
    OrderSide,
    OrderType,
    ProductType,
    TimeInForce,
)
from app.brokers.factory import create_broker_client
from app.core.dashboard_bus import dashboard_bus
from app.data.state_store import StateStore
from app.hub.account_runner import AccountRunner
from app.orders.order_outbox import InMemoryOrderSubmissionOutbox
from app.orders.router import OrderRouter
from app.pnl.pnl_engine import PnLEngine
from app.tenants.models import BrokerAccountModel


class _HubStub:
    def __init__(self, runner: AccountRunner) -> None:
        self._runner = runner

    def get_runner(self, _broker_account_id):
        return self._runner


def _shadow_account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id="A1",
        tenant_id="tenant-1",
        broker_type="angel",
        display_name="shadow-acct",
        client_code="client",
        secret_ref="secret",
    )


@pytest.mark.anyio
async def test_shadow_mode_routes_order_to_virtual_fill_and_updates_pnl(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SHADOW_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_FILL_LATENCY_MS", "0")
    monkeypatch.setenv("SHADOW_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("SHADOW_PARTIAL_FILL_PROB", "0")
    monkeypatch.setenv("ORDER_SUBMISSION_OUTBOX_ENABLED", "false")

    # Safety assertion: Angel paths must not be touched in SHADOW mode.
    import app.brokers.factory as factory_module

    monkeypatch.setattr(
        factory_module,
        "resolve_angel_secrets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("resolve_angel_secrets should not run in SHADOW")
        ),
    )
    monkeypatch.setattr(
        factory_module,
        "AngelBrokerClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("AngelBrokerClient should not be constructed in SHADOW")
        ),
    )

    account = _shadow_account()
    broker_client = create_broker_client(account=account, runtime_mode="SHADOW")
    assert type(broker_client).__name__ == "ShadowBrokerClient"

    state_store = StateStore()
    runner = AccountRunner(
        tenant_id=account.tenant_id,
        broker_account=account,
        broker_client=broker_client,
        runtime_mode="SHADOW",
        state_store=state_store,
        poll_interval_seconds=1.0,
    )
    # Router requires runner to be active; test keeps loop disabled for determinism.
    runner._running = True  # type: ignore[attr-defined]

    dashboard_bus.set_instrument_meta(
        {
            "NIFTY_ATM_CE_25000": {
                "label": "NIFTY_ATM_CE_25000",
                "symbol": "NIFTY26MAR25000CE",
                "token": "123",
                "lot_size": 50,
            }
        }
    )
    dashboard_bus.record_tick(
        "NIFTY_ATM_CE_25000",
        100.0,
        datetime.now(timezone.utc),
    )

    import app.orders.router as router_module

    monkeypatch.setattr(
        router_module,
        "get_settings",
        lambda: SimpleNamespace(default_time_zone="UTC"),
    )
    monkeypatch.setattr(
        router_module,
        "get_runtime_settings",
        lambda **_kwargs: SimpleNamespace(order_router_enforce_idempotency=False),
    )

    pnl_engine = PnLEngine()
    router = OrderRouter(
        hub=_HubStub(runner),
        capital_engine=None,
        risk_engine=None,
        profit_engine=None,
        state_store=state_store,
        pnl_engine=pnl_engine,
        order_lifecycle=None,
        submission_outbox=InMemoryOrderSubmissionOutbox(),
    )
    request = OrderRequest(
        symbol="NIFTY26MAR25000CE",
        quantity=1,  # lot; router preflight expands to 50
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.INTRADAY,
        time_in_force=TimeInForce.DAY,
        symbol_token="123",
    )
    _hub_order_id, response = await router.submit_order(
        tenant_id="tenant-1",
        broker_account_id="A1",
        strategy_id="ema20-trend",
        order_req=request,
    )

    assert response.status == "FILLED"
    assert response.execution_mode == "SHADOW"
    assert response.virtual is True
    assert int(response.filled_quantity) == 50
    assert state_store.get_last_order_response("A1") is not None

    snap = pnl_engine.get_snapshot("tenant-1", "A1", "ema20-trend")
    assert snap is not None
