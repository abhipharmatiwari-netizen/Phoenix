from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.dashboard import auth


def _settings(**overrides):
    values = {
        "admin_api_key": "test-admin",
        "dashboard_hmac_auth_enabled": False,
        "dashboard_hmac_secret": None,
        "dashboard_hmac_max_skew_seconds": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(path: str = "/admin/tenants"):
    return SimpleNamespace(method="GET", url=SimpleNamespace(path=path))


@pytest.mark.asyncio
async def test_get_admin_context_accepts_static_admin_key(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings())
    ctx = await auth.get_admin_context(request=_request(), x_admin_key="test-admin")
    assert ctx.caller == "admin"


@pytest.mark.asyncio
async def test_get_admin_context_rejects_invalid_static_admin_key(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings())
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_admin_context(request=_request(), x_admin_key="wrong")
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_get_admin_context_accepts_hmac_headers_when_enabled(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: _settings(
            dashboard_hmac_auth_enabled=True,
            dashboard_hmac_secret="hmac-secret",
        ),
    )
    timestamp = str(int(time.time()))
    request = _request("/admin/tenants")
    payload = f"{timestamp}:GET:/admin/tenants".encode("utf-8")
    signature = hmac.new(b"hmac-secret", payload, hashlib.sha256).hexdigest()
    ctx = await auth.get_admin_context(
        request=request,
        x_admin_timestamp=timestamp,
        x_admin_signature=signature,
    )
    assert ctx.caller == "admin_hmac"


@pytest.mark.asyncio
async def test_get_admin_context_rejects_expired_hmac_timestamp(monkeypatch):
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: _settings(
            dashboard_hmac_auth_enabled=True,
            dashboard_hmac_secret="hmac-secret",
            dashboard_hmac_max_skew_seconds=1,
        ),
    )
    stale_timestamp = str(int(time.time()) - 120)
    request = _request("/admin/tenants")
    payload = f"{stale_timestamp}:GET:/admin/tenants".encode("utf-8")
    signature = hmac.new(b"hmac-secret", payload, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_admin_context(
            request=request,
            x_admin_timestamp=stale_timestamp,
            x_admin_signature=signature,
        )
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_dashboard_ws_ticket_round_trip():
    settings = _settings()
    ticket = auth.issue_dashboard_ws_ticket(
        admin_ctx=auth.AdminContext(caller="admin", role=auth.AdminRole.READONLY),
        path="/ws/dashboard",
        mode="delta",
        settings=settings,
        now_ts=100,
        ttl_seconds=30,
        nonce="nonce-1",
    )

    ctx = auth.verify_dashboard_ws_ticket(
        ticket.token,
        path="/ws/dashboard",
        mode="delta",
        settings=settings,
        now_ts=110,
    )

    assert ticket.path == "/ws/dashboard"
    assert ticket.mode == "delta"
    assert ctx.caller == "admin"
    assert ctx.role == auth.AdminRole.READONLY


def test_dashboard_ws_ticket_rejects_expired_or_mismatched_ticket():
    settings = _settings()
    expired_ticket = auth.issue_dashboard_ws_ticket(
        admin_ctx=auth.AdminContext(caller="admin"),
        path="/ws/dashboard",
        mode="delta",
        settings=settings,
        now_ts=100,
        ttl_seconds=5,
        nonce="nonce-2",
    )

    with pytest.raises(ValueError, match="expired"):
        auth.verify_dashboard_ws_ticket(
            expired_ticket.token,
            path="/ws/dashboard",
            mode="delta",
            settings=settings,
            now_ts=200,
        )

    fresh_ticket = auth.issue_dashboard_ws_ticket(
        admin_ctx=auth.AdminContext(caller="admin"),
        path="/ws/dashboard",
        mode="delta",
        settings=settings,
        now_ts=100,
        ttl_seconds=30,
        nonce="nonce-3",
    )

    with pytest.raises(ValueError, match="mode mismatch"):
        auth.verify_dashboard_ws_ticket(
            fresh_ticket.token,
            path="/ws/dashboard",
            mode="full",
            settings=settings,
            now_ts=110,
        )


@pytest.mark.asyncio
async def test_get_tenant_context_requires_header():
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_tenant_context(x_tenant_id=None)
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_get_tenant_context_requires_existing_tenant(monkeypatch):
    monkeypatch.setattr(auth, "get_tenant", lambda _: None)
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_tenant_context(x_tenant_id="tenant-1")
    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_tenant_context_returns_tenant(monkeypatch):
    tenant = SimpleNamespace(tenant_id="tenant-1")
    monkeypatch.setattr(auth, "get_tenant", lambda _: tenant)
    ctx = await auth.get_tenant_context(x_tenant_id="tenant-1")
    assert str(ctx.tenant_id) == "tenant-1"
