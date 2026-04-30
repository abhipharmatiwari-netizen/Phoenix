import pytest
from types import SimpleNamespace

from app.brokers.secrets import AngelSecrets
from app.core import angel_login
from app.core.angel_login import AngelLoginTokens


def test_angel_login_uses_postgres_secrets(monkeypatch):
    secrets = AngelSecrets(
        api_key="key",
        api_secret="",
        client_code="C1",
        pin="1234",
        totp_secret="TOTP",
        client_local_ip="127.0.0.1",
        client_public_ip="127.0.0.1",
        mac_address="00:00:00:00:00:00",
    )

    monkeypatch.setattr(
        angel_login, "_load_postgres_secrets_for_default_account", lambda: secrets
    )

    captured = {}

    def fake_login_with_secrets(_secrets):
        captured["secrets"] = _secrets
        return AngelLoginTokens(
            jwt_token="jwt",
            refresh_token="refresh",
            feed_token="feed",
            client_code="C1",
            api_key="key",
        )

    monkeypatch.setattr(angel_login, "angel_login_with_secrets", fake_login_with_secrets)

    result = angel_login.angel_login_and_get_tokens()
    assert captured["secrets"] == secrets
    assert result == {
        "jwtToken": "jwt",
        "feedToken": "feed",
        "clientCode": "C1",
        "API_KEY": "key",
        "client_local_ip": "127.0.0.1",
        "client_public_ip": "127.0.0.1",
        "mac_address": "00:00:00:00:00:00",
    }


def test_angel_login_env_missing_totp_raises(monkeypatch):
    monkeypatch.setattr(
        angel_login, "_load_postgres_secrets_for_default_account", lambda: None
    )
    monkeypatch.delenv("ANGEL_TOTP_SECRET", raising=False)

    with pytest.raises(ValueError, match="ANGEL_TOTP_SECRET environment variable not set"):
        angel_login.angel_login_and_get_tokens()


def test_angel_login_postgres_backend_requires_account_id(monkeypatch):
    monkeypatch.setattr(
        angel_login,
        "get_settings",
        lambda: SimpleNamespace(
            broker_secret_backend="postgres",
            hub_default_broker_account_id="",
        ),
    )
    monkeypatch.setattr(
        angel_login, "_load_postgres_secrets_for_default_account", lambda: None
    )
    monkeypatch.delenv("HUB_DEFAULT_BROKER_ACCOUNT_ID", raising=False)

    with pytest.raises(
        ValueError,
        match="BROKER_SECRET_BACKEND=postgres requires HUB_DEFAULT_BROKER_ACCOUNT_ID",
    ):
        angel_login.angel_login_and_get_tokens()


def test_angel_login_postgres_backend_without_secrets_raises(monkeypatch):
    monkeypatch.setattr(
        angel_login,
        "get_settings",
        lambda: SimpleNamespace(
            broker_secret_backend="postgres",
            hub_default_broker_account_id="A1",
        ),
    )
    monkeypatch.setattr(
        angel_login, "_load_postgres_secrets_for_default_account", lambda: None
    )

    with pytest.raises(
        ValueError,
        match="BROKER_SECRET_BACKEND=postgres but broker_credentials lookup returned no usable secrets",
    ):
        angel_login.angel_login_and_get_tokens()


def test_angel_login_rejects_live_placeholder_network_identity(monkeypatch):
    monkeypatch.setenv("TRADE_MODE", "LIVE")
    monkeypatch.setenv("ANGEL_TOTP_SECRET", "TOTP")
    monkeypatch.setenv("CLIENT_LOCAL_IP", "127.0.0.1")
    monkeypatch.setenv("CLIENT_PUBLIC_IP", "127.0.0.1")
    monkeypatch.setenv("MAC_ADDRESS", "AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(
        angel_login, "_load_postgres_secrets_for_default_account", lambda: None
    )

    with pytest.raises(ValueError, match="requires real broker network identity"):
        angel_login.angel_login_and_get_tokens()


def test_rate_limit_403_is_retryable():
    assert angel_login._is_retryable_login_response(
        403, "Access denied because of exceeding access rate"
    )
    assert not angel_login._is_retryable_login_response(403, "Forbidden")


def test_request_json_with_retry_retries_access_rate_403(monkeypatch):
    class _Resp:
        def __init__(self, status: int, body: str):
            self.status = status
            self._body = body

        def read(self):
            return self._body.encode("utf-8")

    class _Conn:
        def __init__(self):
            self.requests = []
            self._responses = [
                _Resp(403, "Access denied because of exceeding access rate"),
                _Resp(200, '{"status": true, "data": {"ok": true}}'),
            ]

        def request(self, method, path, body=None, headers=None):
            self.requests.append((method, path, body, headers))

        def getresponse(self):
            return self._responses.pop(0)

    monkeypatch.setattr(angel_login.rate_limiter, "acquire", lambda *_a, **_k: None)
    monkeypatch.setattr(angel_login.time, "sleep", lambda *_a, **_k: None)

    conn = _Conn()
    data = angel_login._request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/user/v1/loginByPassword",
        body="{}",
        headers={},
        limiter_key="loginByPassword",
        operation="ANGEL_LOGIN",
        max_attempts=2,
    )

    assert data.get("status") is True
    assert len(conn.requests) == 2


def test_request_json_with_retry_retries_transport_timeout(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return b'{"status": true, "data": {"ok": true}}'

    class _Conn:
        def __init__(self):
            self.requests = 0
            self.closed = 0

        def request(self, method, path, body=None, headers=None):
            _ = method, path, body, headers
            self.requests += 1
            if self.requests == 1:
                raise TimeoutError("Connection timed out")

        def getresponse(self):
            return _Resp()

        def close(self):
            self.closed += 1

    monkeypatch.setattr(angel_login.rate_limiter, "acquire", lambda *_a, **_k: None)
    monkeypatch.setattr(angel_login.time, "sleep", lambda *_a, **_k: None)

    conn = _Conn()
    data = angel_login._request_json_with_retry(
        conn=conn,
        method="POST",
        path="/rest/auth/angelbroking/user/v1/loginByPassword",
        body="{}",
        headers={},
        limiter_key="loginByPassword",
        operation="ANGEL_LOGIN",
        max_attempts=2,
    )

    assert data.get("status") is True
    assert conn.requests == 2
    assert conn.closed == 1


def test_make_angel_connection_uses_login_timeout_env(monkeypatch):
    observed = {}

    class _Conn:
        def __init__(self, host, timeout=None):
            observed["host"] = host
            observed["timeout"] = timeout

    monkeypatch.setenv("ANGEL_LOGIN_HTTP_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setattr(angel_login.http.client, "HTTPSConnection", _Conn)

    angel_login._make_angel_connection()

    assert observed == {"host": "apiconnect.angelone.in", "timeout": 3.5}
