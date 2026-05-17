import importlib
import json
import types
from datetime import datetime, timezone
import pytest

MODULE_PATH = "app.core.order_client"
CALLABLE_NAMES = ['AngelOrderClient']


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


def test_snap_price_to_tick_respects_side():
    mod = importlib.import_module(MODULE_PATH)

    assert mod._snap_price_to_tick(26.73, tick_size=5.0, transaction_type="BUY") == pytest.approx(26.70)
    assert mod._snap_price_to_tick(26.73, tick_size=5.0, transaction_type="SELL") == pytest.approx(26.75)


def test_place_order_slm_uses_zero_price_and_snapped_trigger(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _FakeResponse:
        status = 200

        def getheaders(self):
            return [("Content-Type", "application/json")]

        def read(self):
            return b'{"status": true, "message": "SUCCESS", "data": {"orderid": "OID-1"}}'

    class _FakeConnection:
        def __init__(self, *args, **kwargs):
            self.body = None

        def request(self, method, path, body=None, headers=None):
            self.body = body

        def getresponse(self):
            return _FakeResponse()

    conn = _FakeConnection()
    monkeypatch.setattr(mod.rate_limiter, "acquire", lambda key: None)
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", lambda *args, **kwargs: conn)

    client = mod.AngelOrderClient("jwt-token", "api-key")
    response = client.place_order(
        exchange="MCX",
        tradingsymbol="NATURALGAS24MAR26310CE",
        symboltoken="505131",
        transaction_type="BUY",
        quantity=1250,
        producttype="INTRADAY",
        ordertype="SLM",
        trigger_price=26.866,
        tick_size=0.05,
    )

    payload = json.loads(conn.body)
    assert payload["ordertype"] == "SLM"
    assert payload["price"] == "0"
    assert payload["triggerprice"] == "26.85"
    assert response["data"]["orderid"] == "OID-1"


def test_order_client_preserves_optional_broker_account_id():
    mod = importlib.import_module(MODULE_PATH)

    client = mod.AngelOrderClient(
        "jwt-token",
        "api-key",
        broker_account_id="A1",
    )

    assert client.broker_account_id == "A1"


def test_order_client_uses_explicit_client_network_headers():
    mod = importlib.import_module(MODULE_PATH)

    client = mod.AngelOrderClient(
        "jwt-token",
        "api-key",
        client_local_ip="10.1.2.3",
        client_public_ip="100.14.130.20",
        mac_address="00:11:22:33:44:55",
    )

    headers = client._headers()

    assert headers["X-ClientLocalIP"] == "10.1.2.3"
    assert headers["X-ClientPublicIP"] == "100.14.130.20"
    assert headers["X-MACAddress"] == "00:11:22:33:44:55"


def test_order_client_rejects_live_placeholder_network_identity(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("CLIENT_LOCAL_IP", "127.0.0.1")
    monkeypatch.setenv("CLIENT_PUBLIC_IP", "127.0.0.1")
    monkeypatch.setenv("MAC_ADDRESS", "AA:BB:CC:DD:EE:FF")

    with pytest.raises(ValueError, match="requires real broker network identity"):
        mod.AngelOrderClient("jwt-token", "api-key")


def test_fetch_historical_candles_formats_ist_window_and_returns_rows(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _FakeResponse:
        status = 200

        def getheaders(self):
            return [
                ("Content-Type", "application/json"),
                ("X-Request-ID", "req-hist-1"),
            ]

        def read(self):
            return (
                b'{"status": true, "message": "SUCCESS", "data": {"candles": '
                b'[["2026-03-10T09:30:00+05:30", 100, 101, 99.5, 100.5, 1000]]}}'
            )

    class _FakeConnection:
        def __init__(self, *args, **kwargs):
            self.body = None

        def request(self, method, path, body=None, headers=None):
            self.body = body

        def getresponse(self):
            return _FakeResponse()

    conn = _FakeConnection()
    monkeypatch.setattr(mod.rate_limiter, "acquire", lambda key: None)
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", lambda *args, **kwargs: conn)

    client = mod.AngelOrderClient("jwt-token", "api-key")
    candles = client.fetch_historical_candles(
        exchange="NFO",
        symboltoken="123456",
        interval="FIVE_MINUTE",
        from_datetime=datetime(2026, 3, 10, 4, 0, tzinfo=timezone.utc),
        to_datetime=datetime(2026, 3, 10, 4, 15, tzinfo=timezone.utc),
    )

    payload = json.loads(conn.body)
    assert payload["exchange"] == "NFO"
    assert payload["symboltoken"] == "123456"
    assert payload["interval"] == "FIVE_MINUTE"
    assert payload["fromdate"] == "2026-03-10 09:30"
    assert payload["todate"] == "2026-03-10 09:45"
    assert candles == [["2026-03-10T09:30:00+05:30", 100, 101, 99.5, 100.5, 1000]]


def test_fetch_market_quotes_uses_full_mode_and_returns_fetched_rows(monkeypatch):
    mod = importlib.import_module(MODULE_PATH)

    class _FakeResponse:
        status = 200

        def getheaders(self):
            return [
                ("Content-Type", "application/json"),
                ("X-Request-ID", "req-quote-1"),
            ]

        def read(self):
            return (
                b'{"status": true, "message": "SUCCESS", "data": {"fetched": ['
                b'{"exchange": "NFO", "symbolToken": "12345", "ltp": 42.8, '
                b'"opnInterest": 120000}]}}'
            )

    class _FakeConnection:
        def __init__(self, *args, **kwargs):
            self.body = None

        def request(self, method, path, body=None, headers=None):
            self.method = method
            self.path = path
            self.body = body

        def getresponse(self):
            return _FakeResponse()

    conn = _FakeConnection()
    monkeypatch.setattr(mod.rate_limiter, "acquire", lambda key: None)
    monkeypatch.setattr(mod.http.client, "HTTPSConnection", lambda *args, **kwargs: conn)

    client = mod.AngelOrderClient("jwt-token", "api-key")
    rows = client.fetch_market_quotes(
        mode="FULL",
        exchange_to_tokens={"NFO": ["12345"]},
    )

    payload = json.loads(conn.body)
    assert conn.path == "/rest/secure/angelbroking/market/v1/quote/"
    assert payload == {"mode": "FULL", "exchangeTokens": {"NFO": ["12345"]}}
    assert rows == [{"exchange": "NFO", "symbolToken": "12345", "ltp": 42.8, "opnInterest": 120000}]
