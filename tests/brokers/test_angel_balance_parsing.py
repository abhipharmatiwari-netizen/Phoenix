import asyncio

from app.brokers.angel_client import AngelBrokerClient
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

    def get_rms(self):
        return self._response


def test_balance_parses_data_payload():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StubOrderClient(
        {
            "status": True,
            "data": {
                "availablecash": "123.45",
                "net": "200.0",
                "utilisedmargin": "50.0",
            },
        }
    )

    balance = asyncio.run(client.get_balance())

    assert balance.available == 123.45
    assert balance.total == 200.0
    assert balance.utilized == 50.0


def test_balance_parses_legacy_payload():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StubOrderClient(
        {"available": 10, "net": 20, "utilized": 5}
    )

    balance = asyncio.run(client.get_balance())

    assert balance.available == 10.0
    assert balance.total == 20.0
    assert balance.utilized == 5.0
