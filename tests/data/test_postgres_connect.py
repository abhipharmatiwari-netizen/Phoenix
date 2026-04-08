from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

from app.data import postgres


def test_connect_with_retry_sets_kolkata_timezone_by_default(monkeypatch):
    captured: dict[str, object] = {}

    class _DummyConn:
        pass

    def _fake_connect(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _DummyConn()

    monkeypatch.setattr(postgres.psycopg, "connect", _fake_connect)
    monkeypatch.delenv("POSTGRES_SESSION_TIMEZONE", raising=False)
    monkeypatch.delenv("DEFAULT_TIME_ZONE", raising=False)
    monkeypatch.delenv("POSTGRES_CONNECT_OPTIONS", raising=False)

    conn = postgres.connect_with_retry("postgresql://user:pass@host:5432/db", max_attempts=1)

    assert isinstance(conn, _DummyConn)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("options") == "-c timezone=Asia/Kolkata"


def test_connect_with_retry_normalizes_legacy_calcutta_timezone(monkeypatch):
    captured: dict[str, object] = {}

    class _DummyConn:
        pass

    def _fake_connect(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _DummyConn()

    monkeypatch.setattr(postgres.psycopg, "connect", _fake_connect)
    monkeypatch.setenv("DEFAULT_TIME_ZONE", "Asia/Calcutta")
    monkeypatch.setenv("POSTGRES_CONNECT_OPTIONS", "-c statement_timeout=5000")
    monkeypatch.delenv("POSTGRES_SESSION_TIMEZONE", raising=False)

    conn = postgres.connect_with_retry("postgresql://user:pass@host:5432/db", max_attempts=1)

    assert isinstance(conn, _DummyConn)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("options") == "-c statement_timeout=5000 -c timezone=Asia/Kolkata"


def test_connect_with_retry_prefers_ipv4_for_host_docker_internal(monkeypatch):
    captured: dict[str, object] = {}

    class _DummyConn:
        pass

    def _fake_connect(dsn: str, **kwargs):
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return _DummyConn()

    monkeypatch.setattr(postgres.psycopg, "connect", _fake_connect)
    monkeypatch.setattr(
        postgres.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (postgres.socket.AF_INET, postgres.socket.SOCK_STREAM, 6, "", ("192.168.65.254", 0))
        ],
    )
    monkeypatch.delenv("POSTGRES_FORCE_IPV4", raising=False)

    conn = postgres.connect_with_retry(
        "postgresql://user:pass@host.docker.internal:5432/db",
        max_attempts=1,
    )

    assert isinstance(conn, _DummyConn)
    dsn = captured["dsn"]
    assert isinstance(dsn, str)
    params = conninfo_to_dict(dsn)
    assert params.get("host") == "host.docker.internal"
    assert params.get("hostaddr") == "192.168.65.254"
