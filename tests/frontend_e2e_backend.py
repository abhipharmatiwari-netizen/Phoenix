from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from app.api.models import Role

app = FastAPI(title="Phoenix Frontend E2E Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_users_db: dict[str, dict[str, Any]] = {}
_TOKEN_SECRET = "phoenix-frontend-e2e-secret"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _make_token(*, user_id: str, email: str, role: Role) -> str:
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "email": email,
        "role": role.value,
        "iat": now,
        "exp": now + 60 * 60,
    }
    header_part = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_part = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(
        _TOKEN_SECRET.encode("utf-8"),
        signing_input,
        hashlib.sha256,
    ).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(signature)}"


class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PromoteRequest(BaseModel):
    email: EmailStr
    role: Role


@app.post("/auth/register")
async def register(payload: SignupRequest) -> dict[str, Any]:
    if payload.email in _users_db:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already exists with this email",
        )

    user_id = f"user_{len(_users_db) + 1}"
    _users_db[payload.email] = {
        "id": user_id,
        "email": payload.email,
        "name": payload.name,
        "password": payload.password,
        "role": Role.VIEWER,
    }
    return {
        "id": user_id,
        "email": payload.email,
        "name": payload.name,
        "message": "User created successfully",
    }


@app.post("/auth/login")
async def login(payload: LoginRequest) -> dict[str, Any]:
    user = _users_db.get(payload.email)
    if user is None or str(user.get("password")) != payload.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    stored_role = user.get("role", Role.VIEWER)
    role = stored_role if isinstance(stored_role, Role) else Role(str(stored_role))
    return {
        "token": _make_token(
            user_id=str(user["id"]),
            email=str(user["email"]),
            role=role,
        ),
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": role.value,
        },
    }


@app.post("/auth/promote")
async def promote(payload: PromoteRequest) -> dict[str, Any]:
    user = _users_db.get(payload.email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    user["role"] = payload.role
    return {
        "message": f"User {payload.email} promoted to {payload.role.value}",
    }

_matrix: dict[str, Any] = {
    "tenants": [
        {"tenant_id": "tenant-default", "name": "Default Tenant"},
    ],
    "strategies": [
        {"strategy_id": "ce_orb", "display_name": "Ce Orb"},
        {"strategy_id": "delta_strangle", "display_name": "Delta Strangle"},
    ],
    "matrix": {
        "tenant-default": {
            "ce_orb": False,
            "delta_strangle": True,
        }
    },
}


@app.get("/health/summary")
async def health_summary() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "phoenix",
        "timestamp": "2026-03-13T00:00:00Z",
        "schema_status": "ok",
        "stream_worker_running": True,
        "watchdog_running": True,
        "last_position_sync_ok_ts": "2026-03-13T00:00:00Z",
        "last_broker_blocked_or_rate_limited_ts": None,
        "tracked_account_count": 1,
        "degraded_reasons": [],
        "schema": {
            "status": "ok",
            "checked_at": "2026-03-13T00:00:00Z",
            "missing_tables": [],
            "missing_indexes": [],
        },
        "watchdog": {},
        "per_account_staleness": [
            {
                "broker_account_id": "paper-broker",
                "last_sync_ts": "2026-03-13T00:00:00Z",
                "error_reason": None,
                "stale": False,
            }
        ],
        "alerts": {
            "firing_count": 0,
            "firing_rules": [],
        },
        "auto_mitigation": {
            "enabled": False,
            "total_events": 0,
        },
    }


@app.get("/bff/api/control_tower/matrix")
async def bff_control_tower_matrix() -> dict[str, Any]:
    return _matrix


@app.post("/bff/api/control_tower/toggle")
async def bff_control_tower_toggle(payload: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(payload.get("tenant_id") or "").strip()
    strategy_id = str(payload.get("strategy_id") or "").strip()
    enabled = bool(payload.get("enabled"))

    if tenant_id and strategy_id:
        tenant_row = _matrix["matrix"].setdefault(tenant_id, {})
        tenant_row[strategy_id] = enabled

    return {
        "tenant_id": tenant_id,
        "strategy_id": strategy_id,
        "enabled": enabled,
    }


@app.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"status": "ok"})
    try:
        while True:
            await asyncio.sleep(60)
    except Exception:
        await websocket.close()
