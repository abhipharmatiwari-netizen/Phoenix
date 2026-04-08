import asyncio

from app.brokers.angel_client import AngelBrokerClient
from app.brokers.positions_types import PositionsStatus
from app.brokers.secrets import AngelSecrets
from app.core.identifiers import BrokerAccountId, TenantId
from app.core.order_client import AngelHTTPBlockedError
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


class _RateLimitedOrderClient:
    def get_positions(self):
        raise AngelHTTPBlockedError(
            "Rate limit exceeded",
            url="https://example.com",
            status=403,
            body_head="exceeding access rate",
            content_type="application/json",
            retry_after_seconds=12.0,
        )


class _WafRejectedOrderClient:
    def get_positions(self):
        raise AngelHTTPBlockedError(
            "HTML response",
            url="https://example.com",
            status=200,
            body_head="Request Rejected",
            content_type="text/html",
        )


def test_get_positions_returns_BLOCKED_on_403_rate_limit():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _RateLimitedOrderClient()

    result = asyncio.run(client.get_positions())

    assert result.status == PositionsStatus.BLOCKED
    assert result.reason == "rate_limited"
    assert result.http_status == 403
    assert result.retry_after_seconds == 12.0


def test_get_positions_returns_BLOCKED_on_200_html_request_rejected():
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    client._order_client = _WafRejectedOrderClient()

    result = asyncio.run(client.get_positions())

    assert result.status == PositionsStatus.BLOCKED
    assert result.reason == "waf_rejected"
    assert result.http_status == 200
    assert result.retry_after_seconds is not None
    assert result.retry_after_seconds >= 30.0
