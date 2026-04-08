import time
from types import SimpleNamespace

import pytest

from app.brokers import angel_client as angel_client_module
from app.brokers.angel_client import AngelBrokerClient


def _build_client(*, tenant_id: str, broker_account_id: str) -> AngelBrokerClient:
    return AngelBrokerClient(
        account=SimpleNamespace(
            broker_account_id=broker_account_id,
            tenant_id=tenant_id,
        ),
        secrets=SimpleNamespace(client_code="client-code"),
    )


@pytest.fixture(autouse=True)
def _clear_tenant_cooldowns():
    angel_client_module._TENANT_FETCH_COOLDOWN_UNTIL.clear()
    yield
    angel_client_module._TENANT_FETCH_COOLDOWN_UNTIL.clear()


def test_clear_cooldown_local_only_preserves_tenant_backoff(monkeypatch):
    client_a = _build_client(tenant_id="T1", broker_account_id="A1")
    client_b = _build_client(tenant_id="T1", broker_account_id="A2")

    monkeypatch.setattr(
        AngelBrokerClient,
        "_tenant_cooldown_local_clear_only_enabled",
        staticmethod(lambda: True),
    )

    client_a._set_cooldown(channel="orders", seconds=30.0)
    client_b._orders_fetch_cooldown_until = time.monotonic() + 5.0

    client_b._clear_cooldown(channel="orders")

    assert client_b._orders_fetch_cooldown_until == 0.0
    assert angel_client_module._tenant_get_cooldown_until("T1", "orders") > time.monotonic()
    assert client_b._cooldown_remaining(0.0, channel="orders") > 0.0


def test_clear_cooldown_local_only_still_clears_local_state(monkeypatch):
    client = _build_client(tenant_id="T1", broker_account_id="A1")

    monkeypatch.setattr(
        AngelBrokerClient,
        "_tenant_cooldown_local_clear_only_enabled",
        staticmethod(lambda: True),
    )

    client._orders_fetch_cooldown_until = time.monotonic() + 5.0

    client._clear_cooldown(channel="orders")

    assert client._orders_fetch_cooldown_until == 0.0
    assert client._cooldown_remaining(0.0, channel="orders") == 0.0


def test_clear_cooldown_flag_off_preserves_current_tenant_clear_behavior(monkeypatch):
    client_a = _build_client(tenant_id="T1", broker_account_id="A1")
    client_b = _build_client(tenant_id="T1", broker_account_id="A2")

    monkeypatch.setattr(
        AngelBrokerClient,
        "_tenant_cooldown_local_clear_only_enabled",
        staticmethod(lambda: False),
    )

    client_a._set_cooldown(channel="orders", seconds=30.0)
    client_b._orders_fetch_cooldown_until = time.monotonic() + 5.0

    client_b._clear_cooldown(channel="orders")

    assert client_b._orders_fetch_cooldown_until == 0.0
    assert angel_client_module._tenant_get_cooldown_until("T1", "orders") <= time.monotonic()
    assert client_b._cooldown_remaining(0.0, channel="orders") == 0.0
