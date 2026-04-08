import asyncio

from app.brokers.angel_client import AngelBrokerClient
from app.brokers.positions_types import PositionsStatus
from app.brokers.secrets import AngelSecrets
from app.core.identifiers import BrokerAccountId, TenantId
from app.tenants.models import BrokerAccountModel


def _dummy_account() -> BrokerAccountModel:
    return BrokerAccountModel(
        broker_account_id=BrokerAccountId("ba_test"),
        tenant_id=TenantId("tenant_test"),
        broker_type="angel",
        display_name="test",
        client_code="test_client",
        secret_ref="secret/ref",
    )


def _dummy_secrets() -> AngelSecrets:
    return AngelSecrets(
        api_key="key",
        api_secret="secret",
        client_code="client",
        pin="1234",
        totp_secret="BASE32",
        client_local_ip="127.0.0.1",
        client_public_ip="127.0.0.1",
        mac_address="aa:bb:cc:dd:ee:ff",
    )


class _StubOrderClient:
    def __init__(self, response):
        self._response = response

    def get_positions(self):
        return self._response


def test_positions_guard_blocks_html_response():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StubOrderClient("<html>Request Rejected</html>")

    result = asyncio.run(client.get_positions())
    assert result.status == PositionsStatus.BLOCKED
    assert result.reason == "waf_rejected"


def test_positions_guard_blocks_none_response():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StubOrderClient(None)

    result = asyncio.run(client.get_positions())
    assert result.status == PositionsStatus.ERROR
    assert result.reason == "empty_body"


class _FailingOrderClient:
    def get_positions(self):
        raise Exception("network")


def test_positions_guard_raises_after_relogin_failure(monkeypatch):
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _FailingOrderClient()

    async def _noop_login(self):
        return None

    monkeypatch.setattr(AngelBrokerClient, "login", _noop_login, raising=True)

    result = asyncio.run(client.get_positions())
    assert result.status == PositionsStatus.ERROR
