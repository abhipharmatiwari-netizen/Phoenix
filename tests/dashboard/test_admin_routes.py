import importlib
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

MODULE_PATH = "app.dashboard.admin_routes"
CALLABLE_NAMES = ['TenantUpsertRequest', 'TenantDeactivateRequest', 'BrokerAccountUpsertRequest', 'SubscriptionUpsertRequest', 'AdminTestOrderRequest', 'list_tenants', 'list_broker_accounts', 'list_subscriptions', 'list_runners', 'create_or_update_tenant', 'create_or_update_broker_account', 'create_or_update_subscription', 'deactivate_tenant', 'admin_test_order']


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


def _request():
    return SimpleNamespace(headers={}, client=None, state=SimpleNamespace())


def _admin_ctx(*, all_tenants=True, tenant_ids=(), broker_account_ids=()):
    admin_routes = importlib.import_module(MODULE_PATH)
    return admin_routes.AdminContext(
        caller="admin@test",
        role=admin_routes.AdminRole.ADMIN,
        auth_source="bearer" if not all_tenants else "admin_key",
        tenant_ids=tuple(tenant_ids),
        broker_account_ids=tuple(broker_account_ids),
        all_tenants=all_tenants,
    )


def _tenant(tenant_id="tenant-1", *, status="active", created_at=None):
    admin_routes = importlib.import_module(MODULE_PATH)
    return admin_routes.TenantModel(
        tenant_id=tenant_id,
        name="Tenant One",
        email="one@example.com",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def _account(account_id="acc-1", tenant_id="tenant-1", *, enabled=True):
    admin_routes = importlib.import_module(MODULE_PATH)
    return admin_routes.BrokerAccountModel(
        broker_account_id=account_id,
        tenant_id=tenant_id,
        broker_type="angel",
        display_name="Angel One",
        client_code="A1",
        secret_ref="secret/ref",
        trading_mode="LIVE",
        enabled=enabled,
        default_strategies=[],
    )


def _subscription(subscription_id="sub-1", account_id="acc-1", tenant_id="tenant-1"):
    admin_routes = importlib.import_module(MODULE_PATH)
    now = datetime.now(timezone.utc)
    return admin_routes.SubscriptionModel(
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        broker_account_id=account_id,
        mode="LIVE",
        start_at=now - timedelta(days=1),
        end_at=now + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_list_subscriptions_filters_scoped_admin(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(
        admin_routes,
        "get_all_subscriptions",
        lambda: [
            _subscription("sub-1", "acc-1", "tenant-1"),
            _subscription("sub-2", "acc-2", "tenant-2"),
        ],
    )

    payload = await admin_routes.list_subscriptions(
        ctx=_admin_ctx(
            all_tenants=False,
            tenant_ids=("tenant-1",),
            broker_account_ids=("acc-1",),
        )
    )

    assert payload["count"] == 1
    assert payload["subscriptions"][0].subscription_id == "sub-1"


def test_create_or_update_tenant_preserves_created_at(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    captured = []
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda request: None)
    monkeypatch.setattr(
        admin_routes,
        "get_tenant",
        lambda tenant_id: _tenant(str(tenant_id), created_at=created_at),
    )
    monkeypatch.setattr(
        admin_routes,
        "upsert_tenant",
        lambda model: captured.append(model) or model,
    )
    monkeypatch.setattr(admin_routes, "emit_audit_event", lambda **kwargs: None)

    result = admin_routes.create_or_update_tenant(
        _request(),
        admin_routes.TenantUpsertRequest(
            tenant_id="tenant-1",
            name="Tenant One Updated",
            email="new@example.com",
            status="active",
        ),
        ctx=_admin_ctx(),
    )

    assert result.created_at == created_at
    assert captured[0].created_at == created_at
    assert captured[0].name == "Tenant One Updated"


def test_create_or_update_subscription_rejects_account_tenant_mismatch(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda request: None)
    monkeypatch.setattr(
        admin_routes,
        "get_broker_account",
        lambda broker_account_id: _account(str(broker_account_id), "tenant-other"),
    )

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.create_or_update_subscription(
            _request(),
            admin_routes.SubscriptionUpsertRequest(
                subscription_id="sub-1",
                tenant_id="tenant-1",
                broker_account_id="acc-1",
                mode="LIVE",
                start_at=now,
                end_at=now + timedelta(days=1),
            ),
            ctx=_admin_ctx(),
        )

    assert exc_info.value.status_code == 400


def test_deactivate_tenant_blocks_running_runner(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda request: None)
    monkeypatch.setattr(admin_routes, "get_tenant", lambda tenant_id: _tenant(str(tenant_id)))
    monkeypatch.setattr(admin_routes, "get_all_broker_accounts", lambda: [_account()])
    runtime = SimpleNamespace(
        hub=SimpleNamespace(get_runner=lambda account_id: SimpleNamespace(is_running=True)),
        state_store=SimpleNamespace(
            get_positions=lambda account_id: [],
            get_order_snapshot=lambda account_id: [],
            get_orders=lambda account_id: [],
        ),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: runtime)

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.deactivate_tenant(
            tenant_id="tenant-1",
            request=_request(),
            req=admin_routes.TenantDeactivateRequest(reason="offboard"),
            ctx=_admin_ctx(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["blockers"][0]["reason"] == "runner_running"


def test_deactivate_tenant_rejects_account_scoped_admin(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda request: None)

    with pytest.raises(HTTPException) as exc_info:
        admin_routes.deactivate_tenant(
            tenant_id="tenant-1",
            request=_request(),
            req=admin_routes.TenantDeactivateRequest(reason="offboard"),
            ctx=_admin_ctx(
                all_tenants=False,
                tenant_ids=("tenant-1",),
                broker_account_ids=("acc-1",),
            ),
        )

    assert exc_info.value.status_code == 403


def test_deactivate_tenant_archives_disables_accounts_and_expires_subscriptions(monkeypatch):
    admin_routes = importlib.import_module(MODULE_PATH)
    tenant = _tenant()
    account = _account()
    subscription = _subscription()
    tenant_upserts = []
    account_upserts = []
    subscription_upserts = []
    audit_events = []
    monkeypatch.setattr(admin_routes, "check_rate_limit", lambda request: None)
    monkeypatch.setattr(admin_routes, "get_tenant", lambda tenant_id: tenant)
    monkeypatch.setattr(admin_routes, "get_all_broker_accounts", lambda: [account])
    monkeypatch.setattr(
        admin_routes,
        "get_subscriptions_for_account",
        lambda broker_account_id: [subscription],
    )
    monkeypatch.setattr(
        admin_routes,
        "upsert_tenant",
        lambda model: tenant_upserts.append(model) or model,
    )
    monkeypatch.setattr(
        admin_routes,
        "upsert_broker_account",
        lambda model: account_upserts.append(model) or model,
    )
    monkeypatch.setattr(
        admin_routes,
        "upsert_subscription",
        lambda model: subscription_upserts.append(model) or model,
    )
    monkeypatch.setattr(
        admin_routes,
        "emit_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )
    runtime = SimpleNamespace(
        hub=SimpleNamespace(get_runner=lambda account_id: None),
        state_store=SimpleNamespace(
            get_positions=lambda account_id: [],
            get_order_snapshot=lambda account_id: [],
            get_orders=lambda account_id: [],
        ),
    )
    monkeypatch.setattr(admin_routes, "get_hub_runtime", lambda: runtime)

    payload = admin_routes.deactivate_tenant(
        tenant_id="tenant-1",
        request=_request(),
        req=admin_routes.TenantDeactivateRequest(reason="offboard"),
        ctx=_admin_ctx(),
    )

    assert payload["status"] == "archived"
    assert tenant_upserts[0].status == "archived"
    assert account_upserts[0].enabled is False
    assert subscription_upserts[0].end_at <= datetime.now(timezone.utc)
    assert audit_events[-1]["action"] == "deactivate_tenant"
    assert audit_events[-1]["metadata"]["expired_subscriptions"] == 1
