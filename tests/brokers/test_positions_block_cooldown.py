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
    def __init__(self) -> None:
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        return "<html>Request Rejected</html>"


def test_positions_block_cooldown_grows_retry_after(monkeypatch):
    client = AngelBrokerClient(_dummy_account(), _dummy_secrets())
    stub = _StubOrderClient()
    client._order_client = stub
    
    # Mock both random.uniform and time.monotonic to make test deterministic
    monotonic_time = [0.0]  # Use list to allow modification in closure
    
    def mock_monotonic():
        return monotonic_time[0]
    
    def mock_uniform(*_args, **_kwargs):
        return 0.0
    
    monkeypatch.setattr("random.uniform", mock_uniform)
    monkeypatch.setattr("time.monotonic", mock_monotonic)

    first = asyncio.run(client.get_positions())
    
    # Don't advance time - second call should happen at the same time
    # This tests that the retry_after doesn't decrease due to time passing
    second = asyncio.run(client.get_positions())

    assert first.status == PositionsStatus.BLOCKED
    assert second.status == PositionsStatus.BLOCKED
    assert first.retry_after_seconds is not None
    assert second.retry_after_seconds is not None
    # Second call returns the remaining cooldown from first call at same time, so should be equal
    assert second.retry_after_seconds == first.retry_after_seconds
