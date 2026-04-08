from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth_routes


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
        "_token_secret",
        lambda: "unit-test-demo-auth-secret",
    )
    app = FastAPI()
    app.include_router(auth_routes.router)
    with TestClient(app) as client:
        yield client, users_db


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

    me = client.get(f"/auth/me?token={payload['token']}")
    assert me.status_code == 200
    assert me.json()["email"] == "demo@example.com"


def test_me_rejects_invalid_token(auth_client):
    client, _users_db = auth_client
    invalid = client.get("/auth/me?token=invalid-token")
    assert invalid.status_code == 401
