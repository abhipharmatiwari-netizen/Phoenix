from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth_routes
from app.api.models import Role


@pytest.fixture
def auth_client(monkeypatch):
    users_db: dict[str, dict] = {}

    def _get_user_by_email(email: str):
        return users_db.get(email)

    def _get_user_by_id(user_id: str):
        for user in users_db.values():
            if str(user.get("id")) == str(user_id):
                return user
        return None

    def _create_user(*, user_id: str, email: str, name: str, password_hash: str, role):
        users_db[email] = {
            "id": user_id,
            "email": email,
            "name": name,
            "password_hash": password_hash,
            "role": role,
        }

    monkeypatch.setattr(auth_routes, "_get_user_by_email", _get_user_by_email)
    monkeypatch.setattr(auth_routes, "_get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(auth_routes, "_create_user", _create_user)
    monkeypatch.setattr(
        auth_routes,
        "_next_user_id",
        lambda: f"user_{len(users_db) + 1}",
    )
    monkeypatch.setattr(
        auth_routes,
        "_update_user_role",
        lambda email, role: bool(users_db.get(email)) and not users_db[email].__setitem__("role", role),
    )
    monkeypatch.setattr(
        auth_routes,
        "_token_secret",
        lambda: "unit-test-demo-auth-secret",
    )
    monkeypatch.setattr(
        auth_routes,
        "resolve_user_entitlements",
        lambda **_: type("Entitlements", (), {
            "tenant_ids": ("tenant-123",),
            "broker_account_ids": (),
            "all_tenants": False,
            "source": "unit-test",
        })(),
    )
    app = FastAPI()
    app.include_router(auth_routes.router)
    with TestClient(app) as client:
        yield client, users_db



def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}



def test_register_hashes_password_and_disallows_duplicate_email(auth_client):
    client, users_db = auth_client
    response = client.post(
        "/auth/register",
        json={
            "name": "Test User",
            "email": "test@example.com",
            "password": "p@ssword",
        },
    )
    assert response.status_code == 200
    stored_user = users_db["test@example.com"]
    assert stored_user["password_hash"] != "p@ssword"
    assert "password" not in stored_user

    duplicate = client.post(
        "/auth/register",
        json={
            "name": "Test User",
            "email": "test@example.com",
            "password": "p@ssword",
        },
    )
    assert duplicate.status_code == 409



