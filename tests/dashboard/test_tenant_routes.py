import importlib
import types
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.dashboard.auth import TenantContext
from app.brokers.base import Position, ProductType
from app.data.state_store import StateStore
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.pnl.types import PnLSnapshot, PnLSnapshotKey

MODULE_PATH = "app.dashboard.tenant_routes"
CALLABLE_NAMES = ['list_my_accounts', 'get_account_balance', 'get_account_positions', 'get_account_pnl', 'get_my_trades']


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


def test_tenant_routes_require_admin_key(monkeypatch):
    auth_module = importlib.import_module("app.dashboard.auth")
    tenant_routes = importlib.import_module("app.dashboard.tenant_routes")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "false")
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(
            admin_api_key="test-admin",
            dashboard_hmac_auth_enabled=False,
            dashboard_hmac_secret=None,
        ),
    )
    server.app.dependency_overrides[tenant_routes.get_tenant_context] = (
        lambda: TenantContext(tenant_id="tenant-123")
    )
    client = TestClient(server.app)
    try:
        resp = client.get(
            "/tenant/me/accounts",
            headers={"X-Tenant-Id": "tenant-123"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing X-Admin-Key header."
    finally:
        server.app.dependency_overrides.clear()


def test_tenant_routes_accept_admin_key(monkeypatch):
    auth_module = importlib.import_module("app.dashboard.auth")
    tenant_routes = importlib.import_module("app.dashboard.tenant_routes")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DASHBOARD_AUTH_DISABLED", "false")
    monkeypatch.setattr(
        auth_module,
        "get_settings",
        lambda: SimpleNamespace(
            admin_api_key="test-admin",
            dashboard_hmac_auth_enabled=False,
            dashboard_hmac_secret=None,
        ),
    )
    monkeypatch.setattr(
        tenant_routes,
        "get_broker_accounts_for_tenant",
        lambda tenant_id: [],
    )
    server.app.dependency_overrides[tenant_routes.get_tenant_context] = (
        lambda: TenantContext(tenant_id="tenant-123")
    )

    client = TestClient(server.app)
    try:
        resp = client.get(
            "/tenant/me/accounts",
            headers={"X-Admin-Key": "test-admin", "X-Tenant-Id": "tenant-123"},
        )
        assert resp.status_code == 200
    finally:
        server.app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_account_pnl_uses_live_position_marks_for_account_aggregate(monkeypatch):
    tenant_routes = importlib.import_module("app.dashboard.tenant_routes")
    state_store = StateStore()
    state_store.set_positions(
        "acc-1",
        [
            Position(
                symbol="NATURALGAS23APR26270CE",
                quantity=1250,
                avg_price=20.87,
                product_type=ProductType.CARRY_FORWARD,
            ),
            Position(
                symbol="BANKNIFTY30MAR2654000CE",
                quantity=-30,
                avg_price=543.45,
                product_type=ProductType.CARRY_FORWARD,
            ),
        ],
    )

    runtime = SimpleNamespace(
        state_store=state_store,
        pnl_engine=SimpleNamespace(
            get_snapshot=lambda **kwargs: None,
            get_current_realized_pnl=lambda **kwargs: 150.0,
        ),
    )

    monkeypatch.setattr(
        tenant_routes,
        "get_broker_account",
        lambda broker_account_id: SimpleNamespace(
            broker_account_id=broker_account_id,
            tenant_id="tenant-123",
        ),
    )
    monkeypatch.setattr(tenant_routes, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price_for_instrument",
        lambda **kwargs: {
            "NATURALGAS23APR26270CE": 23.70,
            "BANKNIFTY30MAR2654000CE": 569.10,
        }.get(kwargs.get("symbol")),
    )
    monkeypatch.setattr(tenant_routes.dashboard_bus, "get_last_price", lambda label: None)

    payload = await tenant_routes.get_account_pnl(
        broker_account_id=BrokerAccountId("acc-1"),
        strategy_id=None,
        ctx=TenantContext(tenant_id="tenant-123"),
    )

    assert payload["pnl"]["realized_pnl"] == 150.0
    assert payload["pnl"]["unrealized_pnl"] == pytest.approx(2768.0)
    assert payload["pnl"]["gross_exposure"] == pytest.approx(46698.0)


@pytest.mark.asyncio
async def test_get_account_pnl_overlays_live_marks_when_strategy_snapshot_has_no_mark_update(monkeypatch):
    tenant_routes = importlib.import_module("app.dashboard.tenant_routes")
    state_store = StateStore()
    state_store.set_positions(
        "acc-1",
        [
            Position(
                symbol="NATURALGAS23APR26270CE",
                quantity=1250,
                avg_price=20.87,
                product_type=ProductType.CARRY_FORWARD,
            ),
        ],
    )

    snapshot = PnLSnapshot(
        key=PnLSnapshotKey(
            tenant_id=TenantId("tenant-123"),
            broker_account_id=BrokerAccountId("acc-1"),
            strategy_id=StrategyId("delta_strangle"),
        ),
        realized_pnl=275.0,
        unrealized_pnl=0.0,
        gross_exposure=0.0,
        as_of=datetime(2026, 3, 26, 14, 20, tzinfo=timezone.utc),
        freshness_updated_at=datetime(2026, 3, 26, 14, 20, tzinfo=timezone.utc),
        freshness_source="trade",
    )

    runtime = SimpleNamespace(
        state_store=state_store,
        pnl_engine=SimpleNamespace(
            get_snapshot=lambda **kwargs: snapshot,
            get_current_realized_pnl=lambda **kwargs: 275.0,
        ),
    )

    monkeypatch.setattr(
        tenant_routes,
        "get_broker_account",
        lambda broker_account_id: SimpleNamespace(
            broker_account_id=broker_account_id,
            tenant_id="tenant-123",
        ),
    )
    monkeypatch.setattr(tenant_routes, "get_hub_runtime", lambda: runtime)
    monkeypatch.setattr(
        tenant_routes.dashboard_bus,
        "get_last_price_for_instrument",
        lambda **kwargs: 23.70 if kwargs.get("symbol") == "NATURALGAS23APR26270CE" else None,
    )
    monkeypatch.setattr(tenant_routes.dashboard_bus, "get_last_price", lambda label: None)

    payload = await tenant_routes.get_account_pnl(
        broker_account_id=BrokerAccountId("acc-1"),
        strategy_id=StrategyId("delta_strangle"),
        ctx=TenantContext(tenant_id="tenant-123"),
    )

    assert payload["strategy_id"] == "delta_strangle"
    assert payload["pnl"]["realized_pnl"] == 275.0
    assert payload["pnl"]["unrealized_pnl"] == pytest.approx(3537.5)
    assert payload["pnl"]["gross_exposure"] == pytest.approx(29625.0)
