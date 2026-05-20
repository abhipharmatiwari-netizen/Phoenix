from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from app.api.models import Role
from app.dashboard import auth


class _Entitlements:
    def __init__(self, tenant_ids=(), broker_account_ids=(), all_tenants=False, source='test'):
        self.tenant_ids = tuple(tenant_ids)
        self.broker_account_ids = tuple(broker_account_ids)
        self.all_tenants = all_tenants
        self.source = source



def _settings(**overrides):
    values = {
        'admin_api_key': 'test-admin',
        'dashboard_hmac_auth_enabled': False,
        'dashboard_hmac_secret': None,
        'dashboard_hmac_max_skew_seconds': 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)



def _request(path: str = '/admin/tenants'):
    return SimpleNamespace(method='GET', url=SimpleNamespace(path=path), state=SimpleNamespace())


@pytest.mark.asyncio
async def test_get_admin_context_accepts_static_admin_key(monkeypatch):
    monkeypatch.setattr(auth, 'get_settings', lambda: _settings())
    ctx = await auth.get_admin_context(request=_request(), x_admin_key='test-admin')
    assert ctx.caller == 'admin'
    assert ctx.auth_source == 'admin_key'


@pytest.mark.asyncio
async def test_get_admin_context_accepts_bearer_identity(monkeypatch):
    monkeypatch.setattr(auth, 'authenticate_bearer_token', lambda _: {
        'id': 'user_1',
        'email': 'user@example.com',
        'role': Role.OPERATOR,
    })
    monkeypatch.setattr(
        auth,
        'resolve_user_entitlements',
        lambda **_: _Entitlements(tenant_ids=('tenant-1',), broker_account_ids=('acc-1',)),
    )
    ctx = await auth.get_admin_context(request=_request(), authorization='Bearer token-123')
    assert ctx.caller == 'user@example.com'
    assert ctx.role == auth.AdminRole.OPERATOR
    assert ctx.auth_source == 'bearer'
    assert ctx.tenant_ids == ('tenant-1',)
    assert ctx.broker_account_ids == ('acc-1',)


@pytest.mark.asyncio
async def test_get_admin_context_rejects_invalid_static_admin_key(monkeypatch):
    monkeypatch.setattr(auth, 'get_settings', lambda: _settings())
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_admin_context(request=_request(), x_admin_key='wrong')
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_get_admin_context_accepts_hmac_headers_when_enabled(monkeypatch):
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: _settings(
            dashboard_hmac_auth_enabled=True,
            dashboard_hmac_secret='hmac-secret',
        ),
    )
    timestamp = str(int(time.time()))
    request = _request('/admin/tenants')
    payload = f'{timestamp}:GET:/admin/tenants'.encode('utf-8')
    signature = hmac.new(b'hmac-secret', payload, hashlib.sha256).hexdigest()
    ctx = await auth.get_admin_context(
        request=request,
        x_admin_timestamp=timestamp,
        x_admin_signature=signature,
    )
    assert ctx.caller == 'admin_hmac'
    assert ctx.auth_source == 'admin_hmac'


@pytest.mark.asyncio
async def test_get_admin_context_rejects_expired_hmac_timestamp(monkeypatch):
    monkeypatch.setattr(
        auth,
        'get_settings',
        lambda: _settings(
            dashboard_hmac_auth_enabled=True,
            dashboard_hmac_secret='hmac-secret',
            dashboard_hmac_max_skew_seconds=1,
        ),
    )
    stale_timestamp = str(int(time.time()) - 120)
    request = _request('/admin/tenants')
    payload = f'{stale_timestamp}:GET:/admin/tenants'.encode('utf-8')
    signature = hmac.new(b'hmac-secret', payload, hashlib.sha256).hexdigest()
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
        admin_ctx=auth.AdminContext(
            caller='admin',
            role=auth.AdminRole.READONLY,
            tenant_ids=('tenant-1',),
            broker_account_ids=('acc-1',),
        ),
        path='/ws/dashboard',
        mode='delta',
        settings=settings,
        now_ts=100,
        ttl_seconds=30,
        nonce='nonce-1',
    )

    ctx = auth.verify_dashboard_ws_ticket(
        ticket.token,
        path='/ws/dashboard',
        mode='delta',
        settings=settings,
        now_ts=110,
    )

    assert ticket.path == '/ws/dashboard'
    assert ticket.mode == 'delta'
    assert ctx.caller == 'admin'
    assert ctx.role == auth.AdminRole.READONLY
    assert ctx.tenant_ids == ('tenant-1',)
    assert ctx.broker_account_ids == ('acc-1',)



