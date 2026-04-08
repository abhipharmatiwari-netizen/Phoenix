from __future__ import annotations

import json

from app.core import universe_builder as ub


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class _FakeConnection:
    requests: list[dict] = []

    def __init__(self, _host: str, timeout: float = 0.0) -> None:
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        _FakeConnection.requests.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "headers": headers or {},
            }
        )

    def getresponse(self):
        return _FakeResponse(
            {
                "status": True,
                "data": {
                    "fetched": [
                        {"exchange": "NSE", "symbolToken": "99926000", "ltp": 25000.5},
                        {"exchange": "NFO", "symbolToken": "12345", "ltp": 100.0},
                    ]
                },
            }
        )

    def close(self) -> None:
        return None


def test_get_ltp_for_tokens_parses_batch_payload(monkeypatch):
    _FakeConnection.requests = []
    monkeypatch.setattr(ub.http.client, "HTTPSConnection", _FakeConnection)
    monkeypatch.setattr(ub.rate_limiter, "acquire", lambda *_args, **_kwargs: None)

    result = ub.get_ltp_for_tokens(
        jwt_token="jwt",
        api_key="api",
        exchange_to_tokens={"NSE": ["99926000"], "NFO": ["12345"]},
    )

    assert result[("NSE", "99926000")] == 25000.5
    assert result[("NFO", "12345")] == 100.0
    assert len(_FakeConnection.requests) == 1
    body = json.loads(_FakeConnection.requests[0]["body"])
    assert body["exchangeTokens"]["NSE"] == ["99926000"]
    assert body["exchangeTokens"]["NFO"] == ["12345"]


def test_get_ltp_for_tokens_retries_after_timeout_without_unboundlocal(monkeypatch):
    class _FlakyConnection:
        calls = 0

        def __init__(self, _host: str, timeout: float = 0.0) -> None:
            self.timeout = timeout

        def request(self, method: str, path: str, body=None, headers=None) -> None:
            return None

        def getresponse(self):
            _FlakyConnection.calls += 1
            if _FlakyConnection.calls == 1:
                raise TimeoutError("quote timed out")
            return _FakeResponse(
                {
                    "status": True,
                    "data": {
                        "fetched": [
                            {"exchange": "NSE", "symbolToken": "99926074", "ltp": 12000.0},
                        ]
                    },
                }
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(ub.http.client, "HTTPSConnection", _FlakyConnection)
    monkeypatch.setattr(ub.rate_limiter, "acquire", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ub, "UNIVERSE_QUOTE_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(ub.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ub.random, "uniform", lambda *_args, **_kwargs: 0.0)

    result = ub.get_ltp_for_tokens(
        jwt_token="jwt",
        api_key="api",
        exchange_to_tokens={"NSE": ["99926074"]},
    )

    assert result[("NSE", "99926074")] == 12000.0
    assert _FlakyConnection.calls == 2
