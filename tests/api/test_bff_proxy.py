from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.api import bff_proxy
from app.server import app


class _StubAsyncClient:
    def __init__(self, calls: list[dict[str, object]]) -> None:
        self._calls = calls

    async def __aenter__(self) -> "_StubAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, url, headers=None, params=None):
        self._calls.append(
            {
                "method": "GET",
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
            }
        )
        return httpx.Response(
            200,
            json={"status": "ok"},
            request=httpx.Request("GET", url),
        )

    async def post(self, url, json=None, headers=None, params=None):
        self._calls.append(
            {
                "method": "POST",
                "url": url,
                "headers": dict(headers or {}),
                "params": dict(params or {}),
                "json": json,
            }
        )
        return httpx.Response(
            200,
            json={"status": "ok"},
            request=httpx.Request("POST", url),
        )



def test_bff_get_forwards_user_and_tenant_headers(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        bff_proxy,
        "get_settings",
        lambda: SimpleNamespace(
            backend_url="http://backend:8080",
        ),
    )
    # PR #240 round-7 review P2: the proxy now passes ``timeout=`` to
    # ``httpx.AsyncClient``; accept and ignore it in the stub so the
    # call signature still matches.
    monkeypatch.setattr(
        bff_proxy.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _StubAsyncClient(calls),
    )

    client = TestClient(app)
    response = client.get(
        "/bff/tenant/me/accounts?limit=10",
        headers={
            "Authorization": "Bearer user-token",
            "X-Tenant-Id": "tenant-123",
            "X-Request-Id": "req-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(calls) == 1
    forwarded = calls[0]
    assert forwarded["method"] == "GET"
    assert forwarded["url"] == "http://backend:8080/tenant/me/accounts"
    assert forwarded["params"] == {"limit": "10"}
    assert forwarded["headers"]["Authorization"] == "Bearer user-token"
    assert forwarded["headers"]["X-Tenant-Id"] == "tenant-123"
    assert forwarded["headers"]["X-Request-Id"] == "req-1"
    assert "X-Admin-Key" not in forwarded["headers"]



def test_bff_post_forwards_json_body_and_query_params(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        bff_proxy,
        "get_settings",
        lambda: SimpleNamespace(
            backend_url="http://backend:8080/",
        ),
    )
    # PR #240 round-7 review P2: the proxy now passes ``timeout=`` to
    # ``httpx.AsyncClient``; accept and ignore it in the stub so the
    # call signature still matches.
    monkeypatch.setattr(
        bff_proxy.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: _StubAsyncClient(calls),
    )

    client = TestClient(app)
    response = client.post(
        "/bff/api/control_tower/toggle?dry_run=true",
        headers={
            "Authorization": "Bearer operator-token",
            "X-Correlation-Id": "corr-1",
        },
        json={"tenant_id": "tenant-1", "strategy_id": "strat-1", "enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(calls) == 1
    forwarded = calls[0]
    assert forwarded["method"] == "POST"
    assert forwarded["url"] == "http://backend:8080/api/control_tower/toggle"
    assert forwarded["params"] == {"dry_run": "true"}
    assert forwarded["json"] == {
        "tenant_id": "tenant-1",
        "strategy_id": "strat-1",
        "enabled": True,
    }
    assert forwarded["headers"]["Authorization"] == "Bearer operator-token"
    assert forwarded["headers"]["X-Correlation-Id"] == "corr-1"
    assert forwarded["headers"]["Content-Type"] == "application/json"
