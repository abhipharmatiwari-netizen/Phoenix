from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import control_tower
from app.dashboard.auth import AdminContext, AdminRole, get_admin_context
from app.core.identifiers import BrokerAccountId, StrategyId, TenantId
from app.tenants.models import (
    BrokerAccountModel,
    StrategyConfigModel,
    SubscriptionModel,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(control_tower.router)
    app.dependency_overrides[get_admin_context] = (
        lambda: AdminContext(caller="admin", role=AdminRole.ADMIN)
    )
    return app
    

def test_matrix_all_false_when_no_configs(monkeypatch):
    tenant = SimpleNamespace(tenant_id="tenant-default", name="Default Tenant")

    monkeypatch.setattr(control_tower, "get_all_tenants", lambda: [tenant])
    monkeypatch.setattr(
        control_tower,
        "_discover_strategy_columns",
        lambda: [
            control_tower.StrategyColumn(
                strategy_id="ce_orb", display_name="CE ORB"
            ),
            control_tower.StrategyColumn(
                strategy_id="delta_strangle", display_name="Delta Strangle"
            ),
        ],
    )
    monkeypatch.setattr(
        control_tower, "get_strategy_configs_for_tenant", lambda _: []
    )

    client = TestClient(_build_app())
    resp = client.get("/api/control_tower/matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert data["matrix"]["tenant-default"]["ce_orb"] is False
    assert data["matrix"]["tenant-default"]["delta_strangle"] is False


def test_matrix_aggregates_enabled(monkeypatch):
    tenant = SimpleNamespace(tenant_id="tenant-default", name="Default Tenant")
    now = datetime.now(timezone.utc)

    def fake_configs(_tenant_id):
        return [
            StrategyConfigModel(
                strategy_config_id="cfg1",
                tenant_id=TenantId("tenant-default"),
                broker_account_id=BrokerAccountId("ba1"),
                strategy_id=StrategyId("ce_orb"),
                enabled=True,
                params={},
                created_at=now,
                updated_at=now,
            ),
            StrategyConfigModel(
                strategy_config_id="cfg2",
                tenant_id=TenantId("tenant-default"),
                broker_account_id=BrokerAccountId("ba2"),
                strategy_id=StrategyId("delta_strangle"),
                enabled=False,
                params={},
                created_at=now,
                updated_at=now,
            ),
        ]

    monkeypatch.setattr(control_tower, "get_all_tenants", lambda: [tenant])
    monkeypatch.setattr(
        control_tower,
        "_discover_strategy_columns",
        lambda: [
            control_tower.StrategyColumn(
                strategy_id="ce_orb", display_name="CE ORB"
            ),
            control_tower.StrategyColumn(
                strategy_id="delta_strangle", display_name="Delta Strangle"
            ),
        ],
    )
    monkeypatch.setattr(
        control_tower, "get_strategy_configs_for_tenant", fake_configs
    )

    client = TestClient(_build_app())
    resp = client.get("/api/control_tower/matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert data["matrix"]["tenant-default"]["ce_orb"] is True
    assert data["matrix"]["tenant-default"]["delta_strangle"] is False


def test_toggle_creates_configs_for_enabled_accounts(monkeypatch):
    now = datetime.now(timezone.utc)
    upserts: list[StrategyConfigModel] = []

    accounts = [
        BrokerAccountModel(
            broker_account_id=BrokerAccountId("ba1"),
            tenant_id=TenantId("tenant-a"),
            broker_type="angel",
            display_name="A1",
            client_code="c1",
            secret_ref="sec1",
            trading_mode="PAPER",
            enabled=True,
            default_strategies=[],
            created_at=now,
            updated_at=now,
        ),
        BrokerAccountModel(
            broker_account_id=BrokerAccountId("ba2"),
            tenant_id=TenantId("tenant-a"),
            broker_type="angel",
            display_name="A2",
            client_code="c2",
            secret_ref="sec2",
            trading_mode="PAPER",
            enabled=True,
            default_strategies=[],
            created_at=now,
            updated_at=now,
        ),
    ]

    active_sub = SubscriptionModel(
        subscription_id="sub1",
        tenant_id=TenantId("tenant-a"),
        broker_account_id=BrokerAccountId("ba1"),
        mode="PAPER",
        start_at=now - timedelta(hours=1),
        end_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )

    def fake_get_configs(tenant_id, strategy_id):
        return []

    def fake_get_accounts(tenant_id):
        assert tenant_id == TenantId("tenant-a")
        return accounts

    def fake_get_subs(broker_account_id):
        return [active_sub]

    def fake_upsert(model):
        upserts.append(model)
        return model

    monkeypatch.setattr(
        control_tower, "get_strategy_configs_for_tenant_strategy", fake_get_configs
    )
    monkeypatch.setattr(control_tower, "get_broker_accounts_for_tenant", fake_get_accounts)
    monkeypatch.setattr(control_tower, "get_subscriptions_for_account", fake_get_subs)
    monkeypatch.setattr(control_tower, "upsert_strategy_config", fake_upsert)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/control_tower/toggle",
        json={
            "tenant_id": "tenant-a",
            "strategy_id": "ce_orb",
            "enabled": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    # Should create configs for both eligible accounts
    assert len(upserts) == 2
    assert {cfg.broker_account_id for cfg in upserts} == {
        BrokerAccountId("ba1"),
        BrokerAccountId("ba2"),
    }


def test_toggle_updates_existing(monkeypatch):
    now = datetime.now(timezone.utc)
    updated: list[StrategyConfigModel] = []

    existing_cfgs = [
        StrategyConfigModel(
            strategy_config_id="cfg1",
            tenant_id=TenantId("tenant-z"),
            broker_account_id=BrokerAccountId("ba9"),
            strategy_id=StrategyId("delta_strangle"),
            enabled=True,
            params={},
            created_at=now,
            updated_at=now,
        )
    ]

    def fake_get_configs(tenant_id, strategy_id):
        return existing_cfgs

    def fake_upsert(model):
        updated.append(model)
        return model

    monkeypatch.setattr(
        control_tower, "get_strategy_configs_for_tenant_strategy", fake_get_configs
    )
    monkeypatch.setattr(control_tower, "upsert_strategy_config", fake_upsert)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/control_tower/toggle",
        json={
            "tenant_id": "tenant-z",
            "strategy_id": "delta_strangle",
            "enabled": False,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert len(updated) == 1
    assert updated[0].enabled is False


def test_toggle_emits_control_tower_app_log(monkeypatch, caplog):
    now = datetime.now(timezone.utc)
    existing_cfgs = [
        StrategyConfigModel(
            strategy_config_id="cfg1",
            tenant_id=TenantId("tenant-z"),
            broker_account_id=BrokerAccountId("ba9"),
            strategy_id=StrategyId("delta_strangle"),
            enabled=True,
            params={},
            created_at=now,
            updated_at=now,
        )
    ]

    monkeypatch.setattr(
        control_tower,
        "get_strategy_configs_for_tenant_strategy",
        lambda tenant_id, strategy_id: existing_cfgs,
    )
    monkeypatch.setattr(control_tower, "upsert_strategy_config", lambda model: model)
    monkeypatch.setattr(
        control_tower,
        "get_global_routing_table",
        lambda: SimpleNamespace(refresh=lambda: None),
    )
    caplog.set_level("INFO", logger="app.api.control_tower")

    client = TestClient(_build_app())
    resp = client.post(
        "/api/control_tower/toggle",
        json={
            "tenant_id": "tenant-z",
            "strategy_id": "delta_strangle",
            "enabled": False,
        },
    )

    assert resp.status_code == 200
    assert "event_type=CONTROL_TOWER_TOGGLE" in caplog.text
    assert "tenant_id=tenant-z" in caplog.text