def test_dashboard_ws_ticket_rejects_expired_or_mismatched_ticket():
    settings = _settings()
    expired_ticket = auth.issue_dashboard_ws_ticket(
        admin_ctx=auth.AdminContext(caller='admin'),
        path='/ws/dashboard',
        mode='delta',
        settings=settings,
        now_ts=100,
        ttl_seconds=5,
        nonce='nonce-2',
    )

    with pytest.raises(ValueError, match='expired'):
        auth.verify_dashboard_ws_ticket(
            expired_ticket.token,
            path='/ws/dashboard',
            mode='delta',
            settings=settings,
            now_ts=200,
        )

    fresh_ticket = auth.issue_dashboard_ws_ticket(
        admin_ctx=auth.AdminContext(caller='admin'),
        path='/ws/dashboard',
        mode='delta',
        settings=settings,
        now_ts=100,
        ttl_seconds=30,
        nonce='nonce-3',
    )

    with pytest.raises(ValueError, match='mode mismatch'):
        auth.verify_dashboard_ws_ticket(
            fresh_ticket.token,
            path='/ws/dashboard',
            mode='full',
            settings=settings,
            now_ts=110,
        )


@pytest.mark.asyncio
async def test_get_tenant_context_requires_header_for_admin_key(monkeypatch):
    request = _request('/tenant/me/accounts')
    admin_ctx = auth.AdminContext(caller='admin', role=auth.AdminRole.ADMIN, auth_source='admin_key', all_tenants=True)
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_tenant_context(request=request, admin_ctx=admin_ctx, x_tenant_id=None)
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.asyncio
async def test_get_tenant_context_requires_existing_tenant(monkeypatch):
    request = _request('/tenant/me/accounts')
    admin_ctx = auth.AdminContext(caller='admin', role=auth.AdminRole.ADMIN, auth_source='admin_key', all_tenants=True)
    monkeypatch.setattr(auth, 'get_tenant', lambda _: None)
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_tenant_context(request=request, admin_ctx=admin_ctx, x_tenant_id='tenant-1')
    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_get_tenant_context_returns_entitled_bearer_tenant(monkeypatch):
    request = _request('/tenant/me/accounts')
    admin_ctx = auth.AdminContext(
        caller='user@example.com',
        role=auth.AdminRole.OPERATOR,
        auth_source='bearer',
        tenant_ids=('tenant-1', 'tenant-2'),
        broker_account_ids=('acc-1',),
    )
    tenant = SimpleNamespace(tenant_id='tenant-2')
    monkeypatch.setattr(auth, 'get_tenant', lambda _: tenant)
    ctx = await auth.get_tenant_context(request=request, admin_ctx=admin_ctx, x_tenant_id='tenant-2')
    assert str(ctx.tenant_id) == 'tenant-2'
    assert ctx.broker_account_ids == ('acc-1',)


@pytest.mark.asyncio
async def test_get_tenant_context_rejects_non_entitled_bearer_tenant(monkeypatch):
    request = _request('/tenant/me/accounts')
    admin_ctx = auth.AdminContext(
        caller='user@example.com',
        role=auth.AdminRole.OPERATOR,
        auth_source='bearer',
        tenant_ids=('tenant-1',),
    )
    with pytest.raises(HTTPException) as exc_info:
        await auth.get_tenant_context(request=request, admin_ctx=admin_ctx, x_tenant_id='tenant-2')
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
