import importlib
from datetime import datetime, timedelta, timezone
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hub.hub import Hub, pick_active_subscription
from app.tenants.models import BrokerAccountModel, SubscriptionModel, TenantModel


def test_import_module_succeeds():
    mod = importlib.import_module("app.hub.hub")
    assert hasattr(mod, "Hub")


def test_pick_active_subscription_prefers_latest():
    now = datetime.now(timezone.utc)
    active_old = SubscriptionModel(
        subscription_id="s1",
        tenant_id="t1",
        broker_account_id="b1",
        mode="PAPER",
        start_at=now - timedelta(hours=2),
        end_at=now + timedelta(hours=2),
    )
    active_newer = SubscriptionModel(
        subscription_id="s2",
        tenant_id="t1",
        broker_account_id="b1",
        mode="LIVE",
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(hours=1),
    )
    expired = SubscriptionModel(
        subscription_id="s3",
        tenant_id="t1",
        broker_account_id="b1",
        mode="PAPER",
        start_at=now - timedelta(days=2),
        end_at=now - timedelta(days=1),
    )

    assert pick_active_subscription([expired]) is None
    assert pick_active_subscription([active_old, active_newer]) == active_newer


@pytest.mark.anyio
async def test_hub_initialize_builds_runner(monkeypatch):
    tenant = TenantModel(tenant_id="t1", name="Tenant", email="t@example.com", status="active")
    account = BrokerAccountModel(
        broker_account_id="ba1",
        tenant_id="t1",
        broker_type="angel",
        display_name="Account",
        client_code="123",
        secret_ref="ref",
    )
    subscription = SubscriptionModel(
        subscription_id="sub1",
        tenant_id="t1",
        broker_account_id="ba1",
        mode="PAPER",
        start_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        end_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    module = importlib.import_module("app.hub.hub")
    monkeypatch.setattr(module, "get_all_active_tenants", lambda: [tenant])
    monkeypatch.setattr(module, "get_enabled_broker_accounts", lambda: [account])
    monkeypatch.setattr(module, "get_all_subscriptions", lambda: [subscription])
    monkeypatch.setattr(module, "create_broker_client", lambda account, runtime_mode: SimpleNamespace())
    monkeypatch.setattr(module.Hub, "_control_plane_ready", AsyncMock(return_value=True))

    hub = Hub()
    await hub.initialize()

    assert hub.runner_count == 1
    assert hub.list_runner_ids() == ["ba1"]
    assert hub.last_subscription_refresh is not None


@pytest.mark.anyio
async def test_hub_start_and_stop(monkeypatch):
    tenant = TenantModel(tenant_id="t1", name="Tenant", email="t@example.com", status="active")
    account = BrokerAccountModel(
        broker_account_id="ba1",
        tenant_id="t1",
        broker_type="angel",
        display_name="Account",
        client_code="123",
        secret_ref="ref",
    )
    subscription = SubscriptionModel(
        subscription_id="sub1",
        tenant_id="t1",
        broker_account_id="ba1",
        mode="PAPER",
        start_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        end_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    module = importlib.import_module("app.hub.hub")
    monkeypatch.setattr(module, "get_all_active_tenants", lambda: [tenant])
    monkeypatch.setattr(module, "get_enabled_broker_accounts", lambda: [account])
    monkeypatch.setattr(module, "get_all_subscriptions", lambda: [subscription])
    monkeypatch.setattr(module, "create_broker_client", lambda account, runtime_mode: SimpleNamespace())
    monkeypatch.setattr(module.Hub, "_control_plane_ready", AsyncMock(return_value=True))

    # Avoid spinning real background tasks
    monkeypatch.setattr(module.Hub, "_subscription_watchdog_loop", AsyncMock(return_value=None))
    monkeypatch.setattr(module.Hub, "_profit_watchdog_loop", AsyncMock(return_value=None))

    async def fake_start(self):
        self._running = True

    async def fake_stop(self):
        self._running = False

    monkeypatch.setattr(module.AccountRunner, "start", fake_start, raising=True)
    monkeypatch.setattr(module.AccountRunner, "stop", fake_stop, raising=True)

    hub = Hub()
    await hub.start_all()
    await asyncio.sleep(0)
    assert hub.runner_count == 1
    runner = hub.get_runner("ba1")
    assert runner is not None and runner.is_running is True

    await hub.stop_all()
    assert runner.is_running is False


def test_control_plane_outage_error_classifier():
    assert Hub._is_control_plane_outage_error("ConnectionTimeout('postgres down')")
    assert not Hub._is_control_plane_outage_error("ValueError('bad payload')")


def test_disabled_reconcile_level_keeps_live_expiry_as_warning():
    now = datetime.now(timezone.utc)
    live_expired = SubscriptionModel(
        subscription_id="live-expired",
        tenant_id="t1",
        broker_account_id="ba1",
        mode="LIVE",
        start_at=now - timedelta(days=2),
        end_at=now - timedelta(days=1),
    )
    shadow_expired = SubscriptionModel(
        subscription_id="shadow-expired",
        tenant_id="t1",
        broker_account_id="ba2",
        mode="SHADOW",
        start_at=now - timedelta(days=2),
        end_at=now - timedelta(days=1),
    )

    assert (
        Hub._disabled_reconcile_level("inactive_subscriptions", [live_expired])
        == logging.WARNING
    )
    assert (
        Hub._disabled_reconcile_level("inactive_subscriptions", [shadow_expired])
        == logging.INFO
    )


@pytest.mark.anyio
async def test_reconcile_deduplicates_expired_shadow_subscription_logs(
    monkeypatch, caplog
):
    tenant = TenantModel(tenant_id="t1", name="Tenant", email="t@example.com", status="active")
    account = BrokerAccountModel(
        broker_account_id="ba1",
        tenant_id="t1",
        broker_type="angel",
        display_name="Shadow Account",
        client_code="123",
        secret_ref="ref",
        trading_mode="SHADOW",
    )
    subscription = SubscriptionModel(
        subscription_id="sub-shadow-expired",
        tenant_id="t1",
        broker_account_id="ba1",
        mode="SHADOW",
        start_at=datetime.now(timezone.utc) - timedelta(days=3),
        end_at=datetime.now(timezone.utc) - timedelta(days=2),
    )

    module = importlib.import_module("app.hub.hub")
    monkeypatch.setattr(module, "get_all_active_tenants", lambda: [tenant])
    monkeypatch.setattr(module, "get_enabled_broker_accounts", lambda: [account])
    monkeypatch.setattr(module, "get_all_subscriptions", lambda: [subscription])
    monkeypatch.setattr(module.Hub, "_control_plane_ready", AsyncMock(return_value=True))

    hub = Hub()
    caplog.set_level(logging.DEBUG, logger="app.hub.hub")

    await hub._reconcile_runners_once()
    await hub._reconcile_runners_once()

    disabled_records = [
        record
        for record in caplog.records
        if "subscriptions exist but NONE are active" in record.getMessage()
    ]
    assert [record.levelno for record in disabled_records] == [
        logging.INFO,
        logging.DEBUG,
    ]
    assert hub.runner_count == 0


@pytest.mark.anyio
async def test_control_plane_ready_failure_sets_backoff(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(hub, "_uses_postgres_control_plane", lambda: True)

    async def _fake_to_thread(_fn):
        return "ConnectionTimeout('connection timeout expired')"

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)

    ready = await hub._control_plane_ready()
    assert ready is False
    assert hub._control_plane_probe_failures == 1
    assert hub._control_plane_next_probe_ts > 0.0


def test_hub_running_and_failed_runner_count_with_no_runners():
    hub = Hub()
    assert hub.runner_count == 0
    assert hub.registered_runner_count == 0
    assert hub.running_runner_count == 0
    assert hub.failed_runner_count == 0


def test_hub_running_runner_count_reflects_is_running():
    hub = Hub()
    r_running = SimpleNamespace(is_running=True)
    r_failed = SimpleNamespace(is_running=False)
    hub._account_runners["ba1"] = r_running
    hub._account_runners["ba2"] = r_failed
    hub._account_runners["ba3"] = r_failed

    assert hub.runner_count == 3
    assert hub.registered_runner_count == 3
    assert hub.running_runner_count == 1
    assert hub.failed_runner_count == 2


def test_hub_running_runner_count_all_running():
    hub = Hub()
    hub._account_runners["ba1"] = SimpleNamespace(is_running=True)
    hub._account_runners["ba2"] = SimpleNamespace(is_running=True)

    assert hub.registered_runner_count == 2
    assert hub.running_runner_count == 2
    assert hub.failed_runner_count == 0


def test_hub_running_runner_count_none_running():
    hub = Hub()
    hub._account_runners["ba1"] = SimpleNamespace(is_running=False)
    hub._account_runners["ba2"] = SimpleNamespace(is_running=False)

    assert hub.registered_runner_count == 2
    assert hub.running_runner_count == 0
    assert hub.failed_runner_count == 2