def test_login_returns_signed_token_and_me_validates_it(auth_client):
    client, _users_db = auth_client
    register = client.post(
        "/auth/register",
        json={
            "name": "Demo User",
            "email": "demo@example.com",
            "password": "password-123",
        },
    )
    assert register.status_code == 200

    login = client.post(
        "/auth/login",
        json={"email": "demo@example.com", "password": "password-123"},
    )
    assert login.status_code == 200
    payload = login.json()
    assert "token" in payload
    assert not payload["token"].startswith("token_")

    me = client.get("/auth/me", headers=_auth_header(payload["token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "demo@example.com"
    assert me.json()["tenant_ids"] == ["tenant-123"]


def test_cookie_session_login_sets_httponly_refresh_cookie(auth_client, monkeypatch):
    client, _users_db = auth_client
    monkeypatch.setattr(
        "app.core.session_store.issue_refresh_token",
        lambda **_: ("refresh-cookie-value", None),
    )
    response = client.post(
        "/auth/register",
        json={
            "name": "Cookie User",
            "email": "cookie@example.com",
            "password": "password-123",
        },
    )
    assert response.status_code == 200

    login = client.post(
        "/auth/login",
        json={
            "email": "cookie@example.com",
            "password": "password-123",
            "cookie_session": True,
        },
    )

    assert login.status_code == 200
    payload = login.json()
    assert payload["token"]
    assert payload["refresh_token"] is None
    set_cookie = login.headers.get("set-cookie", "")
    assert "phoenix_refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie


def test_cookie_session_login_sets_secure_samesite_in_production(auth_client, monkeypatch):
    client, users_db = auth_client
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        "app.core.session_store.issue_refresh_token",
        lambda **_: ("refresh-cookie-value", None),
    )
    users_db["secure-cookie@example.com"] = {
        "id": "user-secure-cookie",
        "email": "secure-cookie@example.com",
        "name": "Secure Cookie User",
        "password_hash": auth_routes._hash_password("password-123"),
        "role": Role.ADMIN,
    }

    login = client.post(
        "/auth/login",
        json={
            "email": "secure-cookie@example.com",
            "password": "password-123",
            "cookie_session": True,
        },
    )

    assert login.status_code == 200
    set_cookie = login.headers.get("set-cookie", "")
    assert "phoenix_refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie


def test_refresh_accepts_cookie_session_without_body_token(auth_client, monkeypatch):
    client, users_db = auth_client
    users_db["refresh@example.com"] = {
        "id": "user-refresh",
        "email": "refresh@example.com",
        "name": "Refresh User",
        "password_hash": auth_routes._hash_password("unused"),
        "role": Role.ADMIN,
    }
    monkeypatch.setattr(
        "app.core.session_store.consume_refresh_token",
        lambda token: {
            "user_id": "user-refresh",
            "email": "refresh@example.com",
            "role": Role.ADMIN.value,
        } if token == "refresh-in" else None,
    )
    monkeypatch.setattr(
        "app.core.session_store.issue_refresh_token",
        lambda **_: ("refresh-out", None),
    )

    response = client.post(
        "/auth/refresh",
        json={"cookie_session": True},
        cookies={"phoenix_refresh_token": "refresh-in"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token"]
    assert payload["refresh_token"] is None
    assert "phoenix_refresh_token=" in response.headers.get("set-cookie", "")



def test_me_rejects_invalid_token(auth_client):
    client, _users_db = auth_client
    invalid = client.get("/auth/me", headers=_auth_header("invalid-token"))
    assert invalid.status_code == 401



def test_me_rejects_query_param_tokens(auth_client):
    client, _users_db = auth_client
    register = client.post(
        "/auth/register",
        json={
            "name": "Demo User",
            "email": "query@example.com",
            "password": "password-123",
        },
    )
    assert register.status_code == 200
    login = client.post(
        "/auth/login",
        json={"email": "query@example.com", "password": "password-123"},
    )
    token = login.json()["token"]
    response = client.get(f"/auth/me?token={token}", headers=_auth_header(token))
    assert response.status_code == 400
    assert "Authorization" in response.json()["detail"]



def test_promote_requires_admin_identity(auth_client):
    client, users_db = auth_client
    client.post(
        "/auth/register",
        json={
            "name": "Target User",
            "email": "target@example.com",
            "password": "password-123",
        },
    )
    client.post(
        "/auth/register",
        json={
            "name": "Viewer User",
            "email": "viewer@example.com",
            "password": "password-123",
        },
    )
    viewer_login = client.post(
        "/auth/login",
        json={"email": "viewer@example.com", "password": "password-123"},
    )
    response = client.post(
        "/auth/promote",
        json={"email": "target@example.com", "role": Role.ADMIN.value},
        headers=_auth_header(viewer_login.json()["token"]),
    )
    assert response.status_code == 403
    assert users_db["target@example.com"]["role"] == Role.VIEWER



def test_promote_allows_admin_identity(auth_client):
    client, users_db = auth_client
    client.post(
        "/auth/register",
        json={
            "name": "Target User",
            "email": "target@example.com",
            "password": "password-123",
        },
    )
    client.post(
        "/auth/register",
        json={
            "name": "Admin User",
            "email": "admin@example.com",
            "password": "password-123",
        },
    )
    users_db["admin@example.com"]["role"] = Role.ADMIN
    admin_login = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "password-123"},
    )
    response = client.post(
        "/auth/promote",
        json={"email": "target@example.com", "role": Role.ADMIN.value},
        headers=_auth_header(admin_login.json()["token"]),
    )
    assert response.status_code == 200
    assert users_db["target@example.com"]["role"] == Role.ADMIN
