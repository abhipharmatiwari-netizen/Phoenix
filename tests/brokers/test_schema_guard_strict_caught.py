import asyncio
import logging

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


class _StrictSchemaOrderClient:
    def get_positions(self):
        return [{"tradingsymbol": "SBIN-EQ", "netqty": "1"}]


def test_schema_guard_strict_is_caught_and_converted_to_blocked(monkeypatch, caplog):
    monkeypatch.setenv("BROKER_SCHEMA_CHECK_MODE", "strict")
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _StrictSchemaOrderClient()

    with caplog.at_level(logging.CRITICAL):
        result = asyncio.run(client.get_positions())

    assert result.status == PositionsStatus.BLOCKED
    assert result.reason == "schema_violation"
    assert result.retry_after_seconds is not None
    assert any("strict mode" in rec.message.lower() for rec in caplog.records)
